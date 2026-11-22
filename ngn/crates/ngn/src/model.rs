//! The client's view model: display records and passive snapshots that the
//! Remote engine builds from the generated `ngn-api` client. Nothing here runs
//! tools, talks to providers or persists runtime state; the Python engine owns
//! all of that.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The bundle manifest's runtime protocol field. Installed clients still check
/// it during `ngn update`, so it stays fixed while the engine contract evolves.
pub const RUNTIME_PROTOCOL: u16 = 1;

/// Schema version of the display records built from engine events.
pub const VERSION: u16 = 1;

#[derive(Debug, Default, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProviderAuth {
    #[default]
    ApiKey,
    Chatgpt,
    /// The local Codex CLI login (`$CODEX_HOME` or `~/.codex`), as the Python ngn used.
    Codex,
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub id: String,
    pub title: String,
    pub updated_at_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Run {
    pub id: String,
    pub session_id: String,
    pub request_id: String,
    pub status: RunStatus,
}

#[derive(Debug, Clone, Serialize)]
pub struct Approval {
    pub id: String,
    pub run_id: String,
    pub session_id: String,
    pub root_session_id: String,
    pub agent_title: String,
    pub depth: u8,
    pub call: ToolCall,
    pub preview: String,
}

/// A live child's in-flight tool call, for agent activity rows.
#[derive(Debug, Clone)]
pub struct ChildToolDispatch {
    pub run_id: String,
    pub session_id: String,
    pub call_id: String,
}

/// Latest committed text-phase edge of an exact live child. No text is retained.
#[derive(Debug, Clone)]
pub struct ChildTextPhase {
    pub run_id: String,
    pub session_id: String,
    pub sequence: i64,
    pub responding: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum QueueAction {
    Pause,
    Resume,
    Clear,
}
