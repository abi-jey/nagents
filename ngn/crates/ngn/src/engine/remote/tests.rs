use axum::{
    Json, Router,
    extract::{
        Query, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    http::HeaderMap,
    routing::{get, post},
};
use futures_util::StreamExt;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicU32, Ordering},
    },
};
use tokio::{net::TcpListener, task::JoinHandle};

use super::*;

const REQUEST: &str = "7c5c4096-e4a6-4373-ae78-f34410225b3e";
const ABSENT: &str = "33928bf0-a224-450a-9c13-b98435c9985c";
const TOKEN: &str = "engine-test-token";

struct Fixture {
    origin: String,
    task: JoinHandle<()>,
}

impl Fixture {
    async fn start(routes: Router, tasks: bool) -> Self {
        let routes = routes
            .route(
                "/api/v1/info",
                get(move |headers: HeaderMap| async move {
                    assert_eq!(headers["authorization"], format!("Bearer {TOKEN}"));
                    Json(json!({
                        "protocol_version": 1, "protocol_revision": 0, "version": "fixture",
                        "capabilities": if tasks { vec!["tasks"] } else { vec![] }
                    }))
                }),
            )
            .route(
                "/api/v1/config",
                get(|| async {
                    Json(json!({
                        "workspace": "/engine/workspace", "provider": "default",
                        "model": null, "read_only": true
                    }))
                }),
            )
            .route(
                "/api/v1/providers",
                get(|| async { Json(json!([{"id": "default", "kind": "test-provider"}])) }),
            );
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let task = tokio::spawn(async move { axum::serve(listener, routes).await.unwrap() });
        Self { origin, task }
    }

