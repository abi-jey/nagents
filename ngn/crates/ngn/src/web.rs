use std::{net::SocketAddr, sync::Arc, time::Duration};

use anyhow::{Result, ensure};
use axum::{
    Json, Router,
    body::Body,
    extract::{
        DefaultBodyLimit, Path, Query, State, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    http::{HeaderMap, HeaderValue, Method, Request, StatusCode},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use futures_util::SinkExt;
use ngn_core::{ApprovalMode, Host, queues::QueueAction};
use serde::Deserialize;
use serde_json::{Value, json};
use subtle::ConstantTimeEq;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

#[derive(Clone)]
struct WebState {
    host: Arc<Host>,
    authority: String,
    token: String,
    shutdown: CancellationToken,
}

struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail":self.1}))).into_response()
    }
}
type ApiResult = std::result::Result<Json<Value>, ApiError>;
fn failure(error: anyhow::Error) -> ApiError {
    ApiError(StatusCode::CONFLICT, error.to_string())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RunInput {
    prompt: String,
    request_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct QueueControl {
    action: QueueAction,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Decision {
    run_id: String,
    approval_id: String,
    allow: bool,
}
#[derive(Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
struct Cursor {
    after: i64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Subscription {
    session_id: String,
    #[serde(default)]
    after: i64,
}

fn exactly(headers: &HeaderMap, name: &str, expected: &str) -> bool {
    let values: Vec<_> = headers.get_all(name).iter().collect();
    values.len() == 1 && values[0].as_bytes() == expected.as_bytes()
}

fn token_matches(value: &[u8], expected: &str) -> bool {
    bool::from(value.ct_eq(expected.as_bytes()))
}

async fn boundary(State(state): State<WebState>, request: Request<Body>, next: Next) -> Response {
    let rejected = |status, detail: &str| ApiError(status, detail.into()).into_response();
    let path = request.uri().path();
    let method = request.method();
    let headers = request.headers();
    let origin = format!("http://{}", state.authority);
    let navigation = path == "/"
        && method == Method::GET
        && exactly(headers, "sec-fetch-mode", "navigate")
        && exactly(headers, "sec-fetch-dest", "document");
    let websocket = path == "/api/events";
    let valid = if !exactly(headers, "host", &state.authority) {
        Err((
            StatusCode::FORBIDDEN,
            "Untrusted Host; use the exact loopback address printed by ngn",
        ))
    } else if !navigation
        && (headers
            .get("sec-fetch-site")
            .is_some_and(|value| ![b"same-origin".as_slice(), b"none"].contains(&value.as_bytes()))
            || (headers.contains_key("origin") && !exactly(headers, "origin", &origin)))
    {
        Err((StatusCode::FORBIDDEN, "Same-origin requests only"))
    } else if ![Method::GET, Method::HEAD, Method::POST].contains(method) {
        Err((StatusCode::METHOD_NOT_ALLOWED, "Method not supported"))
    } else if !["/api/events"].contains(&path)
        && request.uri().query().is_some()
        && !path.ends_with("/events")
    {
        Err((
            StatusCode::BAD_REQUEST,
            "Query parameters are not supported on this route",
        ))
    } else if websocket {
        let protocols: Vec<_> = headers
            .get_all("sec-websocket-protocol")
            .iter()
            .filter_map(|value| value.to_str().ok())
            .flat_map(|value| value.split(',').map(str::trim))
            .collect();
        if protocols.len() != 2
            || !protocols.contains(&"ngn.events.v1")
            || protocols
                .iter()
                .filter(|protocol| {
                    protocol
                        .strip_prefix("ngn.token.")
                        .is_some_and(|token| token_matches(token.as_bytes(), &state.token))
                })
                .count()
                != 1
            || !exactly(headers, "origin", &origin)
        {
            Err((
                StatusCode::FORBIDDEN,
                "Invalid WebSocket origin or protocol token",
            ))
        } else {
            Ok(())
        }
    } else if path.starts_with("/api/")
        && !(path == "/api/bootstrap" && method == Method::GET)
        && (headers.get_all("x-ngn-token").iter().count() != 1
            || !headers
                .get("x-ngn-token")
                .is_some_and(|value| token_matches(value.as_bytes(), &state.token)))
    {
        Err((
            StatusCode::FORBIDDEN,
            "Invalid host token; reconnect to this ngn instance",
        ))
    } else if method == Method::POST
        && (!exactly(headers, "origin", &origin)
            || !headers.get("content-type").is_some_and(|value| {
                [
                    b"application/json".as_slice(),
                    b"application/json; charset=utf-8",
                ]
                .contains(&value.as_bytes())
            }))
    {
        Err((
            StatusCode::FORBIDDEN,
            "Mutations require exact Origin and application/json",
        ))
    } else {
        Ok(())
    };
    let mut response = match valid {
        Ok(()) => next.run(request).await,
        Err((status, detail)) => rejected(status, detail),
    };
    for (name, value) in [
        ("cache-control", "no-store"),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-resource-policy", "same-origin"),
        (
            "content-security-policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        ),
    ] {
        response
            .headers_mut()
            .insert(name, HeaderValue::from_static(value));
    }
    response
}

fn router(state: WebState) -> Router {
    Router::new()
        .route(
            "/",
            get(|| async { Html(include_str!("../../../web/index.html")) }),
        )
        .route(
            "/app.js",
            get(|| async {
                (
                    [("content-type", "application/javascript; charset=utf-8")],
                    include_str!("../../../web/app.js"),
                )
            }),
        )
        .route(
            "/style.css",
            get(|| async {
                (
                    [("content-type", "text/css; charset=utf-8")],
                    include_str!("../../../web/style.css"),
                )
            }),
        )
        .route("/api/bootstrap", get(bootstrap))
        .route("/api/state", get(snapshot))
        .route("/api/sessions", get(sessions).post(create_session))
        .route("/api/sessions/{id}/events", get(history))
        .route("/api/sessions/{id}/runs", post(start))
        .route("/api/sessions/{id}/queue", get(queue).post(control_queue))
        .route("/api/sessions/{id}/tasks", get(tasks))
        .route("/api/runs/{id}/cancel", post(cancel))
        .route("/api/runs/{id}/steer", get(steering).post(steer))
        .route("/api/approvals", post(decide))
        .route("/api/events", get(events))
        .layer(DefaultBodyLimit::max(64 * 1024))
        .layer(middleware::from_fn_with_state(state.clone(), boundary))
        .with_state(state)
}

async fn bootstrap(State(state): State<WebState>) -> ApiResult {
    let provider = state.host.config.selected_provider().ok();
    Ok(Json(
        json!({"version":env!("CARGO_PKG_VERSION"), "protocol_version":ngn_protocol::VERSION, "token":state.token, "workspace":state.host.config.workspace, "backend":state.host.config.backend, "provider":state.host.config.provider, "model":provider.map(|provider| provider.model.as_str()), "read_only":state.host.config.read_only}),
    ))
}
async fn snapshot(State(state): State<WebState>) -> ApiResult {
    Ok(Json(
        json!({"runs":state.host.active().await, "approvals":state.host.approvals().await}),
    ))
}
async fn sessions(State(state): State<WebState>) -> ApiResult {
    Ok(Json(
        json!({"sessions":state.host.store.sessions().await.map_err(failure)?}),
    ))
}
async fn create_session(State(state): State<WebState>, Json(_): Json<Empty>) -> ApiResult {
    Ok(Json(
        json!({"session":state.host.store.create_session().await.map_err(failure)?}),
    ))
}
async fn tasks(State(state): State<WebState>, Path(id): Path<String>) -> ApiResult {
    state.host.store.session(&id).await.map_err(failure)?;
    Ok(Json(
        json!({"tasks":state.host.store.tasks(&id).await.map_err(failure)?, "limits":state.host.config.subagents}),
    ))
}
async fn queue(State(state): State<WebState>, Path(id): Path<String>) -> ApiResult {
    Ok(Json(
        json!({"queue": state.host.queue(&id).await.map_err(failure)?, "limit": ngn_core::queues::MAX_SESSION_INPUTS}),
    ))
}
async fn control_queue(
    State(state): State<WebState>,
    Path(id): Path<String>,
    Json(input): Json<QueueControl>,
) -> ApiResult {
    Ok(Json(
        json!({"queue": state.host.control_queue(&id, input.action).await.map_err(failure)?}),
    ))
}
async fn history(
    State(state): State<WebState>,
    Path(id): Path<String>,
    Query(cursor): Query<Cursor>,
) -> ApiResult {
    if cursor.after < 0 {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "Cursor must be nonnegative".into(),
        ));
    }
    state.host.store.session(&id).await.map_err(failure)?;
    let records = state
        .host
        .store
        .events(&id, cursor.after)
        .await
        .map_err(failure)?;
    Ok(Json(
        json!({"has_more":records.len() == 1000, "events":records}),
    ))
}
async fn start(
    State(state): State<WebState>,
    Path(id): Path<String>,
    Json(input): Json<RunInput>,
) -> ApiResult {
    Ok(Json(
        json!({"run":state.host.submit(&id, &input.prompt, &input.request_id, ApprovalMode::Interactive).await.map_err(failure)?}),
    ))
}
async fn cancel(
    State(state): State<WebState>,
    Path(id): Path<String>,
    Json(_): Json<Empty>,
) -> ApiResult {
    state.host.cancel(&id).await.map_err(failure)?;
    Ok(Json(json!({"cancelled":true})))
}
async fn steering(State(state): State<WebState>, Path(id): Path<String>) -> ApiResult {
    Ok(Json(
        json!({"steering": state.host.steering(&id).await.map_err(failure)?}),
    ))
}
async fn steer(
    State(state): State<WebState>,
    Path(id): Path<String>,
    input: std::result::Result<Json<RunInput>, axum::extract::rejection::JsonRejection>,
) -> ApiResult {
    let Json(input) = input.map_err(|error| {
        ApiError(
            error.status(),
            "Invalid steering JSON; expected only prompt and request_id".into(),
        )
    })?;
    Ok(Json(
        json!({"receipt": state.host.steer(&id, &input.prompt, &input.request_id).await.map_err(failure)?}),
    ))
}
async fn decide(State(state): State<WebState>, Json(input): Json<Decision>) -> ApiResult {
    state
        .host
        .decide(&input.run_id, &input.approval_id, input.allow)
        .await
        .map_err(failure)?;
    Ok(Json(json!({"accepted":true})))
}
async fn events(
    State(state): State<WebState>,
    Query(input): Query<Subscription>,
    upgrade: WebSocketUpgrade,
) -> std::result::Result<Response, ApiError> {
    if input.after < 0 {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "Cursor must be nonnegative".into(),
        ));
    }
    state
        .host
        .store
        .session(&input.session_id)
        .await
        .map_err(failure)?;
    Ok(upgrade
        .protocols(["ngn.events.v1"])
        .max_message_size(1024)
        .on_upgrade(move |socket| stream(socket, state, input))
        .into_response())
}

async fn stream(mut socket: WebSocket, state: WebState, input: Subscription) {
    let mut receiver = state.host.events.subscribe(); // Subscribe before durable replay.
    let mut after = input.after;
    loop {
        loop {
            let Ok(records) = state.host.store.events(&input.session_id, after).await else {
                return;
            };
            let count = records.len();
            for record in records {
                let Ok(encoded) = serde_json::to_string(&record) else {
                    return;
                };
                if !matches!(
                    tokio::time::timeout(
                        Duration::from_secs(5),
                        socket.send(Message::Text(encoded.into()))
                    )
                    .await,
                    Ok(Ok(()))
                ) {
                    return;
                }
                after = record.sequence;
            }
            if count < 1000 {
                break;
            }
        }
        tokio::select! {
            _ = state.shutdown.cancelled() => { let _ = socket.close().await; return; }
            message = socket.recv() => match message { Some(Ok(Message::Ping(data))) => { if socket.send(Message::Pong(data)).await.is_err() { return; } }, Some(Ok(Message::Pong(_))) => (), _ => return },
            received = receiver.recv() => match received {
                Ok(record) if record.session_id == input.session_id && record.sequence > after => (), // Replay from the durable cursor, including any skipped records.
                Ok(_) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => (),
                Err(_) => return,
            },
        }
    }
}

pub async fn serve(host: Arc<Host>, address: SocketAddr) -> Result<()> {
    ensure!(
        address.ip().is_loopback(),
        "Web serving is loopback-only; remote deployment needs a separately authenticated boundary"
    );
    let listener = tokio::net::TcpListener::bind(address).await?;
    let actual = listener.local_addr()?;
    let state = WebState {
        host: host.clone(),
        authority: actual.to_string(),
        token: format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple()),
        shutdown: CancellationToken::new(),
    };
    eprintln!(
        "ngn {} · Rust web host\nOpen http://{actual}\nCtrl+C stops the host and joins its agent runners.",
        env!("CARGO_PKG_VERSION")
    );
    let shutdown = state.shutdown.clone();
    let shutdown_host = host.clone();
    let result = axum::serve(listener, router(state))
        .with_graceful_shutdown(async move {
            let _ = crate::shutdown_signal().await;
            shutdown.cancel();
            shutdown_host.shutdown().await;
        })
        .await;
    host.shutdown().await;
    result?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ngn_core::{Config, config::Backend};
    use tower::ServiceExt;

    #[tokio::test]
    async fn host_origin_token_and_security_headers_are_enforced() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let state = WebState {
            host,
            authority: "127.0.0.1:8765".into(),
            token: "test-token".into(),
            shutdown: CancellationToken::new(),
        };
        let app = router(state.clone());
        for (path, host_header, token, origin, method, expected) in [
            (
                "/api/bootstrap",
                "evil.example",
                "",
                "",
                "GET",
                StatusCode::FORBIDDEN,
            ),
            (
                "/api/bootstrap",
                "127.0.0.1:8765",
                "",
                "",
                "GET",
                StatusCode::OK,
            ),
            (
                "/api/sessions",
                "127.0.0.1:8765",
                "",
                "",
                "GET",
                StatusCode::FORBIDDEN,
            ),
            (
                "/api/sessions",
                "127.0.0.1:8765",
                "test-token",
                "http://evil.example",
                "POST",
                StatusCode::FORBIDDEN,
            ),
            (
                "/api/sessions",
                "127.0.0.1:8765",
                "test-token",
                "http://127.0.0.1:8765",
                "POST",
                StatusCode::OK,
            ),
        ] {
            let mut request = Request::builder()
                .method(method)
                .uri(path)
                .header("host", host_header)
                .header("content-type", "application/json");
            if !token.is_empty() {
                request = request.header("x-ngn-token", token);
            }
            if !origin.is_empty() {
                request = request.header("origin", origin);
            }
            let response = app
                .clone()
                .oneshot(request.body(Body::from("{}")).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), expected, "{path} {method}");
            assert_eq!(response.headers()["cache-control"], "no-store");
            assert_eq!(response.headers()["x-frame-options"], "DENY");
        }
        state.host.shutdown().await;
    }

    #[tokio::test]
    async fn steering_routes_are_origin_token_protected_strict_and_never_fall_back_to_fifo() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let session = host.store.create_session().await.unwrap();
        let run = host
            .start(
                &session.id,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny,
            )
            .await
            .unwrap();
        let state = WebState {
            host,
            authority: "127.0.0.1:8765".into(),
            token: "test-token".into(),
            shutdown: CancellationToken::new(),
        };
        let app = router(state.clone());
        for (method, token, origin, extra, expected) in [
            (
                "GET",
                "",
                "http://127.0.0.1:8765",
                false,
                StatusCode::FORBIDDEN,
            ),
            (
                "POST",
                "",
                "http://127.0.0.1:8765",
                false,
                StatusCode::FORBIDDEN,
            ),
            (
                "POST",
                "test-token",
                "http://foreign.example",
                false,
                StatusCode::FORBIDDEN,
            ),
            (
                "POST",
                "test-token",
                "http://127.0.0.1:8765",
                true,
                StatusCode::UNPROCESSABLE_ENTITY,
            ),
            (
                "POST",
                "test-token",
                "http://127.0.0.1:8765",
                false,
                StatusCode::CONFLICT,
            ),
            (
                "GET",
                "test-token",
                "http://127.0.0.1:8765",
                false,
                StatusCode::OK,
            ),
        ] {
            let mut input =
                json!({"prompt":"not a queued run", "request_id":Uuid::new_v4().to_string()});
            if extra {
                input["run_id"] = json!("foreign");
            }
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(format!("/api/runs/{}/steer", run.id))
                        .header("host", &state.authority)
                        .header("x-ngn-token", token)
                        .header("origin", origin)
                        .header("content-type", "application/json")
                        .body(Body::from(if method == "POST" {
                            input.to_string()
                        } else {
                            String::new()
                        }))
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), expected);
        }
        assert!(
            state
                .host
                .steering(&run.id)
                .await
                .unwrap()
                .receipts
                .is_empty()
        );
        assert!(
            state
                .host
                .queue(&session.id)
                .await
                .unwrap()
                .entries
                .is_empty()
        );
        state.host.shutdown().await;
    }

    #[tokio::test]
    async fn task_routes_preserve_topology_and_do_not_promote_children_to_roots() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let session = host.store.create_session().await.unwrap();
        let root = host
            .start(
                &session.id,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny,
            )
            .await
            .unwrap();
        let child = host.tool(&root, ngn_protocol::ToolCall { call_id:"http-child".into(), name:"to_agent".into(), arguments:json!({"prompt":"demo child", "title":"HTTP reviewer", "read_only":true}) }).await.unwrap();
        assert!(child.error.is_empty());
        let state = WebState {
            host,
            authority: "127.0.0.1:8765".into(),
            token: "test-token".into(),
            shutdown: CancellationToken::new(),
        };
        let app = router(state.clone());
        let request = Request::builder()
            .uri(format!("/api/sessions/{}/tasks", session.id))
            .header("host", &state.authority)
            .header("x-ngn-token", &state.token)
            .body(Body::empty())
            .unwrap();
        let response = app.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let payload: Value = serde_json::from_slice(
            &axum::body::to_bytes(response.into_body(), 64 * 1024)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(payload["tasks"][0]["parent_run_id"], root.id);
        assert_eq!(payload["tasks"][0]["read_only"], true);
        assert_eq!(payload["tasks"][0]["status"], "completed");
        assert!(
            payload["tasks"][0].get("result").is_none(),
            "Catalogs must remain metadata-only"
        );
        let request = Request::builder()
            .method("POST")
            .uri(format!(
                "/api/sessions/{}/runs",
                child.result["task"]["session_id"].as_str().unwrap()
            ))
            .header("host", &state.authority)
            .header("x-ngn-token", &state.token)
            .header("origin", format!("http://{}", state.authority))
            .header("content-type", "application/json")
            .body(Body::from(
                json!({"prompt":"escalate", "request_id":Uuid::new_v4().to_string()}).to_string(),
            ))
            .unwrap();
        assert_eq!(
            app.oneshot(request).await.unwrap().status(),
            StatusCode::CONFLICT
        );
        state.host.shutdown().await;
    }
    #[tokio::test]
    async fn queue_routes_accept_busy_inputs_and_validate_controls_and_identity() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = Host::open(
            Config {
                workspace: directory.path().into(),
                data_dir: directory.path().join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap();
        let session = host.store.create_session().await.unwrap().id;
        let state = WebState {
            host,
            authority: "127.0.0.1:8765".into(),
            token: "test-token".into(),
            shutdown: CancellationToken::new(),
        };
        let app = router(state.clone());
        async fn request(
            app: &Router,
            path: &str,
            body: Option<Value>,
            expected: StatusCode,
        ) -> Value {
            let request = Request::builder()
                .method(if body.is_some() { "POST" } else { "GET" })
                .uri(path)
                .header("host", "127.0.0.1:8765")
                .header("origin", "http://127.0.0.1:8765")
                .header("x-ngn-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from(
                    body.map(|value| value.to_string()).unwrap_or_default(),
                ))
                .unwrap();
            let response = app.clone().oneshot(request).await.unwrap();
            assert_eq!(response.status(), expected);
            let bytes = axum::body::to_bytes(response.into_body(), 64 * 1024)
                .await
                .unwrap();
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        }
        let queue_path = format!("/api/sessions/{session}/queue");
        let runs_path = format!("/api/sessions/{session}/runs");
        let paused = request(
            &app,
            &queue_path,
            Some(json!({"action":"pause"})),
            StatusCode::OK,
        )
        .await;
        assert_eq!(paused["queue"]["paused"], true);
        let input = json!({"prompt":"demo hold", "request_id":Uuid::new_v4().to_string()});
        let first = request(&app, &runs_path, Some(input.clone()), StatusCode::OK).await;
        let again = request(&app, &runs_path, Some(input), StatusCode::OK).await;
        assert_eq!(first["run"]["id"], again["run"]["id"]);
        assert_eq!(first["run"]["status"], "queued");
        request(
            &app,
            &queue_path,
            Some(json!({"action":"resume", "approved":true})),
            StatusCode::UNPROCESSABLE_ENTITY,
        )
        .await;
        request(
            &app,
            &queue_path,
            Some(json!({"action":"retry_interrupted"})),
            StatusCode::UNPROCESSABLE_ENTITY,
        )
        .await;
        request(
            &app,
            &queue_path,
            Some(json!({"action":"resume"})),
            StatusCode::OK,
        )
        .await;
        let second = request(
            &app,
            &runs_path,
            Some(json!({"prompt":"later", "request_id":Uuid::new_v4().to_string()})),
            StatusCode::OK,
        )
        .await;
        assert_eq!(second["run"]["status"], "queued");
        let snapshot = request(&app, &queue_path, None, StatusCode::OK).await;
        assert_eq!(
            snapshot["queue"]["entries"][0]["run"]["id"],
            second["run"]["id"]
        );
        request(
            &app,
            &format!("/api/runs/{}/cancel", second["run"]["id"].as_str().unwrap()),
            Some(json!({})),
            StatusCode::OK,
        )
        .await;
        assert_eq!(state.host.active().await.len(), 1);
        let records = state.host.store.events(&session, 0).await.unwrap();
        assert!(records.iter().any(|record| record.run_id.is_empty()
            && matches!(record.event, ngn_protocol::Event::QueueUpdated { .. })));
        let response = app
            .oneshot(
                Request::builder()
                    .uri(&queue_path)
                    .header("host", &state.authority)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
        state.host.shutdown().await;
    }
}
