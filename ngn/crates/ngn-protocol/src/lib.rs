//! Agent Runtime Protocol v1. NDJSON on private stdin/stdout, never a public socket.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const VERSION: u16 = 1;
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;

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
    pub api_key_env: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartRun {
    pub session_id: String,
    pub prompt: String,
    pub system_prompt: String,
    /// Opaque runtime-owned storage. The host never opens the nagents schema.
    pub database: String,
    pub provider: ProviderConfig,
    pub tools: Vec<ToolDefinition>,
    pub max_tool_rounds: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum HostMessage {
    Start { run: StartRun },
    ToolResult { result: ToolResult },
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
}
