//! The terminal client's boundary to the Python engine. The engine owns runtime
//! policy, tools and persistence; the TUI consumes snapshots and events.

#[cfg(test)]
pub(crate) mod fake;
mod remote;

use std::{
    borrow::Cow,
    future::Future,
    path::{Path, PathBuf},
    time::Duration,
};

use anyhow::Result;
use futures_util::Stream;

use crate::model::{EventRecord, ProviderAuth, RunStatus, TaskInfo};

pub(crate) use remote::Remote;

// Passive snapshots only. No persistence or runtime handles cross the Engine
// interface.
pub(crate) use crate::model::{
    Approval, ChildTextPhase, ChildToolDispatch, QueueAction, Run, Session,
};

#[derive(Default)]
pub(crate) struct ClientDisplay {
    pub(crate) workspace: PathBuf,
    pub(crate) read_only: bool,
    pub(crate) provider: ProviderDisplay,
    pub(crate) animations: Option<bool>,
    /// The host's shell deadline, shown beside each shell call's elapsed time.
    pub(crate) shell_timeout_seconds: Option<u64>,
}

#[derive(Default)]
pub(crate) enum ProviderDisplay {
    #[default]
    Unselected,
    Configured {
        id: String,
        kind: String,
        model: Option<String>,
        auth: Option<ProviderAuth>,
    },
}

pub(crate) struct LogPaths<'a> {
    pub(crate) runtime: Option<&'a Path>,
    pub(crate) wire: Option<&'a Path>,
}

pub(crate) enum SessionSelection<'a> {
    Resume(&'a str),
    LatestOrNew,
    New,
}

impl<'a> SessionSelection<'a> {
    pub(crate) fn initial(resume: Option<&'a str>, continue_session: bool) -> Self {
        match resume {
            Some(id) => Self::Resume(id),
            None if continue_session => Self::LatestOrNew,
            None => Self::New,
        }
    }
}

pub(crate) struct HistoryPage {
    pub(crate) records: Vec<EventRecord>,
    /// The durable watermark, including events this client does not recognize.
    pub(crate) cursor: i64,
    pub(crate) has_more: bool,
}

pub(crate) struct ConversationActivity {
    pub(crate) active: Option<Run>,
    pub(crate) child_text: Vec<ChildTextPhase>,
    /// Pending decisions in this workspace. The UI matches exact identities.
    pub(crate) approvals: Vec<Approval>,
}

#[derive(Clone, Copy)]
pub(crate) enum Delivery {
    /// A committed event from the ordered remote feed.
    Durable,
    /// Live-only data that must not advance the durable cursor.
    Transient,
}

pub(crate) struct LiveUpdate {
    pub(crate) record: EventRecord,
    pub(crate) delivery: Delivery,
}

pub(crate) enum FeedNotice {
    /// Retain the durable cursor and replay; do not infer phase changes.
    ResyncRequired,
    Reconnecting,
    Disconnected(String),
}

pub(crate) type Update = Result<LiveUpdate, FeedNotice>;

pub(crate) struct InputReceipt {
    pub(crate) run_id: Option<String>,
    pub(crate) request_id: String,
    pub(crate) status: Option<RunStatus>,
}

impl InputReceipt {
    pub(crate) fn label(&self) -> Cow<'_, str> {
        input_label(self.run_id.as_deref(), &self.request_id)
    }
}

#[derive(Default)]
pub(crate) struct QueueState {
    pub(crate) paused: bool,
    pub(crate) entries: Vec<QueueEntry>,
}

pub(crate) struct QueueEntry {
    pub(crate) prompt: String,
    pub(crate) request_id: String,
    pub(crate) run_id: Option<String>,
}

impl QueueEntry {
    pub(crate) fn label(&self) -> Cow<'_, str> {
        input_label(self.run_id.as_deref(), &self.request_id)
    }
}

