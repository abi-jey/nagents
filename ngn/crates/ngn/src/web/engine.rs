//! Same-origin browser gateway for the opt-in Python engine.
//!
//! Only explicitly registered v1 operations can reach the engine. Never
//! forward inbound credentials, headers, or an arbitrary destination.

use std::{net::SocketAddr, time::Duration};

use anyhow::{Result, ensure};
use axum::{
    Json, Router,
    extract::{
        DefaultBodyLimit, Path, Query, State, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    http::StatusCode,
    middleware,
    response::Response,
    routing::{get, post},
};
use ngn_api::{
    Client, EVENTS_PATH, EVENTS_SUBPROTOCOL, Error, Events,
    models::{
        ApprovalDecision, InputLookup, QueueUpdate, RunStart, SessionCreate, SessionFork,
        SessionRename, SteeringInput,
    },
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use super::{ApiError, BrowserBoundary, boundary, static_file};

#[derive(Clone)]
struct EngineWeb {
    engine: Client,
    browser_token: String,
    shutdown: CancellationToken,
}

fn error_response(error: Error) -> ApiError {
    match error {
        Error::Http(status, detail) if status.is_client_error() => ApiError(status, detail),
        _ => ApiError(
            StatusCode::BAD_GATEWAY,
            "The Python engine is unavailable.".into(),
        ),
    }
}

fn reply<T: Serialize>(result: Result<T, Error>) -> Result<Json<T>, ApiError> {
    result.map(Json).map_err(error_response)
}

fn created<T: Serialize>(
    status: StatusCode,
    result: Result<T, Error>,
) -> Result<(StatusCode, Json<T>), ApiError> {
    reply(result).map(|value| (status, value))
}

async fn bootstrap(State(state): State<EngineWeb>) -> Result<Json<serde_json::Value>, ApiError> {
    let info = state.engine.info().await.map_err(error_response)?;
    let config = state.engine.config().await.map_err(error_response)?;
    Ok(Json(json!({
        "transport": "engine-v1",
        "version": info.version,
        "protocol_version": info.protocol_version,
        "capabilities": info.capabilities,
        "token": state.browser_token,
        "workspace": config.workspace,
        "backend": if config.provider == "demo" { "demo" } else { "nagents" },
        "provider": config.provider,
        "model": config.model,
        "read_only": config.read_only,
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HistoryQuery {
    #[serde(default)]
    after: u64,
    #[serde(default = "history_limit")]
    limit: u64,
}

fn history_limit() -> u64 {
    256
}

async fn history(
    State(state): State<EngineWeb>,
    Path(id): Path<String>,
    Query(input): Query<HistoryQuery>,
) -> Result<Json<ngn_api::models::History>, ApiError> {
    reply(state.engine.history(&id, input.after, input.limit).await)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Subscription {
    session_id: String,
    #[serde(default)]
    after: u64,
}

async fn events(
    State(state): State<EngineWeb>,
    Query(input): Query<Subscription>,
    upgrade: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    // The browser's socket token authorizes only this gateway. The generated
    // client supplies the distinct engine bearer credential on its own socket.
    if input.session_id.is_empty() || input.session_id.len() > 200 {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            "A session ID is required.".into(),
        ));
    }
    let source = state
        .engine
        .subscribe(input.after, Some(&input.session_id))
        .await
        .map_err(error_response)?;
    Ok(upgrade
        .protocols([EVENTS_SUBPROTOCOL])
        .max_message_size(1024)
        .max_frame_size(1024)
        .on_upgrade(move |socket| relay(socket, source, state.shutdown)))
}

async fn relay(mut browser: WebSocket, mut engine: Events, shutdown: CancellationToken) {
    loop {
        tokio::select! {
            _ = shutdown.cancelled() => return,
            message = browser.recv() => match message {
                Some(Ok(Message::Ping(_) | Message::Pong(_))) => (),
                _ => return,
            },
            record = engine.next() => {
                let Ok(Some(record)) = record else { return; };
                let Ok(payload) = serde_json::to_string(&record) else { return; };
                // No intermediate queue. A slow browser is disconnected so
                // it can recover from its last durable cursor on reconnect.
                if !matches!(tokio::time::timeout(Duration::from_secs(5),
                    browser.send(Message::Text(payload.into()))).await, Ok(Ok(()))) {
                    return;
                }
            }
        }
    }
}

fn router(state: EngineWeb, authority: String) -> Router {
    let security = BrowserBoundary {
        authority,
        token: state.browser_token.clone(),
        events_path: EVENTS_PATH,
    };
    Router::new()
        .route("/", get(static_file))
        .route("/assets/{*path}", get(static_file))
        .route("/api/bootstrap", get(bootstrap))
        .route(
            "/api/v1/info",
            get(|State(s): State<EngineWeb>| async move { reply(s.engine.info().await) }),
        )
        .route(
            "/api/v1/config",
            get(|State(s): State<EngineWeb>| async move { reply(s.engine.config().await) }),
        )
        .route(
            "/api/v1/state",
            get(|State(s): State<EngineWeb>| async move { reply(s.engine.snapshot().await) }),
        )
        .route(
            "/api/v1/sessions",
            get(|State(s): State<EngineWeb>| async move { reply(s.engine.sessions().await) }).post(
                |State(s): State<EngineWeb>, Json(input): Json<SessionCreate>| async move {
                    created(StatusCode::CREATED, s.engine.create_session(&input).await)
                },
            ),
        )
        .route(
            "/api/v1/sessions/{id}",
            get(
                |State(s): State<EngineWeb>, Path(id): Path<String>| async move {
                    reply(s.engine.session(&id).await)
                },
            ),
        )
        .route(
            "/api/v1/sessions/{id}/rename",
            post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<SessionRename>| async move {
                    reply(s.engine.rename_session(&id, &input).await)
                },
            ),
        )
        .route(
            "/api/v1/sessions/{id}/fork",
            post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<SessionFork>| async move {
                    created(
                        StatusCode::CREATED,
                        s.engine.fork_session(&id, &input).await,
                    )
                },
            ),
        )
        .route("/api/v1/sessions/{id}/history", get(history))
        .route(
            "/api/v1/sessions/{id}/runs",
            post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<RunStart>| async move {
                    created(StatusCode::ACCEPTED, s.engine.start_run(&id, &input).await)
                },
            ),
        )
        .route(
            "/api/v1/sessions/{id}/receipts",
            post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<InputLookup>| async move {
                    reply(s.engine.input_receipt(&id, &input).await)
                },
            ),
        )
        .route(
            "/api/v1/sessions/{id}/queue",
            get(
                |State(s): State<EngineWeb>, Path(id): Path<String>| async move {
                    reply(s.engine.queue(&id).await)
                },
            )
            .post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<QueueUpdate>| async move {
                    reply(s.engine.control_queue(&id, &input).await)
                },
            ),
        )
        .route(
            "/api/v1/sessions/{id}/tasks",
            get(
                |State(s): State<EngineWeb>, Path(id): Path<String>| async move {
                    reply(s.engine.tasks(&id).await)
                },
            ),
        )
        .route(
            "/api/v1/runs/{id}/cancel",
            post(
                |State(s): State<EngineWeb>, Path(id): Path<String>| async move {
                    reply(s.engine.cancel_run(&id).await)
                },
            ),
        )
        .route(
            "/api/v1/runs/{id}/steer",
            get(
                |State(s): State<EngineWeb>, Path(id): Path<String>| async move {
                    reply(s.engine.steering(&id).await)
                },
            )
            .post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<SteeringInput>| async move {
                    reply(s.engine.steer(&id, &input).await)
                },
            ),
        )
        .route(
            "/api/v1/approvals/{id}",
            post(
                |State(s): State<EngineWeb>,
                 Path(id): Path<String>,
                 Json(input): Json<ApprovalDecision>| async move {
                    reply(s.engine.decide_approval(&id, &input).await)
                },
            ),
        )
        .route(
            "/api/v1/providers",
            get(|State(s): State<EngineWeb>| async move { reply(s.engine.providers().await) }),
        )
        .route(
            "/api/v1/providers/{id}/models",
            get(
                |State(s): State<EngineWeb>, Path(id): Path<String>| async move {
                    reply(s.engine.models(&id).await)
                },
            ),
        )
        .route(EVENTS_PATH, get(events))
        .layer(DefaultBodyLimit::max(64 * 1024))
        .layer(middleware::from_fn_with_state(security, boundary))
        .with_state(state)
}