    async fn engine(&self) -> Remote {
        let client = Client::connect(&self.origin, TOKEN).await.unwrap();
        Remote::new(client).await.unwrap()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn session(id: &str, stamp: u64, archived: bool) -> Value {
    json!({"id": id, "title": id, "updated_at_ms": stamp, "archived": archived})
}

fn receipt(id: &str) -> Value {
    json!({
        "request_id": id, "session_id": "session", "status": "queued", "run_id": null
    })
}

fn running(id: &str, session: &str) -> Value {
    json!({"id": id, "session_id": session, "request_id": REQUEST, "status": "running"})
}

#[tokio::test]
async fn snapshots_and_actions_use_the_exact_wire_identities() {
    let fixture = Fixture::start(
        Router::new()
            .route(
                "/api/v1/sessions",
                get(|| async {
                    Json(json!([
                        session("archive", 3, true),
                        session("first", 1, false),
                        session("session", 2, false),
                    ]))
                }),
            )
            .route(
                "/api/v1/sessions/session/queue",
                get(|| async {
                    Json(json!({
                        "session_id": "session", "paused": true, "limit": 32,
                        "entries": [{"prompt": "keep this", "request_id": REQUEST, "run_id": null}]
                    }))
                }),
            )
            .route(
                "/api/v1/sessions/session/runs",
                post(|Json(body): Json<Value>| async move {
                    assert_eq!(body["request_id"], REQUEST);
                    assert_eq!(body["prompt"], "keep this");
                    assert_eq!(body["interactive"], true);
                    if body["mode"] == "idle" {
                        return (
                            StatusCode::CONFLICT,
                            Json(json!({
                                "detail": "This session is busy or paused. Queue the input or try again when idle."
                            })),
                        );
                    }
                    assert_eq!(body["mode"], "queue");
                    (StatusCode::ACCEPTED, Json(receipt(REQUEST)))
                }),
            )
            .route(
                "/api/v1/sessions/session/receipts",
                post(|Json(body): Json<Value>| async move {
                    if body["request_id"] == ABSENT {
                        return (StatusCode::OK, Json(Value::Null));
                    }
                    assert_eq!(body["request_id"], REQUEST);
                    if body["prompt"] == "keep this" {
                        (StatusCode::OK, Json(receipt(REQUEST)))
                    } else {
                        (StatusCode::CONFLICT, Json(json!({"detail":"different submission"})))
                    }
                }),
            )
            .route(
                "/api/v1/state",
                get(|| async {
                    Json(json!({
                        "runs": [running("foreign", "foreign-session"), running("run", "session")],
                        "approvals": [{
                            "id":"approval", "session_id":"child-session", "root_session_id":"session", "run_id":"run",
                            "call":{"call_id":"exact-call", "name":"edit","arguments":{"path":"note.txt"}},
                            "depth":1, "preview":"preview", "agent_title":"Child"
                        }]
                    }))
                }),
            )
            .route(
                "/api/v1/approvals/approval",
                post(|Json(body): Json<Value>| async move {
                    assert_eq!(body, json!({"run_id":"run", "call_id":"exact-call", "allow":false}));
                    Json(json!({"approval_id":"approval", "allowed":false}))
                }),
            )
            .route(
                "/api/v1/runs/run/steer",
                get(|| async {
                    Json(json!({
                        "run_id":"run", "session_id":"session", "run_status":"running",
                        "supported":true, "accepting":true, "receipts":[]
                    }))
                }).post(|Json(body): Json<Value>| async move {
                    assert_eq!(body, json!({"prompt":"steer", "request_id":REQUEST}));
                    Json(json!({
                        "input_id":"input", "run_id":"run", "session_id":"session", "position":1,
                        "request_id":REQUEST, "status":"persisted-future-status", "prompt":"steer"
                    }))
                }),
            )
            .route(
                "/api/v1/sessions/session/tasks",
                get(|| async {
                    Json(json!([{
                        "run_id":"child", "session_id":"child-session", "parent_run_id":"run",
                        "parent_session_id":"session", "root_run_id":"run", "root_session_id":"session",
                        "call_id":"delegate", "title":"Child", "depth":1,"read_only":true,"status":"running"
                    }, {
                        "run_id":"foreign-child", "session_id":"foreign-child-session", "parent_run_id":"other",
                        "parent_session_id":"other-session", "root_run_id":"other", "root_session_id":"other-session",
                        "call_id":"delegate", "title":"Foreign child", "depth":1,"read_only":true,"status":"running"
                    }]))
                }),
            ),
        true,
    )
    .await;
    let client = fixture.engine().await;
    assert!(matches!(
        &client.display().provider,
        ProviderDisplay::Configured { kind, model: None, auth: None, .. } if kind == "test-provider"
    ));
    assert!(client.display().read_only);
    assert_eq!(
        client
            .open_session(SessionSelection::LatestOrNew)
            .await
            .unwrap(),
        "session"
    );

    let queue = client.queue("session").await.unwrap();
    assert!(queue.paused);
    assert!(queue.entries[0].run_id.is_none());
    assert_eq!(queue.entries[0].label(), format!("request {REQUEST}"));
    assert!(matches!(
        client
            .submit("session", "keep this", REQUEST, Submission::Idle)
            .await,
        InputAdmission::Rejected {
            idle_changed: true,
            ..
        }
    ));
    let InputAdmission::Accepted {
        receipt,
        warning: None,
    } = client
        .submit("session", "keep this", REQUEST, Submission::Queue)
        .await
    else {
        panic!("expected a genuine queue receipt")
    };
    assert!(receipt.run_id.is_none());
    assert_eq!(receipt.status, Some(RunStatus::Queued));
    assert!(
        client
            .input_receipt("session", "keep this", REQUEST)
            .await
            .unwrap()
            .is_some()
    );
    assert!(
        client
            .input_receipt("session", "unknown input", ABSENT)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        client
            .input_receipt("session", "different prompt", REQUEST)
            .await
            .is_err()
    );

    let activity = client.conversation_activity("session").await.unwrap();
    assert_eq!(activity.active.unwrap().id, "run");
    let approval = &activity.approvals[0];
    assert_eq!(approval.session_id, "child-session");
    client
        .decide(
            &approval.run_id,
            &approval.id,
            &approval.call.call_id,
            false,
        )
        .await
        .unwrap();
    assert!(client.steering("run").await.unwrap().accepting);
    assert!(matches!(
        client.steer("run", "steer", REQUEST).await.unwrap(),
        SteeringStatus::Unknown
    ));
    let tasks = client.tasks("session").await.unwrap().unwrap();
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].run_id, "child");
    assert!(client.log_paths().is_none());
    client.shutdown().await;
    assert!(client.queue("session").await.unwrap().paused);
}

fn event(sequence: u64, payload: Value) -> Value {
    json!({
        "schema_version": 1, "sequence": sequence, "timestamp_ms": 100,
        "run_id": "run", "session_id": "session", "event": payload
    })
}

#[tokio::test]
async fn unknown_history_still_advances_and_unadvertised_tasks_are_not_polled() {
    let fixture = Fixture::start(
        Router::new().route(
            "/api/v1/sessions/session/history",
            get(|Query(query): Query<HashMap<String, String>>| async move {
                assert_eq!(query.get("limit").unwrap(), "256");
                Json(match query["after"].as_str() {
                    "0" => json!({
                        "cursor": 2, "has_more": true,
                        "events": [event(2, json!({"type":"future.event","extension":{"hidden":true}}))]
                    }),
                    "2" => json!({
                        "cursor": 6, "has_more": false,
                        "events": [event(6, json!({"type":"text.completed","text":"persisted"}))]
                    }),
                    value => panic!("wrong cursor {value}"),
                })
            }),
        ),
        false,
    )
    .await;
    let client = fixture.engine().await;
    let first = client.history("session", 0).await.unwrap();
    assert!(first.records.is_empty());
    assert_eq!(first.cursor, 2);
    assert!(first.has_more);
    let next = client.history("session", first.cursor).await.unwrap();
    assert_eq!(next.records[0].sequence, 6);
    assert!(matches!(&next.records[0].event, Event::TextCompleted { text } if text == "persisted"));
    assert!(!next.has_more);
    // No fixture endpoint was installed; a mistaken refresh would get a 404.
    assert!(client.tasks("session").await.unwrap().is_none());
}

