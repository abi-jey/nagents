use axum::{
    Json, Router,
    extract::{
        Query, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use ngn_api::{
    Client, EVENTS_PATH, EVENTS_SUBPROTOCOL, Error, PROTOCOL_VERSION,
    models::{EngineEvent, RunMode, RunStart, RunStatus},
};
use serde::Deserialize;
use serde_json::json;
use tokio::{net::TcpListener, task::JoinHandle};

const TOKEN: &str = "test_token-1";
const REQUEST_ID: &str = "00000000-0000-4000-8000-000000000001";

struct Server {
    url: String,
    task: JoinHandle<()>,
}

impl Server {
    async fn start(router: Router) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        Self { url, task }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn authorized(headers: &HeaderMap) -> bool {
    headers
        .get("authorization")
        .is_some_and(|value| value == &format!("Bearer {TOKEN}"))
}

fn info_router(version: u64) -> Router {
    Router::new().route(
        "/api/v1/info",
        get(move |headers: HeaderMap| async move {
            if !authorized(&headers) {
                return StatusCode::UNAUTHORIZED.into_response();
            }
            Json(json!({
                "protocol_version": version,
                "protocol_revision": 99,
                "version": "test",
                "capabilities": ["new-in-minor-release"]
            }))
            .into_response()
        }),
    )
}

#[tokio::test]
async fn connect_requires_the_right_credentials_and_major_version() {
    let compatible = Server::start(info_router(PROTOCOL_VERSION)).await;
    assert!(Client::connect(&compatible.url, TOKEN).await.is_ok());
    assert!(matches!(
        Client::connect(&compatible.url, "other_token").await,
        Err(Error::Http(StatusCode::UNAUTHORIZED, _))
    ));
    let other = Server::start(info_router(PROTOCOL_VERSION + 1)).await;
    match Client::connect(&other.url, TOKEN).await {
        Err(Error::Incompatible { client, engine }) => {
            assert_eq!(client, PROTOCOL_VERSION);
            assert_eq!(engine, PROTOCOL_VERSION + 1);
        }
        _ => panic!("Engine with a different major version was not rejected"),
    }
}

#[tokio::test]
async fn generated_method_sends_the_path_bearer_and_request_body() {
    let app = info_router(PROTOCOL_VERSION).route(
        "/api/v1/sessions/session-1/runs",
        post(
            |headers: HeaderMap, Json(body): Json<RunStart>| async move {
                assert!(authorized(&headers));
                assert_eq!(body.mode, Some(RunMode::Queue));
                assert_eq!(body.request_id, REQUEST_ID);
                assert_eq!(body.prompt, "Hello");
                assert_eq!(body.interactive, None);
                (
                    StatusCode::ACCEPTED,
                    Json(json!({
                        "request_id": REQUEST_ID,
                        "session_id": "session-1",
                        "run_id": null,
                        "status": "queued",
                    })),
                )
            },
        ),
    );
    let server = Server::start(app).await;
    let client = Client::connect(&server.url, TOKEN).await.unwrap();
    let receipt = client
        .start_run(
            "session-1",
            &RunStart {
                request_id: REQUEST_ID.into(),
                prompt: "Hello".into(),
                mode: Some(RunMode::Queue),
                interactive: None,
            },
        )
        .await
        .unwrap();
    assert_eq!(receipt.status, RunStatus::Queued);
    assert!(receipt.run_id.is_none());
}

#[derive(Deserialize)]
struct Subscription {
    after: u64,
    session_id: String,
}

async fn events(
    headers: HeaderMap,
    Query(query): Query<Subscription>,
    upgrade: WebSocketUpgrade,
) -> Response {
    assert!(authorized(&headers));
    assert_eq!(query.after, 50);
    assert_eq!(query.session_id, "session-1");
    upgrade
        .protocols([EVENTS_SUBPROTOCOL])
        .on_upgrade(|mut socket: WebSocket| async move {
            for version in [PROTOCOL_VERSION, PROTOCOL_VERSION + 1] {
                let frame = json!({
                    "schema_version": version,
                    "sequence": 51,
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "timestamp_ms": 1_735_689_600_000_u64,
                    "event": {"type": "future.event", "message": "From a new minor version"}
                });
                socket
                    .send(Message::Text(frame.to_string().into()))
                    .await
                    .unwrap();
            }
        })
}

#[tokio::test]
async fn websocket_authenticates_resumes_and_checks_schema_major() {
    let server = Server::start(info_router(PROTOCOL_VERSION).route(EVENTS_PATH, get(events))).await;
    let client = Client::connect(&server.url, TOKEN).await.unwrap();
    let mut stream = client.subscribe(50, Some("session-1")).await.unwrap();
    let read = async {
        let first = stream.next().await.unwrap().unwrap();
        assert_eq!(first.sequence, 51);
        assert!(matches!(first.event, EngineEvent::Unknown(tag, _) if tag == "future.event"));
        assert!(matches!(
            stream.next().await,
            Err(Error::Incompatible { client, engine })
                if client == PROTOCOL_VERSION && engine == PROTOCOL_VERSION + 1
        ));
    };
    tokio::time::timeout(std::time::Duration::from_secs(5), read)
        .await
        .unwrap();
}