pub(crate) async fn serve_python(engine: Client, address: SocketAddr) -> Result<()> {
    ensure!(address.ip().is_loopback(), "Web serving is loopback-only");
    let listener = tokio::net::TcpListener::bind(address).await?;
    let actual = listener.local_addr()?;
    let state = EngineWeb {
        engine,
        browser_token: format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple()),
        shutdown: CancellationToken::new(),
    };
    let shutdown = state.shutdown.clone();
    eprintln!(
        "ngn {} · Python engine gateway\nOpen http://{actual}",
        env!("CARGO_PKG_VERSION")
    );
    axum::serve(listener, router(state, actual.to_string()))
        .with_graceful_shutdown(async move {
            let _ = crate::shutdown_signal().await;
            shutdown.cancel();
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use axum::{
        body::{Body, to_bytes},
        http::{HeaderMap, Method, Request},
    };
    use serde_json::Value;
    use tower::ServiceExt;

    use super::*;

    const ENGINE_TOKEN: &str = "engine_secret";
    const BROWSER_TOKEN: &str = "browser_only";
    const AUTHORITY: &str = "127.0.0.1:8765";
    const ORIGIN: &str = "http://127.0.0.1:8765";

    fn assert_engine_headers(headers: &HeaderMap) {
        assert_eq!(
            headers.get("authorization").unwrap(),
            "Bearer engine_secret"
        );
        for name in ["origin", "x-ngn-token", "cookie", "x-forwarded-host"] {
            assert!(
                !headers.contains_key(name),
                "Forwarded forbidden header: {name}"
            );
        }
    }

    #[tokio::test]
    async fn browser_boundary_does_not_expose_or_forward_credentials() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let upstream = Router::new()
            .route("/api/v1/info", get(|headers: HeaderMap| async move {
                assert_engine_headers(&headers);
                Json(json!({
                    "protocol_version": 1, "protocol_revision": 0, "version": "test",
                    "capabilities": ["sessions"],
                }))
            }))
            .route("/api/v1/config", get(|headers: HeaderMap| async move {
                assert_engine_headers(&headers);
                Json(json!({"workspace": "/test", "provider": "demo", "model": "fixture", "read_only": true}))
            }))
            .route("/api/v1/sessions", post(|headers: HeaderMap, Json(input): Json<SessionCreate>| async move {
                assert_engine_headers(&headers);
                assert_eq!(input.title.as_deref(), Some("Example"));
                (StatusCode::CREATED, Json(json!({
                    "id": "session", "title": "Example", "updated_at_ms": 0, "archived": false,
                })))
            }));
        let server = tokio::spawn(async move { axum::serve(listener, upstream).await.unwrap() });
        let state = EngineWeb {
            engine: Client::connect(&url, ENGINE_TOKEN).await.unwrap(),
            browser_token: BROWSER_TOKEN.into(),
            shutdown: CancellationToken::new(),
        };
        let app = router(state, AUTHORITY.into());
        let request = |method: Method,
                       path: &str,
                       host: &str,
                       origin: &str,
                       token: &str,
                       body: &'static str| {
            let request = Request::builder()
                .method(method)
                .uri(path)
                .header("host", host)
                .header("origin", origin)
                .header("x-ngn-token", token)
                .header("content-type", "application/json")
                .header("x-forwarded-host", "attacker.invalid")
                .header("authorization", "Bearer browser_supplied")
                .header("cookie", "browser=value")
                .body(Body::from(body))
                .unwrap();
            app.clone().oneshot(request)
        };
        let boot = request(Method::GET, "/api/bootstrap", AUTHORITY, ORIGIN, "", "")
            .await
            .unwrap();
        assert_eq!(boot.status(), StatusCode::OK);
        assert_eq!(boot.headers()["cache-control"], "no-store");
        assert_eq!(boot.headers()["x-frame-options"], "DENY");
        let bytes = to_bytes(boot.into_body(), 8192).await.unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains(ENGINE_TOKEN));
        let boot: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(boot["token"], BROWSER_TOKEN);
        assert_eq!(boot["transport"], "engine-v1");

        for (host, origin, token) in [
            ("attacker.invalid", ORIGIN, BROWSER_TOKEN),
            (AUTHORITY, "http://attacker.invalid", BROWSER_TOKEN),
            (AUTHORITY, ORIGIN, ENGINE_TOKEN),
        ] {
            let response = request(Method::POST, "/api/v1/sessions", host, origin, token, "{}")
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::FORBIDDEN);
        }
        let invalid = request(
            Method::POST,
            "/api/v1/sessions",
            AUTHORITY,
            ORIGIN,
            BROWSER_TOKEN,
            r#"{"workspace":"/other"}"#,
        )
        .await
        .unwrap();
        assert_eq!(invalid.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let created = request(
            Method::POST,
            "/api/v1/sessions",
            AUTHORITY,
            ORIGIN,
            BROWSER_TOKEN,
            r#"{"title":"Example"}"#,
        )
        .await
        .unwrap();
        assert_eq!(created.status(), StatusCode::CREATED);
        let unlisted = request(
            Method::GET,
            "/api/v1/experimental/anything",
            AUTHORITY,
            ORIGIN,
            BROWSER_TOKEN,
            "",
        )
        .await
        .unwrap();
        assert_eq!(unlisted.status(), StatusCode::NOT_FOUND);
        server.abort();
    }
}
