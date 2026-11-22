use ngn_api::{PROTOCOL_VERSION, models::*};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};

fn fixtures() -> Value {
    serde_json::from_str(include_str!(
        "../../../../contracts/engine/v1/fixtures.json"
    ))
    .unwrap()
}

fn roundtrip<T: DeserializeOwned + Serialize>(wire: &Value) {
    let typed = serde_json::from_value::<T>(wire.clone()).unwrap();
    assert_eq!(serde_json::to_value(typed).unwrap(), *wire);
}

#[test]
fn every_http_message_roundtrips() {
    let fixture = fixtures();
    let messages = fixture["messages"].as_object().unwrap();
    macro_rules! cases {
        ($($type:ident),+ $(,)?) => {{
            let tested = [$(stringify!($type)),+];
            assert_eq!(tested.len(), messages.len());
            $(roundtrip::<$type>(&messages[stringify!($type)]);)+
        }};
    }
    cases!(
        Approval,
        ApprovalDecision,
        ApprovalReceipt,
        EngineConfig,
        EngineInfo,
        EngineSnapshot,
        ErrorResponse,
        History,
        InputLookup,
        InputReceipt,
        Model,
        Provider,
        QueueEntry,
        QueueState,
        QueueUpdate,
        Run,
        RunStart,
        Session,
        SessionCreate,
        SessionFork,
        SessionRename,
        SteeringInput,
        SteeringReceipt,
        SteeringState,
        TaskInfo,
        ToolCall,
        ToolResult,
    );
}

#[test]
fn every_event_is_recognized_and_roundtrips() {
    for (index, body) in fixtures()["events"].as_array().unwrap().iter().enumerate() {
        let wire = json!({
            "schema_version": PROTOCOL_VERSION,
            "sequence": index + 1,
            "session_id": "session-1",
            "run_id": "run-1",
            "timestamp_ms": 1_735_689_600_000_u64,
            "event": body,
        });
        let typed = serde_json::from_value::<EventRecord>(wire.clone()).unwrap();
        assert!(!matches!(typed.event, EngineEvent::Unknown(..)), "{body}");
        assert_eq!(serde_json::to_value(typed).unwrap(), wire);
    }
}

#[test]
fn additive_fields_and_unknown_variants_are_forward_compatible() {
    let known = serde_json::from_value::<EngineEvent>(
        json!({"type": "text.delta", "text": "Hello", "new_field": [1, 2]}),
    )
    .unwrap();
    assert_eq!(
        known,
        EngineEvent::TextDelta(TextDelta {
            text: "Hello".into()
        })
    );
    let future = json!({"type": "future.event", "payload": {"a": [true, null]}});
    let typed = serde_json::from_value::<EngineEvent>(future.clone()).unwrap();
    assert_eq!(
        typed,
        EngineEvent::Unknown("future.event".into(), future.clone())
    );
    assert_eq!(serde_json::to_value(typed).unwrap(), future);
    let future_status = json!("suspended");
    let status = serde_json::from_value::<RunStatus>(future_status.clone()).unwrap();
    assert_eq!(status, RunStatus::Unknown("suspended".into()));
    assert_eq!(serde_json::to_value(status).unwrap(), future_status);
    // A known but invalid event isn't silently reclassified as an extension.
    assert!(serde_json::from_value::<EngineEvent>(json!({"type": "text.delta"})).is_err());
}

#[test]
fn missing_and_null_have_distinct_shapes() {
    let parse = |value| serde_json::from_value::<SessionFork>(value).unwrap().title;
    assert_eq!(parse(json!({})), None);
    assert_eq!(parse(json!({"title": null})), Some(None));
    assert_eq!(parse(json!({"title": "Copy"})), Some(Some("Copy".into())));
    roundtrip::<SessionFork>(&json!({}));
    roundtrip::<SessionFork>(&json!({"title": null}));
    roundtrip::<SessionFork>(&json!({"title": "Copy"}));
    assert!(serde_json::from_value::<SessionCreate>(json!({"title": null})).is_err());
    let mut config = fixtures()["messages"]["EngineConfig"].clone();
    config.as_object_mut().unwrap().remove("model");
    assert!(serde_json::from_value::<EngineConfig>(config).is_err());
}

#[test]
fn request_authority_cannot_be_silently_dropped() {
    assert!(
        serde_json::from_value::<ApprovalDecision>(json!({
            "run_id": "run-1", "call_id": "call-1", "allow": false, "unrecognized": true
        }))
        .is_err()
    );
}

#[test]
fn history_requires_the_matching_event_schema_version() {
    let mut history = fixtures()["messages"]["History"].clone();
    history["events"][0]["schema_version"] = json!(PROTOCOL_VERSION + 1);
    assert!(serde_json::from_value::<History>(history.clone()).is_err());
    history["events"][0]
        .as_object_mut()
        .unwrap()
        .remove("schema_version");
    assert!(serde_json::from_value::<History>(history).is_err());
}