async fn send(socket: &mut WebSocket, sequence: u64, payload: Value) {
    socket
        .send(Message::Text(event(sequence, payload).to_string().into()))
        .await
        .unwrap();
}

#[tokio::test]
async fn one_socket_reconnects_after_the_last_live_sequence_and_deduplicates() {
    let fixture = Fixture::start(
        Router::new().route(
            ngn_api::EVENTS_PATH,
            get(
                |Query(query): Query<HashMap<String, String>>, ws: WebSocketUpgrade| async move {
                    let after = query["after"].clone();
                    assert!(!query.contains_key("session_id"));
                    ws.protocols([ngn_api::EVENTS_SUBPROTOCOL]).on_upgrade(
                        move |mut socket| async move {
                            match after.as_str() {
                                "0" => {
                                    send(
                                        &mut socket,
                                        2,
                                        json!({"type":"run.started","prompt":"start"}),
                                    )
                                    .await;
                                    send(&mut socket, 3, json!({"type":"future.event"})).await;
                                    send(
                                        &mut socket,
                                        4,
                                        json!({"type":"text.delta","text":"live"}),
                                    )
                                    .await;
                                    socket.send(Message::Close(None)).await.unwrap();
                                }
                                "4" => {
                                    send(
                                        &mut socket,
                                        4,
                                        json!({"type":"text.delta","text":"duplicate"}),
                                    )
                                    .await;
                                    send(
                                        &mut socket,
                                        6,
                                        json!({"type":"text.completed","text":"persisted"}),
                                    )
                                    .await;
                                }
                                value => panic!("wrong reconnect cursor {value}"),
                            }
                        },
                    )
                },
            ),
        ),
        false,
    )
    .await;
    let client = fixture.engine().await;
    let mut stream = std::pin::pin!(client.subscribe().await.unwrap());
    tokio::time::timeout(Duration::from_secs(3), async {
        assert!(matches!(
            stream.next().await,
            Some(Ok(LiveUpdate {
                delivery: Delivery::Durable,
                record: EventRecord { sequence: 2, .. }
            }))
        ));
        assert!(matches!(
            stream.next().await,
            Some(Ok(LiveUpdate {
                delivery: Delivery::Transient,
                record: EventRecord { sequence: 4, .. }
            }))
        ));
        assert!(matches!(
            stream.next().await,
            Some(Err(FeedNotice::Reconnecting))
        ));
        assert!(matches!(
            stream.next().await,
            Some(Err(FeedNotice::ResyncRequired))
        ));
        assert!(matches!(
            stream.next().await,
            Some(Ok(LiveUpdate {
                delivery: Delivery::Durable,
                record: EventRecord { sequence: 6, .. }
            }))
        ));
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn reconnect_stops_when_handshakes_do_not_restore_delivery() {
    let connections = Arc::new(AtomicU32::new(0));
    let observed = connections.clone();
    let fixture = Fixture::start(
        Router::new().route(
            ngn_api::EVENTS_PATH,
            get(move |ws: WebSocketUpgrade| {
                let observed = observed.clone();
                async move {
                    observed.fetch_add(1, Ordering::Relaxed);
                    ws.protocols([ngn_api::EVENTS_SUBPROTOCOL]).on_upgrade(
                        |mut socket| async move {
                            let _ = socket.send(Message::Close(None)).await;
                        },
                    )
                }
            }),
        ),
        false,
    )
    .await;
    let client = fixture.engine().await;
    let mut stream = std::pin::pin!(client.subscribe().await.unwrap());
    tokio::time::timeout(Duration::from_secs(8), async {
        for _ in 0..RECONNECT_ATTEMPTS {
            assert!(matches!(
                stream.next().await,
                Some(Err(FeedNotice::Reconnecting))
            ));
            assert!(matches!(
                stream.next().await,
                Some(Err(FeedNotice::ResyncRequired))
            ));
        }
        assert!(matches!(
            stream.next().await,
            Some(Err(FeedNotice::Reconnecting))
        ));
        assert!(matches!(
            stream.next().await,
            Some(Err(FeedNotice::Disconnected(_)))
        ));
        assert!(stream.next().await.is_none());
    })
    .await
    .unwrap();
    assert_eq!(connections.load(Ordering::Relaxed), RECONNECT_ATTEMPTS + 1);
}

#[test]
fn admission_errors_do_not_claim_an_ambiguous_request_was_rejected() {
    for status in [
        StatusCode::REQUEST_TIMEOUT,
        StatusCode::BAD_GATEWAY,
        StatusCode::SERVICE_UNAVAILABLE,
    ] {
        assert!(matches!(
            classify_rejection(
                Error::Http(status, "lost response".into()),
                Submission::Queue
            ),
            InputAdmission::Unconfirmed(_)
        ));
    }
}
