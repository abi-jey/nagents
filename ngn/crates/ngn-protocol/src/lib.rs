//! Agent Runtime Protocol v1. NDJSON on private stdin/stdout, never a public socket.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const VERSION: u16 = 1;
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;

#[derive(Debug, Default, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProviderAuth {
    #[default]
    ApiKey,
    Chatgpt,
    /// The local Codex CLI login (`$CODEX_HOME` or `~/.codex`), as the Python ngn used.
    Codex,
}

impl ProviderAuth {
    fn is_api_key(&self) -> bool {
        *self == Self::ApiKey
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub parameters: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ToolCall {
    pub call_id: String,
    pub name: String,
    pub arguments: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ToolResult {
    pub call_id: String,
    pub name: String,
    pub result: Value,
    pub error: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    pub kind: String,
    pub model: String,
    #[serde(default)]
    pub api: String,
    #[serde(default)]
    pub base_url: String,
    #[serde(default)]
    pub api_key_env: String,
    /// Omitted for API keys so existing v1 runtimes receive the original wire shape.
    #[serde(default, skip_serializing_if = "ProviderAuth::is_api_key")]
    pub auth: ProviderAuth,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartRun {
    pub session_id: String,
    pub prompt: String,
    pub system_prompt: String,
    /// Opaque runtime-owned storage. The host never opens the nagents schema.
    pub database: String,
    /// Host-generated absolute path, only sent after chatgpt_auth negotiation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth_store: Option<String>,
    pub provider: ProviderConfig,
    pub tools: Vec<ToolDefinition>,
    pub max_tool_rounds: u32,
    /// Host acknowledgment, sent only after the runtime advertises safe_steering.
    #[serde(default, skip_serializing_if = "disabled")]
    pub steering: bool,
}

fn disabled(enabled: &bool) -> bool {
    !enabled
}

/// Optional v1 extension: runtime capability AND explicit host start acknowledgment.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SteeringBoundary {
    AfterTools,
    Final,
}

/// Only the host can create input identities, ordering and attribution.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SteeringInput {
    pub input_id: String,
    pub request_id: String,
    pub position: i64,
    pub prompt: String,
}

fn steering_input<'de, D>(deserializer: D) -> Result<Option<SteeringInput>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Option::<SteeringInput>::deserialize(deserializer)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum HostMessage {
    Start {
        run: StartRun,
    },
    ToolResult {
        result: ToolResult,
    },
    SteeringOffer {
        boundary_id: String,
        #[serde(deserialize_with = "steering_input")]
        input: Option<SteeringInput>,
    },
    Cancel,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostFrame {
    pub protocol_version: u16,
    pub run_id: String,
    pub message: HostMessage,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum RuntimeMessage {
    Ready {
        runtime: String,
        capabilities: Vec<String>,
    },
    TextDelta {
        text: String,
    },
    TextCompleted {
        text: String,
    },
    ToolRequest {
        call: ToolCall,
    },
    Notice {
        text: String,
    },
    SteeringBoundary {
        boundary_id: String,
        kind: SteeringBoundary,
    },
    SteeringApplied {
        boundary_id: String,
        input_id: String,
    },
    Finished {
        status: RunStatus,
        error: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeFrame {
    pub protocol_version: u16,
    /// Empty only for the initial ready handshake.
    pub run_id: String,
    pub message: RuntimeMessage,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    /// Host-owned pending input, not an executing runtime status.
    Queued,
    Running,
    Completed,
    Failed,
    Cancelled,
    Interrupted,
}

/// Host-owned topology. An agent/runtime cannot supply or change these identities.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TaskInfo {
    pub run_id: String,
    pub session_id: String,
    pub parent_run_id: String,
    pub parent_session_id: String,
    pub root_run_id: String,
    pub root_session_id: String,
    pub call_id: String,
    pub title: String,
    pub depth: u8,
    pub read_only: bool,
    pub status: RunStatus,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum Event {
    RunQueued {
        prompt: String,
    },
    /// Session-scoped control event; EventRecord.run_id is empty.
    QueueUpdated {
        paused: bool,
    },
    RunStarted {
        prompt: String,
    },
    /// Host-only snapshot invalidation. Empty input_id means capability/admission
    /// changed; otherwise inspect the exact run's durable steering receipts.
    SteeringUpdated {
        input_id: String,
    },
    TextDelta {
        text: String,
    },
    TextCompleted {
        text: String,
    },
    ToolStarted {
        call: ToolCall,
    },
    ToolCompleted {
        result: ToolResult,
        duration_ms: u64,
    },
    /// Host-owned active time; excludes this exact call's approval waiting.
    ToolApprovalTiming {
        call_id: String,
        elapsed_ms: u64,
        waiting: bool,
    },
    ApprovalRequested {
        approval_id: String,
        call: ToolCall,
        preview: String,
    },
    ApprovalResolved {
        approval_id: String,
        allowed: bool,
    },
    Notice {
        text: String,
    },
    TaskStarted {
        task: TaskInfo,
    },
    TaskUpdated {
        task: TaskInfo,
    },
    TaskFinished {
        task: TaskInfo,
        error: String,
    },
    RunFinished {
        status: RunStatus,
        error: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventRecord {
    pub schema_version: u16,
    /// Durable, host-assigned ordering; never supplied by a runtime or model.
    pub sequence: i64,
    /// Empty for session-scoped queue controls; otherwise an exact run identity.
    pub run_id: String,
    pub session_id: String,
    pub timestamp_ms: u64,
    #[serde(flatten)]
    pub event: Event,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn runtime_frames_round_trip() {
        let source = json!({"protocol_version": 1, "run_id": "run-123", "message": {"type": "tool_request",
            "call": {"call_id": "call-123", "name": "read_file", "arguments": {"path": "README.md"}}}});
        let frame: RuntimeFrame = serde_json::from_value(source.clone()).unwrap();
        assert_eq!(serde_json::to_value(frame).unwrap(), source);
    }

    #[test]
    fn unknown_tools_fields_are_not_accepted() {
        assert!(
            serde_json::from_value::<ToolCall>(json!({"call_id":"c", "name":"shell",
            "arguments":{}, "approved":true}))
            .is_err()
        );
    }

    #[test]
    fn host_messages_round_trip() {
        let frame = HostFrame {
            protocol_version: VERSION,
            run_id: "run-1".into(),
            message: HostMessage::Cancel,
        };
        let encoded = serde_json::to_vec(&frame).unwrap();
        assert!(serde_json::from_slice::<HostFrame>(&encoded).is_ok());
    }

    #[test]
    fn api_key_wire_remains_identical_and_auth_is_opt_in() {
        let source = json!({"session_id":"s", "prompt":"hello", "system_prompt":"policy", "database":"/runtime.sqlite3",
            "provider":{"kind":"openai_compatible", "model":"test", "api":"auto", "base_url":"", "api_key_env":"TEST_KEY"},
            "tools":[], "max_tool_rounds":10});
        let run: StartRun = serde_json::from_value(source.clone()).unwrap();
        assert_eq!(run.provider.auth, ProviderAuth::ApiKey);
        assert!(run.auth_store.is_none());
        assert_eq!(serde_json::to_value(run).unwrap(), source);
        let mut subscription = source;
        subscription["provider"]["auth"] = json!("chatgpt");
        subscription["auth_store"] = json!("/private/auth/openai.json");
        let run: StartRun = serde_json::from_value(subscription.clone()).unwrap();
        assert_eq!(run.provider.auth, ProviderAuth::Chatgpt);
        assert_eq!(serde_json::to_value(run).unwrap(), subscription);
        subscription["steering"] = json!(true);
        let run: StartRun = serde_json::from_value(subscription.clone()).unwrap();
        assert!(run.steering);
        assert_eq!(run.auth_store.as_deref(), Some("/private/auth/openai.json"));
        assert_eq!(serde_json::to_value(run).unwrap(), subscription);
        subscription["provider"]["auth"] = json!("codex_home");
        assert!(serde_json::from_value::<StartRun>(subscription).is_err());
    }

    #[test]
    fn steering_extension_frames_are_typed_and_reject_extra_authority_fields() {
        let boundary = json!({"protocol_version":1, "run_id":"run", "message":{"type":"steering_boundary", "boundary_id":"boundary", "kind":"after_tools"}});
        assert!(serde_json::from_value::<RuntimeFrame>(boundary.clone()).is_ok());
        let mut forged = boundary;
        forged["message"]["request_id"] = json!("model-cannot-admit-input");
        assert!(serde_json::from_value::<RuntimeFrame>(forged).is_err());
        let mut offer = json!({"protocol_version":1, "run_id":"run", "message":{"type":"steering_offer", "boundary_id":"boundary", "input":{"input_id":"host", "request_id":"request", "position":1, "prompt":"user"}}});
        assert!(serde_json::from_value::<HostFrame>(offer.clone()).is_ok());
        offer["message"]["input"]["approved"] = json!(true);
        assert!(serde_json::from_value::<HostFrame>(offer).is_err());
        assert!(serde_json::from_value::<HostFrame>(json!({"protocol_version":1,"run_id":"run","message":{"type":"steering_offer","boundary_id":"boundary"}})).is_err());
        assert!(serde_json::from_value::<RuntimeFrame>(json!({"protocol_version":1,"run_id":"run","message":{"type":"steering_updated","input_id":"forged"}})).is_err());
    }

    #[test]
    fn steering_start_acknowledgment_is_strict_optional_and_omitted_when_disabled() {
        let baseline = json!({"session_id":"session", "prompt":"user", "system_prompt":"policy",
            "database":"/private/runtime.sqlite3", "provider":{"kind":"fixture", "model":"fixture", "api":"", "base_url":"", "api_key_env":"FIXTURE_KEY"}, "tools":[], "max_tool_rounds":10});
        let start: StartRun = serde_json::from_value(baseline.clone()).unwrap();
        assert!(!start.steering);
        assert_eq!(serde_json::to_value(&start).unwrap(), baseline);
        for enabled in [false, true] {
            let mut explicit = baseline.clone();
            explicit["steering"] = json!(enabled);
            let start: StartRun = serde_json::from_value(explicit.clone()).unwrap();
            assert_eq!(start.steering, enabled);
            assert_eq!(
                serde_json::to_value(start).unwrap(),
                if enabled { explicit } else { baseline.clone() }
            );
        }
        for invalid in [
            json!(null),
            json!(0),
            json!(1),
            json!("true"),
            json!([]),
            json!({}),
        ] {
            let mut value = baseline.clone();
            value["steering"] = invalid;
            assert!(serde_json::from_value::<StartRun>(value).is_err());
        }
        let mut unknown = baseline;
        unknown["steering_approved"] = json!(true);
        assert!(serde_json::from_value::<StartRun>(unknown).is_err());
    }
}