fn input_label<'a>(run_id: Option<&'a str>, request_id: &str) -> Cow<'a, str> {
    match run_id {
        Some(id) => Cow::Borrowed(id),
        None => Cow::Owned(format!("request {request_id}")),
    }
}

pub(crate) struct SteeringState {
    pub(crate) run_id: String,
    pub(crate) session_id: String,
    pub(crate) supported: bool,
    pub(crate) accepting: bool,
    pub(crate) receipts: Vec<SteeringReceipt>,
}

pub(crate) struct SteeringReceipt {
    pub(crate) input_id: String,
    pub(crate) run_id: String,
    pub(crate) prompt: String,
    pub(crate) position: i64,
    pub(crate) status: SteeringStatus,
}

#[derive(Clone, Copy)]
pub(crate) enum SteeringStatus {
    Pending,
    Offered,
    Applied,
    Uncertain,
    Undelivered,
    Unknown,
}

#[derive(Clone, Copy)]
pub(crate) enum Submission {
    /// The displayed turn was idle. Refuse if a root took over before
    /// admission, rather than silently converting this to an enqueue.
    Idle,
    Queue,
}

/// A durable admission can exist even if dispatch failed. An unknown outcome
/// must retain the original request identity for verification or retry.
pub(crate) enum InputAdmission {
    Accepted {
        receipt: InputReceipt,
        warning: Option<anyhow::Error>,
    },
    Rejected {
        error: anyhow::Error,
        idle_changed: bool,
    },
    Unconfirmed(anyhow::Error),
}

/// Exact private child identity (session, run).
pub(crate) type TaskIdentity = (String, String);

pub(crate) trait Engine {
    fn display(&self) -> &ClientDisplay;
    fn log_paths(&self) -> Option<LogPaths<'_>>;
    fn refresh_interval(&self) -> Duration {
        Duration::from_millis(100)
    }

    /// Subscribe before loading the initial durable cursor to close the race
    /// between replay and live delivery. The feed is bounded.
    async fn subscribe(&self) -> Result<impl Stream<Item = Update> + Send>;
    async fn open_session(&self, selection: SessionSelection<'_>) -> Result<String>;
    async fn sessions(&self) -> Result<Vec<Session>>;
    /// Bounded committed records in ascending sequence order, exclusive of
    /// `after`. A continuation page must advance the cursor.
    async fn history(&self, session: &str, after: i64) -> Result<HistoryPage>;

    async fn conversation_activity(&self, session: &str) -> Result<ConversationActivity>;
    async fn child_tool_dispatches(&self) -> Result<Vec<ChildToolDispatch>>;
    /// `None` means this backend does not expose task topology.
    async fn tasks(&self, session: &str) -> Result<Option<Vec<TaskInfo>>>;

    /// Optional short, ordered tails by exact child identity, in request order.
    /// An owned future lets the UI load them without holding the engine or
    /// blocking input and drawing on a best-effort motion hint.
    fn task_activity_tail(
        &self,
        targets: Vec<TaskIdentity>,
    ) -> impl Future<Output = Result<Vec<Vec<EventRecord>>>> + Send + 'static;

    async fn submit(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
        submission: Submission,
    ) -> InputAdmission;
    async fn input_receipt(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
    ) -> Result<Option<InputReceipt>>;
    async fn queue(&self, session: &str) -> Result<QueueState>;
    async fn control_queue(&self, session: &str, action: QueueAction) -> Result<()>;
    async fn steering(&self, run: &str) -> Result<SteeringState>;
    async fn steer(&self, run: &str, prompt: &str, request: &str) -> Result<SteeringStatus>;
    async fn decide(&self, run: &str, approval: &str, call: &str, allow: bool) -> Result<()>;
    async fn cancel(&self, run: &str) -> Result<()>;

    /// Idempotently close the client. Only an owned engine should be stopped;
    /// an attached engine can keep serving other clients.
    async fn shutdown(&self);
}
