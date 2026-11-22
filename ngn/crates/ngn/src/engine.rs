//! The terminal client's boundary. The local adapter keeps runtime policy and
//! persistence in the existing host; the TUI consumes snapshots and events.

mod remote;

use std::{
    borrow::Cow,
    future::Future,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use anyhow::Result;
use futures_util::{Stream, stream};
use ngn_core::{
    ApprovalMode, Config, Host,
    host::{IdleTurnChanged, InputAdmission as HostAdmission},
    store::InputReceipt as HostReceipt,
};
use ngn_protocol::{EventRecord, ProviderAuth, RunStatus, TaskInfo};
use tokio::sync::broadcast::error::RecvError;

pub(crate) use remote::Remote;

// Reuse passive native snapshots until the generated client replaces the local
// protocol. No persistence or runtime handles cross the Engine interface.
pub(crate) use ngn_core::{
    host::{Approval, ChildTextPhase, ChildToolDispatch},
    legacy::PaintedComposerTheme,
    queues::QueueAction,
    store::{Run, Session},
};

#[derive(Default)]
pub(crate) struct ClientDisplay {
    pub(crate) workspace: PathBuf,
    pub(crate) read_only: bool,
    pub(crate) provider: ProviderDisplay,
    pub(crate) animations: Option<bool>,
    pub(crate) composer_theme: Option<PaintedComposerTheme>,
    /// The host's shell deadline, shown beside each shell call's elapsed time.
    pub(crate) shell_timeout_seconds: Option<u64>,
}

#[derive(Default)]
pub(crate) enum ProviderDisplay {
    #[default]
    Unselected,
    Unavailable,
    Configured {
        id: String,
        kind: String,
        model: Option<String>,
        auth: Option<ProviderAuth>,
    },
}

impl From<&Config> for ClientDisplay {
    fn from(config: &Config) -> Self {
        let provider = match config.providers.get(&config.provider) {
            Some(provider) => ProviderDisplay::Configured {
                id: config.provider.clone(),
                kind: provider.kind.clone(),
                model: Some(provider.model.clone()),
                auth: Some(provider.auth),
            },
            None if config.provider.is_empty() => ProviderDisplay::Unselected,
            None => ProviderDisplay::Unavailable,
        };
        Self {
            workspace: config.workspace.clone(),
            read_only: config.read_only,
            provider,
            animations: config.legacy_animations,
            composer_theme: config.legacy_composer_theme,
            shell_timeout_seconds: Some(config.shell_timeout_seconds),
        }
    }
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
    /// Local broadcast: replay from the native store before drawing.
    Hint,
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

impl From<HostReceipt> for InputReceipt {
    fn from(receipt: HostReceipt) -> Self {
        Self {
            run_id: Some(receipt.id),
            request_id: receipt.request_id,
            status: receipt.status,
        }
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

impl From<ngn_core::steering::SteeringStatus> for SteeringStatus {
    fn from(status: ngn_core::steering::SteeringStatus) -> Self {
        use ngn_core::steering::SteeringStatus as Host;
        match status {
            Host::Pending => Self::Pending,
            Host::Offered => Self::Offered,
            Host::Applied => Self::Applied,
            Host::Uncertain => Self::Uncertain,
            Host::Undelivered => Self::Undelivered,
        }
    }
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

pub(crate) struct InProcess {
    host: Arc<Host>,
    display: ClientDisplay,
}

impl InProcess {
    /// Attach this client to the host started by this invocation.
    pub(crate) fn new(host: Arc<Host>) -> Self {
        let display = ClientDisplay::from(&host.config);
        Self { host, display }
    }
}

impl Engine for InProcess {
    fn display(&self) -> &ClientDisplay {
        &self.display
    }

    fn log_paths(&self) -> Option<LogPaths<'_>> {
        Some(LogPaths {
            runtime: self.host.runtime_log_path(),
            wire: self.host.wire_log_path(),
        })
    }

    async fn subscribe(&self) -> Result<impl Stream<Item = Update> + Send> {
        Ok(stream::unfold(
            self.host.events.subscribe(),
            |mut receiver| async move {
                let update = match receiver.recv().await {
                    Ok(record) => Ok(LiveUpdate {
                        record,
                        delivery: Delivery::Hint,
                    }),
                    Err(RecvError::Lagged(_)) => Err(FeedNotice::ResyncRequired),
                    Err(RecvError::Closed) => return None,
                };
                Some((update, receiver))
            },
        ))
    }

    async fn open_session(&self, selection: SessionSelection<'_>) -> Result<String> {
        let store = &self.host.store;
        match selection {
            SessionSelection::Resume(id) => return Ok(store.session(id).await?.id),
            SessionSelection::LatestOrNew => {
                if let Some(id) = store.latest_native_session().await? {
                    return Ok(id);
                }
            }
            SessionSelection::New => (),
        }
        Ok(store.create_session().await?.id)
    }

    async fn sessions(&self) -> Result<Vec<Session>> {
        self.host.store.sessions().await
    }

    async fn history(&self, session: &str, after: i64) -> Result<HistoryPage> {
        let records = self.host.store.events(session, after).await?;
        Ok(HistoryPage {
            cursor: records.last().map_or(after, |record| record.sequence),
            // Store::events returns at most this many records.
            has_more: records.len() == 1000,
            records,
        })
    }

    async fn conversation_activity(&self, session: &str) -> Result<ConversationActivity> {
        let (active, child_text) = self.host.conversation_activity(session).await;
        Ok(ConversationActivity {
            active,
            child_text,
            approvals: self.host.approvals().await,
        })
    }

    async fn child_tool_dispatches(&self) -> Result<Vec<ChildToolDispatch>> {
        Ok(self.host.child_tool_dispatches())
    }

    async fn tasks(&self, session: &str) -> Result<Option<Vec<TaskInfo>>> {
        Ok(Some(self.host.store.tasks(session).await?))
    }

    fn task_activity_tail(
        &self,
        targets: Vec<TaskIdentity>,
    ) -> impl Future<Output = Result<Vec<Vec<EventRecord>>>> + Send + 'static {
        let store = self.host.store.clone();
        async move { store.task_activity_tail(targets).await }
    }

    async fn submit(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
        submission: Submission,
    ) -> InputAdmission {
        let host = &self.host;
        let approval = ApprovalMode::Interactive;
        let outcome = match submission {
            Submission::Idle => {
                host.submit_idle_with_receipt(session, prompt, request, approval)
                    .await
            }
            Submission::Queue => {
                host.submit_with_receipt(session, prompt, request, approval)
                    .await
            }
        };
        match outcome {
            HostAdmission::Accepted { receipt, warning } => InputAdmission::Accepted {
                receipt: receipt.into(),
                warning,
            },
            HostAdmission::Rejected(error) => InputAdmission::Rejected {
                idle_changed: error.is::<IdleTurnChanged>(),
                error,
            },
            HostAdmission::Unconfirmed(error) => InputAdmission::Unconfirmed(error),
        }
    }

    async fn input_receipt(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
    ) -> Result<Option<InputReceipt>> {
        Ok(self
            .host
            .input_receipt(session, prompt, request)
            .await?
            .map(InputReceipt::from))
    }

    async fn queue(&self, session: &str) -> Result<QueueState> {
        let state = self.host.queue(session).await?;
        Ok(QueueState {
            paused: state.paused,
            entries: state
                .entries
                .into_iter()
                .map(|entry| QueueEntry {
                    prompt: entry.prompt,
                    request_id: entry.run.request_id,
                    run_id: Some(entry.run.id),
                })
                .collect(),
        })
    }

    async fn control_queue(&self, session: &str, action: QueueAction) -> Result<()> {
        self.host.control_queue(session, action).await?;
        Ok(())
    }

    async fn steering(&self, run: &str) -> Result<SteeringState> {
        let state = self.host.steering(run).await?;
        Ok(SteeringState {
            run_id: state.run_id,
            session_id: state.session_id,
            supported: state.supported,
            accepting: state.accepting,
            receipts: state
                .receipts
                .into_iter()
                .map(|receipt| SteeringReceipt {
                    input_id: receipt.input_id,
                    run_id: receipt.run_id,
                    prompt: receipt.prompt,
                    position: receipt.position,
                    status: receipt.status.into(),
                })
                .collect(),
        })
    }

    async fn steer(&self, run: &str, prompt: &str, request: &str) -> Result<SteeringStatus> {
        Ok(self.host.steer(run, prompt, request).await?.status.into())
    }

    async fn decide(&self, run: &str, approval: &str, _call: &str, allow: bool) -> Result<()> {
        // Native approval IDs already bind the exact call inside Host.
        self.host.decide(run, approval, allow).await
    }

    async fn cancel(&self, run: &str) -> Result<()> {
        self.host.cancel(run).await
    }

    async fn shutdown(&self) {
        self.host.shutdown().await;
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use futures_util::StreamExt;
    use ngn_core::config::Backend;
    use ngn_protocol::Event;

    use super::*;

    fn host(directory: &Path) -> Arc<Host> {
        Host::open(Config {
            workspace: directory.into(),
            data_dir: directory.join("state"),
            backend: Backend::Demo,
            ..Config::default()
        })
        .unwrap()
    }

    #[tokio::test]
    async fn startup_uses_exact_resume_and_latest_or_new_without_duplicates() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let engine = InProcess::new(host(directory.path()));
        let first = engine
            .open_session(SessionSelection::initial(None, true))
            .await
            .unwrap();
        let second = engine
            .open_session(SessionSelection::initial(None, false))
            .await
            .unwrap();
        assert_ne!(first, second);
        assert_eq!(
            engine
                .open_session(SessionSelection::initial(Some(&first), false))
                .await
                .unwrap(),
            first
        );
        assert_eq!(
            engine
                .open_session(SessionSelection::initial(None, true))
                .await
                .unwrap(),
            second
        );
        assert!(
            engine
                .open_session(SessionSelection::Resume(&first[..12]))
                .await
                .is_err()
        );
        assert_eq!(engine.sessions().await.unwrap().len(), 2);
        engine.shutdown().await;
    }

    #[tokio::test]
    async fn live_feed_signals_lag_and_history_replays_the_missing_record() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let engine = InProcess::new(host.clone());
        let session = engine.open_session(SessionSelection::New).await.unwrap();
        let mut updates = std::pin::pin!(engine.subscribe().await.unwrap());
        tokio::time::timeout(Duration::from_secs(5), async {
            engine
                .control_queue(&session, QueueAction::Pause)
                .await
                .unwrap();
            let Some(Ok(LiveUpdate { record: first, .. })) = updates.next().await else {
                panic!("expected a live queue update");
            };
            assert!(matches!(first.event, Event::QueueUpdated { paused: true }));
            engine
                .control_queue(&session, QueueAction::Resume)
                .await
                .unwrap();
            // Crowd the committed resume out of the 1024-slot live feed.
            for _ in 0..1024 {
                host.events.send(first.clone()).unwrap();
            }
            assert!(matches!(
                updates.next().await,
                Some(Err(FeedNotice::ResyncRequired))
            ));
            let page = engine.history(&session, first.sequence).await.unwrap();
            assert!(!page.has_more);
            let [resumed] = page.records.as_slice() else {
                panic!("expected exactly the missed durable update");
            };
            assert_eq!(resumed.session_id, session);
            assert!(matches!(
                resumed.event,
                Event::QueueUpdated { paused: false }
            ));
        })
        .await
        .unwrap();
        engine.shutdown().await;
    }
}
