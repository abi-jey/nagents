use std::{
    collections::HashMap,
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex as SyncMutex,
        atomic::{AtomicBool, AtomicI64, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Context, Result, ensure};
use ngn_protocol::{
    Event, EventRecord, RunStatus, SteeringBoundary, SteeringInput, ToolCall, ToolDefinition,
    ToolResult,
};
use serde::Serialize;
use serde_json::Value;
use tokio::sync::{Mutex, broadcast, oneshot, watch};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::{
    Config,
    config::Backend,
    logs::{Logs, RuntimeEvent},
    queues::{QueueAction, QueueState},
    runner::{self, RunOutput},
    steering::{self, SteeringReceipt, SteeringState},
    store::{InputReceipt, Run, Store},
    subagents::{
        self, ChildAdmission, DelegateInput, Execution, TaskInput, TaskOutcome, WaitInput,
    },
    timing::ToolClock,
    tools::Tools,
};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ApprovalMode {
    Deny,
    Interactive,
}

/// The view was idle, but a root started before admission. No new input was accepted.
#[derive(Debug)]
pub struct IdleTurnChanged;

impl std::fmt::Display for IdleTurnChanged {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(
            "An active turn started; draft retained, not queued. Ctrl+Enter to queue explicitly",
        )
    }
}

impl std::error::Error for IdleTurnChanged {}

/// Admission and dispatch are separate: a durable receipt is never a rejection.
pub enum InputAdmission {
    Accepted {
        receipt: InputReceipt,
        warning: Option<anyhow::Error>,
    },
    Rejected(anyhow::Error),
    Unconfirmed(anyhow::Error),
}

impl InputAdmission {
    fn recovered(receipt: InputReceipt, warning: Option<anyhow::Error>) -> Self {
        let warning = warning.or_else(|| {
            receipt.status.is_none().then(|| {
                anyhow::anyhow!("Saved run status is unreadable; the exact admission is confirmed")
            })
        });
        Self::Accepted { receipt, warning }
    }
}

struct ActiveRun {
    execution: Execution,
    accepting_children: bool,
    safe_steering: bool,
    accepting_steering: bool,
    completed: watch::Receiver<bool>,
}

#[derive(Clone, Serialize)]
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

struct Pending {
    approval: Approval,
    answer: oneshot::Sender<bool>,
}

/// Ephemeral host state, not a journal, web event, or runtime frame.
#[derive(Clone)]
pub struct ChildToolDispatch {
    pub run_id: String,
    pub session_id: String,
    pub call_id: String,
}

/// Latest committed public text-phase edge of an exact live child.
/// No text payload is retained, and this is not a published event.
pub struct ChildTextPhase {
    pub run_id: String,
    pub session_id: String,
    pub sequence: i64,
    pub responding: bool,
}

impl ChildTextPhase {
    fn edge(event: &Event) -> Option<bool> {
        match event {
            Event::TextDelta { text } if !text.is_empty() => Some(true),
            Event::RunStarted { .. }
            | Event::TextCompleted { .. }
            | Event::ToolStarted { .. }
            | Event::ApprovalRequested { .. }
            | Event::ToolCompleted { .. } => Some(false),
            _ => None,
        }
    }
}

struct ChildToolGuard<'a> {
    active: &'a SyncMutex<HashMap<String, ChildToolDispatch>>,
    run_id: &'a str,
}

impl Drop for ChildToolGuard<'_> {
    fn drop(&mut self) {
        self.active.lock().unwrap().remove(self.run_id);
    }
}

pub struct Host {
    pub config: Config,
    pub store: Store,
    pub tools: Tools,
    pub events: broadcast::Sender<EventRecord>,
    pub(crate) logs: Logs,
    active: Mutex<HashMap<String, ActiveRun>>,
    pending: Mutex<HashMap<String, Pending>>,
    child_tools: SyncMutex<HashMap<String, ChildToolDispatch>>,
    workers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    emission: Mutex<()>,
    closed: AtomicBool,
    _identity_guard: crate::auth::IdentityGuard,
}

impl Host {
    pub fn open(config: Config) -> Result<Arc<Self>> {
        Self::open_with_wire_log(config, false)
    }

    /// Enable local provider payload capture for this host invocation only.
    pub fn open_with_wire_log(config: Config, wire_log: bool) -> Result<Arc<Self>> {
        let identity_guard = crate::auth::IdentityGuard::host(&config.data_dir)?;
        let store = Store::open(&config.state_dir())?;
        let protected = config.data_dir.canonicalize()?;
        let tools = Tools::open(
            &config.workspace,
            &protected,
            config.read_only,
            config.shell_timeout_seconds,
        )?;
        #[cfg(any(test, feature = "test-support"))]
        let tools = if config.backend == Backend::Demo {
            tools.with_demo_preview()
        } else {
            tools
        };
        let (events, _) = broadcast::channel(1024);
        let logs = Logs::open(&protected, wire_log)?;
        Ok(Arc::new(Self {
            config,
            store,
            tools,
            events,
            logs,
            active: Mutex::new(HashMap::new()),
            pending: Mutex::new(HashMap::new()),
            child_tools: SyncMutex::new(HashMap::new()),
            workers: Mutex::new(Vec::new()),
            emission: Mutex::new(()),
            closed: AtomicBool::new(false),
            _identity_guard: identity_guard,
        }))
    }

    pub fn runtime_log_path(&self) -> Option<&std::path::Path> {
        self.logs.runtime_path()
    }

    pub fn wire_log_path(&self) -> Option<&std::path::Path> {
        self.logs.wire_path()
    }

    pub async fn start(
        self: &Arc<Self>,
        session: &str,
        prompt: &str,
        request_id: &str,
        approval: ApprovalMode,
    ) -> Result<Run> {
        ensure!(
            !prompt.trim().is_empty() && prompt.len() <= 32_000,
            "Prompt must contain 1..32000 UTF-8 bytes"
        );
        Uuid::parse_str(request_id).context(
            "request_id must be a UUID; reuse it only when retrying the same submission",
        )?;
        self.store.session(session).await?;
        self.store.ensure_not_archive(session).await?;
        ensure!(
            !self.store.is_child_session(session).await?,
            "Subagent sessions are inspect-only; they cannot be resumed as unrestricted root agents"
        );
        let mut active = self.active.lock().await;
        ensure!(
            !self.closed.load(Ordering::Acquire),
            "Host is shutting down"
        );
        if let Some(existing) = self
            .store
            .existing_request(session, request_id, prompt)
            .await?
        {
            return Ok(existing);
        }
        let queue = self.store.queue(session).await?;
        ensure!(
            !queue.paused && queue.entries.is_empty(),
            "Session has a paused or pending input queue; resume or clear it explicitly"
        );
        ensure!(
            !active
                .values()
                .any(|active| active.execution.run.session_id == session),
            "Session already has an active run"
        );
        ensure!(
            active
                .values()
                .filter(|active| active.execution.task.is_none())
                .count()
                < 8,
            "Host already has eight active root runs"
        );
        ensure!(active.len() < 64, "Host active-run capacity exhausted");
        if self.config.backend == Backend::Nagents {
            self.config.selected_provider()?;
        }
        let _emission = self.emission.lock().await;
        let (run, record) = self.store.accept(session, request_id, prompt).await?;
        self.launch_root(&mut active, run.clone(), prompt.to_owned(), approval)
            .await;
        let _ = self.events.send(record);
        Ok(run)
    }

    async fn root_session(&self, session: &str) -> Result<()> {
        self.store.session(session).await?;
        ensure!(
            !self.store.is_child_session(session).await?,
            "Subagent sessions are inspect-only; they cannot have root input queues"
        );
        Ok(())
    }

    pub async fn queue(&self, session: &str) -> Result<QueueState> {
        self.root_session(session).await?;
        // Serialize snapshots with admission, dispatch and operator controls.
        let _active = self.active.lock().await;
        self.store.queue(session).await
    }

    /// Admit only to this activation. Never silently turn steering into a queued run.
    pub async fn steer(
        &self,
        run_id: &str,
        prompt: &str,
        request_id: &str,
    ) -> Result<SteeringReceipt> {
        ensure!(
            !prompt.trim().is_empty() && prompt.len() <= steering::MAX_PROMPT_BYTES,
            "Steering prompt must contain 1..32000 UTF-8 bytes"
        );
        let request = Uuid::parse_str(request_id)
            .context("Steering request_id must be a UUID")?
            .to_string();
        let active = self.active.lock().await;
        // Retry identity is independent of current liveness and queue identities.
        if let Some(saved) = self
            .store
            .existing_steering(run_id, prompt, &request)
            .await?
        {
            return Ok(saved);
        }
        ensure!(
            !self.closed.load(Ordering::Acquire),
            "Host is shutting down"
        );
        let owner = active
            .get(run_id)
            .context("Steering requires the exact running root in this host")?;
        ensure!(
            owner.execution.task.is_none(),
            "Subagent runs cannot receive operator steering"
        );
        ensure!(
            owner.safe_steering,
            "Runtime has not negotiated safe_steering; no input was admitted"
        );
        ensure!(
            owner.accepting_steering && !owner.execution.cancel.is_cancelled(),
            "Run is closing; steering admission is sealed"
        );
        let receipt = self
            .store
            .admit_steering(&owner.execution.run, prompt, &request)
            .await?;
        self.steering_updated(&owner.execution.run, &receipt.input_id)
            .await?;
        Ok(receipt)
    }

    pub async fn steering(&self, run_id: &str) -> Result<SteeringState> {
        let active = self.active.lock().await;
        let run = self.store.steering_run(run_id).await?;
        let owner = active.get(run_id);
        Ok(SteeringState {
            run_id: run.id,
            session_id: run.session_id,
            run_status: run.status,
            supported: owner
                .is_some_and(|owner| owner.safe_steering && owner.execution.task.is_none()),
            accepting: owner.is_some_and(|owner| {
                owner.safe_steering
                    && owner.accepting_steering
                    && owner.execution.task.is_none()
                    && !owner.execution.cancel.is_cancelled()
                    && !self.closed.load(Ordering::Acquire)
            }),
            receipts: self.store.steering_receipts(run_id).await?,
            run_limit: steering::MAX_RUN_INPUTS,
            host_limit: steering::MAX_HOST_INPUTS,
            prompt_byte_limit: steering::MAX_PROMPT_BYTES,
        })
    }

    pub(crate) async fn negotiate_steering(&self, run: &Run, supported: bool) -> Result<()> {
        let mut active = self.active.lock().await;
        let owner = active
            .get_mut(&run.id)
            .context("Runtime activation is no longer active")?;
        ensure!(
            owner.execution.run.session_id == run.session_id,
            "Runtime activation identity mismatch"
        );
        owner.safe_steering = supported;
        // A handshake cannot reopen admission after cancellation or shutdown.
        owner.accepting_steering &= supported
            && !owner.execution.cancel.is_cancelled()
            && !self.closed.load(Ordering::Acquire);
        if owner.execution.task.is_none() {
            self.steering_updated(run, "").await?;
        }
        Ok(())
    }

    /// Called with admission locked: persist invalidation before broadcasting.
    /// This is not a runtime message and conveys no model-supplied authority.
    async fn steering_updated(&self, run: &Run, input_id: &str) -> Result<()> {
        let _emission = self.emission.lock().await;
        let record = self
            .store
            .append(
                run,
                Event::SteeringUpdated {
                    input_id: input_id.to_owned(),
                },
            )
            .await?;
        let _ = self.events.send(record);
        Ok(())
    }

    pub(crate) async fn steering_boundary(
        &self,
        run: &Run,
        boundary_id: &str,
        kind: SteeringBoundary,
    ) -> Result<Option<SteeringInput>> {
        let mut active = self.active.lock().await;
        let owner = active
            .get_mut(&run.id)
            .context("Runtime activation is no longer active")?;
        ensure!(
            owner.execution.run.session_id == run.session_id && owner.safe_steering,
            "Unnegotiated or foreign steering boundary"
        );
        ensure!(
            owner.accepting_steering
                && !owner.execution.cancel.is_cancelled()
                && !self.closed.load(Ordering::Acquire),
            "Run steering admission is sealed"
        );
        let offered = self.store.offer_steering(&run.id, boundary_id).await?;
        if offered.is_none() && kind == SteeringBoundary::Final {
            // Crucially, close under the SAME lock as steer(), BEFORE writing the
            // empty reply. A later caller gets a conflict, never a lost receipt.
            owner.accepting_steering = false;
        }
        if offered.is_some() || kind == SteeringBoundary::Final {
            self.steering_updated(
                run,
                offered
                    .as_ref()
                    .map_or("", |receipt| receipt.input_id.as_str()),
            )
            .await?;
        }
        Ok(offered.map(|receipt| receipt.input()))
    }

    pub(crate) async fn steering_applied(
        &self,
        run: &Run,
        boundary_id: &str,
        input_id: &str,
    ) -> Result<()> {
        let active = self.active.lock().await;
        let owner = active
            .get(&run.id)
            .context("Runtime activation is no longer active")?;
        ensure!(
            owner.execution.run.session_id == run.session_id
                && owner.safe_steering
                && !owner.execution.cancel.is_cancelled(),
            "Unnegotiated, cancelled or foreign steering acknowledgment"
        );
        self.store
            .apply_steering(&run.id, boundary_id, input_id)
            .await?;
        self.steering_updated(run, input_id).await
    }

    /// Accept an input exactly once. Busy or paused sessions retain it durably.
    pub async fn submit(
        self: &Arc<Self>,
        session: &str,
        prompt: &str,
        request_id: &str,
        approval: ApprovalMode,
    ) -> Result<Run> {
        match self
            .submit_with_receipt(session, prompt, request_id, approval)
            .await
        {
            InputAdmission::Accepted { receipt, warning } => {
                if warning.is_some() {
                    eprintln!("ngn: input accepted with a queue warning; inspect the input queue");
                }
                receipt.into_run()
            }
            InputAdmission::Rejected(error) => Err(error),
            InputAdmission::Unconfirmed(error) => Err(anyhow::anyhow!(
                "Input acceptance unconfirmed; retry only with the same session, text and request ID: {error}"
            )),
        }
    }

    /// Inspect only the exact acceptance identity. Never enqueue or dispatch.
    pub async fn input_receipt(
        &self,
        session: &str,
        prompt: &str,
        request_id: &str,
    ) -> Result<Option<InputReceipt>> {
        let _active = self.active.lock().await;
        self.store.input_receipt(session, request_id, prompt).await
    }

    pub async fn submit_with_receipt(
        self: &Arc<Self>,
        session: &str,
        prompt: &str,
        request_id: &str,
        approval: ApprovalMode,
    ) -> InputAdmission {
        self.submit_input(session, prompt, request_id, approval, false)
            .await
    }

    /// Send from an idle view without silently queueing behind a newly active root.
    /// Paused queues and older entries still obey the normal durable FIFO policy.
    pub async fn submit_idle_with_receipt(
        self: &Arc<Self>,
        session: &str,
        prompt: &str,
        request_id: &str,
        approval: ApprovalMode,
    ) -> InputAdmission {
        self.submit_input(session, prompt, request_id, approval, true)
            .await
    }

    async fn submit_input(
        self: &Arc<Self>,
        session: &str,
        prompt: &str,
        request_id: &str,
        approval: ApprovalMode,
        require_idle: bool,
    ) -> InputAdmission {
        if prompt.trim().is_empty() || prompt.len() > 32_000 {
            return InputAdmission::Rejected(anyhow::anyhow!(
                "Prompt must contain 1..32000 UTF-8 bytes"
            ));
        }
        if let Err(error) = Uuid::parse_str(request_id)
            .context("request_id must be a UUID; reuse it only when retrying the same submission")
        {
            return InputAdmission::Rejected(error);
        }
        let mut active = self.active.lock().await;
        // Reconcile before admission preconditions (including shutdown). A
        // retry can confirm a finished or cancelled run, without dispatching it.
        match self.store.input_receipt(session, request_id, prompt).await {
            Ok(Some(receipt)) => return InputAdmission::recovered(receipt, None),
            Ok(None) => (),
            Err(error) => return InputAdmission::Unconfirmed(error),
        }
        if let Err(error) = self.check_input_admission(session).await {
            return InputAdmission::Rejected(error);
        }
        if require_idle
            && active.values().any(|owner| {
                owner.execution.task.is_none() && owner.execution.run.session_id == session
            })
        {
            return InputAdmission::Rejected(IdleTurnChanged.into());
        }
        let enqueued = {
            let _emission = self.emission.lock().await;
            let enqueued = self
                .store
                .enqueue(
                    session,
                    prompt,
                    request_id,
                    approval == ApprovalMode::Interactive,
                )
                .await;
            enqueued.map(|(run, record)| {
                let _ = self.events.send(record);
                run
            })
        };
        let mut run = match enqueued {
            Ok(run) => run,
            Err(error) => {
                // A store error is not proof that the enqueue failed to commit.
                return match self.store.input_receipt(session, request_id, prompt).await {
                    Ok(Some(receipt)) => InputAdmission::recovered(
                        receipt,
                        Some(error.context("Enqueue reported an error; dispatch skipped")),
                    ),
                    Ok(None) => InputAdmission::Rejected(error),
                    Err(read) => InputAdmission::Unconfirmed(
                        error.context(format!("Cannot confirm the FIFO receipt: {read}")),
                    ),
                };
            }
        };
        let warning = self.dispatch(&mut active).await.err().map(|error| {
            self.logs.record(RuntimeEvent::DispatchFailed);
            anyhow::anyhow!("Cannot dispatch pending inputs: {error}")
        });
        if active.contains_key(&run.id) {
            run.status = RunStatus::Running;
        }
        // The enqueue transaction returned a durable receipt. Even if dispatch
        // fails, the caller must stop treating this draft as unsent.
        InputAdmission::Accepted {
            receipt: run.into(),
            warning,
        }
    }

    async fn check_input_admission(&self, session: &str) -> Result<()> {
        ensure!(
            !self.closed.load(Ordering::Acquire),
            "Host is shutting down"
        );
        self.root_session(session).await?;
        self.store.ensure_not_archive(session).await?;
        if self.config.backend == Backend::Nagents {
            self.config.selected_provider()?;
        }
        Ok(())
    }

    pub async fn control_queue(
        self: &Arc<Self>,
        session: &str,
        action: QueueAction,
    ) -> Result<QueueState> {
        self.root_session(session).await?;
        self.store.ensure_not_archive(session).await?;
        let mut active = self.active.lock().await;
        ensure!(
            !self.closed.load(Ordering::Acquire),
            "Host is shutting down"
        );
        if matches!(action, QueueAction::Resume) && self.config.backend == Backend::Nagents {
            self.config.selected_provider()?;
        }
        {
            let _emission = self.emission.lock().await;
            for record in self.store.queue_control(session, action).await? {
                let _ = self.events.send(record);
            }
        }
        self.dispatch(&mut active).await?;
        self.store.queue(session).await
    }

    async fn launch_root(
        self: &Arc<Self>,
        active: &mut HashMap<String, ActiveRun>,
        run: Run,
        prompt: String,
        approval: ApprovalMode,
    ) {
        let execution = Execution {
            run: run.clone(),
            root: run.clone(),
            task: None,
            read_only: self.config.read_only,
            approval,
            cancel: CancellationToken::new(),
            text_edge: None,
        };
        let (completed, receiver) = watch::channel(false);
        active.insert(
            run.id.clone(),
            ActiveRun {
                execution: execution.clone(),
                accepting_children: true,
                safe_steering: false,
                accepting_steering: true,
                completed: receiver,
            },
        );
        let mut workers = self.workers.lock().await;
        workers.retain(|worker| !worker.is_finished());
        workers.push(tokio::spawn(
            self.clone().worker(execution, prompt, completed),
        ));
    }

    /// Always called under the admission lock; claiming and starting are atomic
    /// with respect to pause, cancellation, shutdown and all other submissions.
    async fn dispatch(self: &Arc<Self>, active: &mut HashMap<String, ActiveRun>) -> Result<()> {
        if self.closed.load(Ordering::Acquire) {
            return Ok(());
        }
        for input in self.store.ready_inputs().await? {
            if self.closed.load(Ordering::Acquire)
                || active.len() >= 64
                || active
                    .values()
                    .filter(|run| run.execution.task.is_none())
                    .count()
                    >= 8
            {
                break;
            }
            let mut run = input.entry.run;
            if active
                .values()
                .any(|active| active.execution.run.session_id == run.session_id)
            {
                continue;
            }
            let _emission = self.emission.lock().await;
            let record = self.store.claim_input(&run).await?;
            run.status = RunStatus::Running;
            let approval = if input.interactive {
                ApprovalMode::Interactive
            } else {
                ApprovalMode::Deny
            };
            self.launch_root(active, run, input.entry.prompt, approval)
                .await;
            let _ = self.events.send(record);
        }
        Ok(())
    }

    // Erase the worker future's type: a run may request a child which starts the
    // same runner, but admission never recursively awaits another worker future.
    fn worker(
        self: Arc<Self>,
        execution: Execution,
        prompt: String,
        completed: watch::Sender<bool>,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        Box::pin(async move {
            self.logs.record(RuntimeEvent::RunStarted);
            let mut output = {
                let runtime = async {
                    match self.config.backend {
                        #[cfg(any(test, feature = "test-support"))]
                        Backend::Demo => {
                            runner::demo(&self, &execution.run, &prompt, execution.cancel.clone())
                                .await
                        }
                        Backend::Nagents => runner::stdio(&self, &execution, &prompt).await,
                    }
                };
                tokio::pin!(runtime);
                tokio::select! {
                    biased;
                    _ = execution.cancel.cancelled() => {
                        let _ = tokio::time::timeout(Duration::from_secs(5), &mut runtime).await;
                        RunOutput::cancelled()
                    }
                    result = &mut runtime => match result {
                        Ok(output) => output,
                        Err(error) => {
                            self.logs.record(RuntimeEvent::RunnerFailed);
                            RunOutput::failed(error.to_string())
                        },
                    },
                    _ = tokio::time::sleep(Duration::from_secs(self.config.subagents.timeout_seconds)), if execution.task.is_some() => {
                        execution.cancel.cancel();
                        let _ = tokio::time::timeout(Duration::from_secs(5), &mut runtime).await;
                        RunOutput::failed("Subagent execution deadline exceeded; completed actions were not rolled back".into())
                    }
                }
            };
            // Close admission before checking children. No child can escape the
            // drain by being admitted concurrently with its parent's completion.
            let children = {
                let mut active = self.active.lock().await;
                if let Some(owner) = active.get_mut(&execution.run.id) {
                    owner.accepting_children = false;
                    owner.accepting_steering = false;
                }
                active
                    .values()
                    .filter(|child| {
                        child
                            .execution
                            .task
                            .as_ref()
                            .is_some_and(|task| task.parent_run_id == execution.run.id)
                    })
                    .map(|child| child.completed.clone())
                    .collect::<Vec<_>>()
            };
            if !children.is_empty() && output.status == RunStatus::Completed {
                output = RunOutput::failed("Agent ended with active background subagents. They were cancelled; use agent_wait before finishing.".into());
            }
            execution.cancel.cancel(); // Structured lifetime: all descendants stop with their owner.
            for mut child in children {
                while !*child.borrow_and_update() {
                    if child.changed().await.is_err() {
                        break;
                    }
                }
            }
            self.pending
                .lock()
                .await
                .retain(|_, pending| pending.approval.run_id != execution.run.id);
            let mut active = self.active.lock().await;
            let _emission = self.emission.lock().await;
            let records = self
                .store
                .finish(&execution.run, output.status, &output.text, &output.error)
                .await;
            self.logs.record(match output.status {
                RunStatus::Completed => RuntimeEvent::RunCompleted,
                RunStatus::Cancelled => RuntimeEvent::RunCancelled,
                _ => RuntimeEvent::RunFailed,
            });
            active.remove(&execution.run.id);
            let _ = completed.send(true);
            match records {
                Ok(records) => {
                    for record in records {
                        let _ = self.events.send(record);
                    }
                }
                Err(_) => {
                    self.logs.record(RuntimeEvent::PersistenceFailed);
                    eprintln!(
                        "ngn: unable to persist final run status; it will be marked interrupted on restart"
                    );
                }
            }
            drop(_emission);
            if self.dispatch(&mut active).await.is_err() {
                self.logs.record(RuntimeEvent::DispatchFailed);
                eprintln!(
                    "ngn: unable to dispatch pending inputs; inspect the queue before resuming"
                );
            }
        })
    }

    async fn execution(&self, run: &Run) -> Result<Execution> {
        let active = self.active.lock().await;
        let execution = &active.get(&run.id).context("Run is not active")?.execution;
        ensure!(
            execution.run.session_id == run.session_id,
            "Run/session identity mismatch"
        );
        Ok(execution.clone())
    }

    pub(crate) fn catalog(&self, execution: &Execution) -> Vec<ToolDefinition> {
        let mut tools = self.tools.restricted(execution.read_only).definitions();
        if self.config.subagents.max_depth > 0 {
            tools.extend(subagents::definitions(
                execution.depth() < self.config.subagents.max_depth,
            ));
        }
        tools
    }

    pub async fn emit(&self, run: &Run, event: Event) -> Result<()> {
        let execution = self.execution(run).await?;
        let attention = matches!(
            event,
            Event::ApprovalRequested { .. } | Event::ApprovalResolved { .. }
        );
        let _emission = self.emission.lock().await;
        let record = self.store.append(run, event).await?;
        if let Some(edge) = &execution.text_edge
            && let Some(open) = ChildTextPhase::edge(&record.event)
        {
            // Make the committed edge observable even if broadcast drops it.
            edge.store(
                if open {
                    record.sequence
                } else {
                    -record.sequence
                },
                Ordering::Release,
            );
        }
        let _ = self.events.send(record);
        if attention && let Some(task) = execution.task {
            // Root operators need child approval attention, never child reasoning
            // or raw text masquerading as the parent's assistant response.
            let record = self
                .store
                .append(&execution.root, Event::TaskUpdated { task })
                .await?;
            let _ = self.events.send(record);
        }
        Ok(())
    }

    pub async fn active(&self) -> Vec<Run> {
        self.active
            .lock()
            .await
            .values()
            .map(|active| active.execution.run.clone())
            .collect()
    }

    /// Read the foreground run and related ephemeral child text state in one
    /// active-set lock, with no history read or extra poll.
    pub async fn conversation_activity(&self, session: &str) -> (Option<Run>, Vec<ChildTextPhase>) {
        let active = self.active.lock().await;
        let mut current = None;
        let mut children = Vec::new();
        for entry in active.values() {
            let execution = &entry.execution;
            if execution.run.session_id == session {
                current = Some(execution.run.clone());
            }
            if let (Some(task), Some(edge)) = (&execution.task, &execution.text_edge)
                && (task.root_session_id == session || task.parent_session_id == session)
            {
                let position = edge.load(Ordering::Acquire);
                if position != 0 {
                    children.push(ChildTextPhase {
                        run_id: execution.run.id.clone(),
                        session_id: execution.run.session_id.clone(),
                        sequence: position.abs(),
                        responding: position > 0,
                    });
                }
            }
        }
        (current, children)
    }
    pub async fn approvals(&self) -> Vec<Approval> {
        self.pending
            .lock()
            .await
            .values()
            .map(|pending| pending.approval.clone())
            .collect()
    }

    pub async fn decide(&self, run: &str, id: &str, allow: bool) -> Result<()> {
        let mut pending = self.pending.lock().await;
        let decision = pending.get(id).context("Approval is no longer pending")?;
        ensure!(
            decision.approval.run_id == run,
            "Approval does not belong to this run"
        );
        let decision = pending
            .remove(id)
            .context("Approval is no longer pending")?;
        decision
            .answer
            .send(allow)
            .map_err(|_| anyhow::anyhow!("Approval expired or run was cancelled"))
    }

    pub async fn cancel(&self, run: &str) -> Result<()> {
        let mut active = self.active.lock().await;
        let _emission = self.emission.lock().await;
        if let Some(owner) = active.get_mut(run) {
            owner.accepting_steering = false;
            self.store.settle_steering(run).await?;
            if owner.execution.task.is_none() {
                // Already holding emission and admission locks. Failure to notify
                // must not prevent cancellation; final/recovery snapshots remain.
                match self
                    .store
                    .append(
                        &owner.execution.run,
                        Event::SteeringUpdated {
                            input_id: String::new(),
                        },
                    )
                    .await
                {
                    Ok(record) => {
                        let _ = self.events.send(record);
                    }
                    Err(_) => eprintln!(
                        "ngn: unable to persist steering cancellation update; inspect final receipts"
                    ),
                }
                for record in self
                    .store
                    .queue_control(&owner.execution.run.session_id, QueueAction::Pause)
                    .await?
                {
                    let _ = self.events.send(record);
                }
            }
            owner.execution.cancel.cancel();
        } else {
            for record in self.store.cancel_input(run).await? {
                let _ = self.events.send(record);
            }
        }
        Ok(())
    }

    async fn delegate(
        self: &Arc<Self>,
        owner: &Execution,
        call: &ToolCall,
        input: DelegateInput,
    ) -> Result<TaskOutcome> {
        let mut active = self.active.lock().await;
        ensure!(
            !self.closed.load(Ordering::Acquire),
            "Host is shutting down"
        );
        let parent = active
            .get(&owner.run.id)
            .context("Delegation parent is no longer active")?;
        ensure!(
            parent.accepting_children && !parent.execution.cancel.is_cancelled(),
            "Delegation parent is closing"
        );
        let root = active
            .get(&owner.root.id)
            .context("Delegation root is no longer active")?;
        ensure!(
            root.accepting_children && !root.execution.cancel.is_cancelled(),
            "Delegation root is closing"
        );
        ensure!(active.len() < 64, "Host active-run capacity exhausted");
        let depth = owner.depth() + 1;
        let read_only = owner.read_only || input.read_only;
        let _emission = self.emission.lock().await;
        let (run, task, records) = self
            .store
            .admit_child(ChildAdmission {
                parent: owner.run.clone(),
                root: owner.root.clone(),
                call_id: call.call_id.clone(),
                prompt: input.prompt.clone(),
                title: input.title,
                depth,
                read_only,
                limits: self.config.subagents.clone(),
            })
            .await?;
        let execution = Execution {
            run: run.clone(),
            root: owner.root.clone(),
            task: Some(task.clone()),
            read_only,
            approval: owner.approval,
            cancel: owner.cancel.child_token(),
            text_edge: Some(Arc::new(AtomicI64::new(0))),
        };
        let (completed, receiver) = watch::channel(false);
        active.insert(
            run.id.clone(),
            ActiveRun {
                execution: execution.clone(),
                accepting_children: true,
                safe_steering: false,
                accepting_steering: true,
                completed: receiver,
            },
        );
        let mut workers = self.workers.lock().await;
        workers.retain(|worker| !worker.is_finished());
        workers.push(tokio::spawn(self.clone().worker(
            execution,
            input.prompt,
            completed,
        )));
        for record in records {
            let _ = self.events.send(record);
        }
        Ok(TaskOutcome {
            task,
            result: String::new(),
            error: String::new(),
        })
    }

    async fn owned_task(&self, owner: &Execution, run_id: &str) -> Result<TaskOutcome> {
        ensure!(
            !run_id.is_empty() && run_id.len() <= 80,
            "Invalid subagent run_id"
        );
        let task = self.store.task(run_id).await?;
        ensure!(
            task.task.parent_run_id == owner.run.id,
            "Only the immediate owning run can control or receive this subagent's result"
        );
        Ok(task)
    }

    async fn wait_task(
        &self,
        owner: &Execution,
        run_id: &str,
        timeout: Option<Duration>,
        dispatch: Option<&ToolCall>,
    ) -> Result<TaskOutcome> {
        let outcome = self.owned_task(owner, run_id).await?;
        let _dispatch = dispatch
            .map(|call| self.begin_child_tool(owner, call))
            .transpose()?
            .flatten();
        if subagents::terminal(outcome.task.status) {
            return Ok(outcome);
        }
        let receiver = self
            .active
            .lock()
            .await
            .get(run_id)
            .map(|active| active.completed.clone());
        if let Some(mut receiver) = receiver {
            let wait = async {
                while !*receiver.borrow_and_update() {
                    if receiver.changed().await.is_err() {
                        break;
                    }
                }
            };
            tokio::select! {
                _ = wait => (),
                _ = owner.cancel.cancelled() => anyhow::bail!("Delegation wait cancelled"),
                _ = async { match timeout { Some(timeout) => tokio::time::sleep(timeout).await, None => std::future::pending().await } } => (),
            }
        }
        self.owned_task(owner, run_id).await
    }

    pub fn child_tool_dispatches(&self) -> Vec<ChildToolDispatch> {
        self.child_tools.lock().unwrap().values().cloned().collect()
    }

    fn begin_child_tool<'a>(
        &'a self,
        owner: &'a Execution,
        call: &ToolCall,
    ) -> Result<Option<ChildToolGuard<'a>>> {
        if owner.task.is_none() {
            return Ok(None);
        }
        ensure!(!owner.cancel.is_cancelled(), "Run was cancelled");
        let run_id = &owner.run.id;
        let mut active = self.child_tools.lock().unwrap();
        ensure!(
            !active.contains_key(run_id),
            "Child already dispatching a tool"
        );
        active.insert(
            run_id.clone(),
            ChildToolDispatch {
                run_id: run_id.clone(),
                session_id: owner.run.session_id.clone(),
                call_id: call.call_id.clone(),
            },
        );
        Ok(Some(ChildToolGuard {
            active: &self.child_tools,
            run_id,
        }))
    }

    pub async fn tool(self: &Arc<Self>, run: &Run, call: ToolCall) -> Result<ToolResult> {
        let owner = self.execution(run).await?;
        ensure!(!owner.cancel.is_cancelled(), "Run was cancelled");
        ensure!(
            !call.call_id.is_empty() && call.call_id.len() <= 200 && call.arguments.is_object(),
            "Invalid tool call"
        );
        self.emit(run, Event::ToolStarted { call: call.clone() })
            .await?;
        self.logs.record(RuntimeEvent::ToolStarted);
        let mut clock = ToolClock::new(Instant::now());
        let result: Result<Value> = async {
            match call.name.as_str() {
                "to_agent" => {
                    let input: DelegateInput = serde_json::from_value(call.arguments.clone())?;
                    ensure!(
                        !input.prompt.trim().is_empty() && input.prompt.len() <= 32_000,
                        "Delegation prompt must contain 1..32000 UTF-8 bytes"
                    );
                    ensure!(
                        !input.title.trim().is_empty()
                            && input.title.len() <= 160
                            && input.title.chars().all(|character| !character.is_control()),
                        "Subagent title must contain 1..160 UTF-8 bytes without control characters"
                    );
                    let background = input.background;
                    let _dispatch = self.begin_child_tool(&owner, &call)?;
                    let task = self.delegate(&owner, &call, input).await?;
                    if background { return Ok(serde_json::to_value(task)?); }
                    return Ok(serde_json::to_value(self.wait_task(&owner, &task.task.run_id, None, None).await?)?);
                }
                "agent_status" | "agent_cancel" => {
                    let input: TaskInput = serde_json::from_value(call.arguments.clone())?;
                    let task = self.owned_task(&owner, &input.run_id).await?;
                    let _dispatch = self.begin_child_tool(&owner, &call)?;
                    if call.name == "agent_cancel" && task.task.status == RunStatus::Running {
                        let _ = self.cancel(&input.run_id).await;
                        return Ok(serde_json::to_value(self.wait_task(&owner, &input.run_id, None, None).await?)?);
                    }
                    return Ok(serde_json::to_value(self.owned_task(&owner, &input.run_id).await?)?);
                }
                "agent_wait" => {
                    let input: WaitInput = serde_json::from_value(call.arguments.clone())?;
                    ensure!((1..=60).contains(&input.timeout_seconds), "Wait timeout must be 1..60 seconds");
                    return Ok(serde_json::to_value(self.wait_task(&owner, &input.run_id, Some(Duration::from_secs(input.timeout_seconds)), Some(&call)).await?)?);
                }
                _ => (),
            }
            let tools = self.tools.restricted(owner.read_only);
            let owned = call.clone();
            let prepare_tools = tools.clone();
            let prepared = tokio::task::spawn_blocking(move || prepare_tools.prepare(&owned)).await??;
            if prepared.needs_approval {
                ensure!(owner.approval == ApprovalMode::Interactive, "Denied: non-interactive runs cannot grant tool approval");
                let id = format!("approval-{}", Uuid::new_v4().simple());
                let (answer, receive) = oneshot::channel();
                let approval = Approval { id:id.clone(), run_id:run.id.clone(), session_id:run.session_id.clone(), root_session_id:owner.root.session_id.clone(), agent_title:owner.task.as_ref().map_or_else(|| "Main".into(), |task| task.title.clone()), depth:owner.depth(), call:call.clone(), preview:prepared.preview.clone() };
                self.pending.lock().await.insert(id.clone(), Pending { approval, answer });
                if let Err(error) = self.emit(run, Event::ApprovalRequested { approval_id:id.clone(), call:call.clone(), preview:prepared.preview.clone() }).await { self.pending.lock().await.remove(&id); return Err(error); }
                let elapsed_ms = clock.pause(Instant::now());
                if let Err(error) = self.emit(run, Event::ToolApprovalTiming { call_id:call.call_id.clone(), elapsed_ms, waiting:true }).await { self.pending.lock().await.remove(&id); return Err(error); }
                let allowed = tokio::select! { answer = receive => answer.unwrap_or(false), _ = owner.cancel.cancelled() => false, _ = tokio::time::sleep(Duration::from_secs(300)) => false };
                let elapsed_ms = clock.resume(Instant::now());
                self.pending.lock().await.remove(&id);
                self.emit(run, Event::ToolApprovalTiming { call_id:call.call_id.clone(), elapsed_ms, waiting:false }).await?;
                self.emit(run, Event::ApprovalResolved { approval_id:id, allowed }).await?;
                ensure!(allowed && !owner.cancel.is_cancelled(), "Tool approval denied, expired or cancelled");
            }
            let _dispatch = self.begin_child_tool(&owner, &call)?;
            tools.execute(prepared, owner.cancel.clone()).await
        }.await;
        let (result, error) = match result {
            Ok(result) => {
                self.logs.record(RuntimeEvent::ToolCompleted);
                (result, String::new())
            }
            Err(error) => {
                self.logs.record(RuntimeEvent::ToolFailed);
                (Value::Null, error.to_string())
            }
        };
        let result = ToolResult {
            call_id: call.call_id,
            name: call.name,
            result,
            error,
        };
        self.emit(
            run,
            Event::ToolCompleted {
                result: result.clone(),
                duration_ms: clock.elapsed(Instant::now()),
            },
        )
        .await?;
        Ok(result)
    }

    pub async fn shutdown(&self) {
        let first_shutdown = !self.closed.swap(true, Ordering::AcqRel);
        let mut active = self.active.lock().await;
        {
            let _emission = self.emission.lock().await;
            match self.store.pause_pending().await {
                Ok(records) => {
                    for record in records {
                        let _ = self.events.send(record);
                    }
                }
                Err(_) => {
                    self.logs.record(RuntimeEvent::ShutdownFailed);
                    eprintln!(
                        "ngn: unable to persist paused queues; recovery will pause unstarted inputs"
                    );
                }
            }
        }
        for active in active.values_mut() {
            active.accepting_steering = false;
            if self
                .store
                .settle_steering(&active.execution.run.id)
                .await
                .is_err()
            {
                self.logs.record(RuntimeEvent::ShutdownFailed);
                eprintln!(
                    "ngn: unable to settle steering receipts; recovery will mark them unconfirmed"
                );
            }
            active.execution.cancel.cancel();
        }
        drop(active);
        let workers = std::mem::take(&mut *self.workers.lock().await);
        for mut worker in workers {
            if tokio::time::timeout(Duration::from_secs(15), &mut worker)
                .await
                .is_err()
            {
                self.logs.record(RuntimeEvent::ShutdownFailed);
                worker.abort();
                let _ = worker.await;
            }
        }
        if first_shutdown {
            self.logs.record(RuntimeEvent::HostStopped);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::Path;

    fn host(directory: &Path) -> Arc<Host> {
        Host::open(
            Config {
                workspace: directory.to_owned(),
                data_dir: directory.join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn submissions_are_idempotent_and_concurrency_is_per_session() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let session = host.store.create_session().await.unwrap();
        let request = Uuid::new_v4().to_string();
        let first = host
            .start(&session.id, "demo hold", &request, ApprovalMode::Deny)
            .await
            .unwrap();
        let again = host
            .start(&session.id, "demo hold", &request, ApprovalMode::Deny)
            .await
            .unwrap();
        assert_eq!(first.id, again.id);
        assert!(
            host.start(
                &session.id,
                "other",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny
            )
            .await
            .is_err()
        );
        host.shutdown().await;
    }

    #[tokio::test]
    async fn noninteractive_modifications_fail_closed() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
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
        let result = host
            .tool(
                &run,
                ToolCall {
                    call_id: "c".into(),
                    name: "demo_preview".into(),
                    arguments: json!({}),
                },
            )
            .await
            .unwrap();
        assert!(result.error.contains("non-interactive"));
        assert!(!directory.path().join("demo.txt").exists());
        assert!(host.approvals().await.is_empty());
        host.shutdown().await;
    }

    #[tokio::test]
    async fn approval_timing_is_call_bound_and_human_wait_is_not_execution_time() {
        for allow in [true, false] {
            let directory = crate::test_support::private_tempdir().unwrap();
            let host = host(directory.path());
            let session = host.store.create_session().await.unwrap();
            let run = host
                .start(
                    &session.id,
                    "demo hold",
                    &Uuid::new_v4().to_string(),
                    ApprovalMode::Interactive,
                )
                .await
                .unwrap();
            let mut events = host.events.subscribe();
            let owner = host.clone();
            let owned_run = run.clone();
            let wall = Instant::now();
            let operation = tokio::spawn(async move {
                owner
                    .tool(
                        &owned_run,
                        ToolCall {
                            call_id: "timed-call".into(),
                            name: "demo_preview".into(),
                            arguments: json!({}),
                        },
                    )
                    .await
            });
            tokio::time::timeout(Duration::from_secs(10), async {
                loop {
                    let record = events.recv().await.unwrap();
                    if matches!(record.event, Event::ToolApprovalTiming { ref call_id, waiting:true, .. } if call_id == "timed-call") { break; }
                }
            }).await.unwrap();
            tokio::time::sleep(Duration::from_millis(120)).await;
            let approval = host
                .approvals()
                .await
                .into_iter()
                .find(|approval| approval.call.call_id == "timed-call")
                .unwrap();
            host.decide(&run.id, &approval.id, allow).await.unwrap();
            let result = tokio::time::timeout(Duration::from_secs(10), operation)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            assert_eq!(result.error.is_empty(), allow);
            let records = host.store.events(&session.id, 0).await.unwrap();
            let snapshots: Vec<_> = records
                .iter()
                .filter_map(|record| match &record.event {
                    Event::ToolApprovalTiming {
                        call_id,
                        elapsed_ms,
                        waiting,
                    } if call_id == "timed-call" => Some((*elapsed_ms, *waiting)),
                    _ => None,
                })
                .collect();
            assert_eq!(snapshots.len(), 2);
            assert_eq!(snapshots[0], (snapshots[1].0, true));
            assert!(!snapshots[1].1);
            let duration = records
                .iter()
                .find_map(|record| match &record.event {
                    Event::ToolCompleted {
                        result,
                        duration_ms,
                    } if result.call_id == "timed-call" => Some(*duration_ms),
                    _ => None,
                })
                .unwrap();
            assert!(
                u128::from(duration) + 80 < wall.elapsed().as_millis(),
                "human wait leaked into active duration"
            );
            assert!(host.approvals().await.is_empty());
            host.shutdown().await;
        }
    }

    async fn held(host: &Arc<Host>) -> Run {
        let session = host.store.create_session().await.unwrap();
        host.start(
            &session.id,
            "demo hold",
            &Uuid::new_v4().to_string(),
            ApprovalMode::Deny,
        )
        .await
        .unwrap()
    }

    async fn child(host: &Arc<Host>, parent: &Run, id: &str, prompt: &str) -> Run {
        let result = host
            .tool(
                parent,
                ToolCall {
                    call_id: id.into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":prompt, "title":id, "background":true}),
                },
            )
            .await
            .unwrap();
        assert!(result.error.is_empty(), "{}", result.error);
        Run {
            id: result.result["task"]["run_id"].as_str().unwrap().into(),
            session_id: result.result["task"]["session_id"].as_str().unwrap().into(),
            request_id: String::new(),
            status: RunStatus::Running,
        }
    }

    async fn finished(host: &Host, run: &Run) {
        let receiver = host
            .active
            .lock()
            .await
            .get(&run.id)
            .map(|active| active.completed.clone());
        if let Some(mut receiver) = receiver {
            tokio::time::timeout(Duration::from_secs(5), async {
                while !*receiver.borrow_and_update() {
                    receiver.changed().await.unwrap();
                }
            })
            .await
            .unwrap();
        }
    }

    #[tokio::test]
    async fn foreground_results_are_direct_parent_scoped_and_children_cannot_become_roots() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let parent = held(&host).await;
        let foreign = held(&host).await;
        let result = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "delegate".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo child", "title":"Reviewer", "read_only":true}),
                },
            )
            .await
            .unwrap();
        assert!(result.error.is_empty(), "{}", result.error);
        assert_eq!(result.result["task"]["status"], "completed");
        assert!(
            result.result["result"]
                .as_str()
                .unwrap()
                .contains("OFFLINE DEMO")
        );
        let id = result.result["task"]["run_id"].as_str().unwrap();
        let session = result.result["task"]["session_id"].as_str().unwrap();
        assert!(
            host.start(
                session,
                "escalate",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive
            )
            .await
            .is_err()
        );
        assert_eq!(
            host.store.sessions().await.unwrap().len(),
            2,
            "Child sessions must not be listed as unrestricted roots"
        );
        let denied = host
            .tool(
                &foreign,
                ToolCall {
                    call_id: "foreign".into(),
                    name: "agent_status".into(),
                    arguments: json!({"run_id":id}),
                },
            )
            .await
            .unwrap();
        assert!(denied.error.contains("immediate owning run"));
        let events = host.store.events(&parent.session_id, 0).await.unwrap();
        assert!(
            events
                .iter()
                .any(|record| matches!(record.event, Event::TaskFinished { .. }))
        );
        assert!(
            !events
                .iter()
                .any(|record| matches!(record.event, Event::TextDelta { .. })),
            "Child text must not be forwarded as parent text"
        );
        let duplicate = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "delegate".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo hold"}),
                },
            )
            .await
            .unwrap();
        assert!(!duplicate.error.is_empty());
        assert_eq!(host.store.tasks(&parent.session_id).await.unwrap().len(), 1);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn depth_concurrency_and_execution_budgets_are_shared_and_not_refunded() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let mut config = Config {
            workspace: directory.path().into(),
            data_dir: directory.path().join("state"),
            backend: Backend::Demo,
            ..Config::default()
        };
        config.subagents.max_depth = 1;
        config.subagents.max_concurrent = 1;
        config.subagents.max_executions = 2;
        let host = Host::open(config.resolve().unwrap()).unwrap();
        let parent = held(&host).await;
        let first = child(&host, &parent, "first", "demo hold").await;
        let blocked = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "blocked".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo hold", "background":true}),
                },
            )
            .await
            .unwrap();
        assert!(blocked.error.contains("concurrency"));
        let nested = host
            .tool(
                &first,
                ToolCall {
                    call_id: "nested".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo child"}),
                },
            )
            .await
            .unwrap();
        assert!(nested.error.contains("depth"));
        host.cancel(&first.id).await.unwrap();
        finished(&host, &first).await;
        assert!(
            host.active().await.iter().any(|run| run.id == parent.id),
            "Cancelling a child must not cancel its parent"
        );
        let second = child(&host, &parent, "second", "demo hold").await;
        host.cancel(&second.id).await.unwrap();
        finished(&host, &second).await;
        let exhausted = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "third".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo child"}),
                },
            )
            .await
            .unwrap();
        assert!(exhausted.error.contains("execution budget"));
        assert_eq!(host.store.tasks(&parent.session_id).await.unwrap().len(), 2);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn permissions_only_tighten_and_parent_cancel_joins_the_entire_tree() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let parent = held(&host).await;
        let result = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "readonly".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo hold", "read_only":true, "background":true}),
                },
            )
            .await
            .unwrap();
        let child_run = Run {
            id: result.result["task"]["run_id"].as_str().unwrap().into(),
            session_id: result.result["task"]["session_id"].as_str().unwrap().into(),
            request_id: String::new(),
            status: RunStatus::Running,
        };
        let grandchild = child(&host, &child_run, "cannot-escalate", "demo hold").await;
        let context = host.execution(&grandchild).await.unwrap();
        assert!(context.read_only);
        assert!(
            host.catalog(&context)
                .iter()
                .all(|tool| !matches!(tool.name.as_str(), "write" | "edit" | "shell" | "to_agent"))
        );
        assert_eq!(
            host.store
                .task(&grandchild.id)
                .await
                .unwrap()
                .task
                .parent_run_id,
            child_run.id
        );
        let denied = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "not-direct".into(),
                    name: "agent_cancel".into(),
                    arguments: json!({"run_id":grandchild.id}),
                },
            )
            .await
            .unwrap();
        assert!(denied.error.contains("immediate owning run"));
        host.cancel(&parent.id).await.unwrap();
        finished(&host, &parent).await;
        assert!(
            host.active().await.is_empty(),
            "Parent terminal state must follow descendant cleanup"
        );
        assert!(
            host.store
                .tasks(&parent.session_id)
                .await
                .unwrap()
                .iter()
                .all(|task| task.status == RunStatus::Cancelled)
        );
        let root_records = host.store.events(&parent.session_id, 0).await.unwrap();
        assert!(matches!(
            root_records.last().unwrap().event,
            Event::RunFinished {
                status: RunStatus::Cancelled,
                ..
            }
        ));
        host.shutdown().await;
    }

    #[tokio::test]
    async fn deadlines_and_unjoined_work_fail_closed_without_orphans() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let mut config = Config {
            workspace: directory.path().into(),
            data_dir: directory.path().join("state"),
            backend: Backend::Demo,
            ..Config::default()
        };
        config.subagents.timeout_seconds = 1;
        let host = Host::open(config.resolve().unwrap()).unwrap();
        let parent = held(&host).await;
        let result = host
            .tool(
                &parent,
                ToolCall {
                    call_id: "deadline".into(),
                    name: "to_agent".into(),
                    arguments: json!({"prompt":"demo hold"}),
                },
            )
            .await
            .unwrap();
        assert_eq!(result.result["task"]["status"], "failed");
        assert!(
            result.result["error"]
                .as_str()
                .unwrap()
                .contains("deadline")
        );
        let session = host.store.create_session().await.unwrap();
        let orphan = host
            .start(
                &session.id,
                "demo orphan",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny,
            )
            .await
            .unwrap();
        finished(&host, &orphan).await;
        let records = host.store.events(&session.id, 0).await.unwrap();
        assert!(matches!(
            records.last().unwrap().event,
            Event::RunFinished {
                status: RunStatus::Failed,
                ..
            }
        ));
        assert!(
            host.store
                .tasks(&session.id)
                .await
                .unwrap()
                .iter()
                .all(|task| task.status != RunStatus::Running)
        );
        host.shutdown().await;
    }

    #[tokio::test]
    async fn concurrent_admission_is_atomic_and_other_roots_keep_their_own_budget() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let mut config = Config {
            workspace: directory.path().into(),
            data_dir: directory.path().join("state"),
            backend: Backend::Demo,
            ..Config::default()
        };
        config.subagents.max_concurrent = 1;
        let host = Host::open(config.resolve().unwrap()).unwrap();
        let first = held(&host).await;
        let second = held(&host).await;
        let call = |id: &str| ToolCall {
            call_id: id.into(),
            name: "to_agent".into(),
            arguments: json!({"prompt":"demo hold", "background":true}),
        };
        let (left, right) = tokio::join!(
            host.tool(&first, call("race-left")),
            host.tool(&first, call("race-right"))
        );
        let admitted = [left.unwrap(), right.unwrap()]
            .into_iter()
            .filter(|result| result.error.is_empty())
            .count();
        assert_eq!(admitted, 1);
        let independent = child(&host, &second, "independent", "demo hold").await;
        host.cancel(&first.id).await.unwrap();
        finished(&host, &first).await;
        assert!(
            host.active()
                .await
                .iter()
                .any(|run| run.id == independent.id)
        );
        assert_eq!(host.store.tasks(&first.session_id).await.unwrap().len(), 1);
        host.shutdown().await;
    }

    #[tokio::test]
    async fn steering_invalidations_are_durable_exact_root_and_do_not_reemit_on_retry() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let root = held(&host).await;
        let mut events = host.events.subscribe();
        host.negotiate_steering(&root, true).await.unwrap();
        let request = Uuid::new_v4().to_string();
        let receipt = host
            .steer(&root.id, "operator only", &request)
            .await
            .unwrap();
        host.steer(&root.id, "operator only", &request)
            .await
            .unwrap();
        host.steering_boundary(&root, "safe-batch", SteeringBoundary::AfterTools)
            .await
            .unwrap();
        host.steering_applied(&root, "safe-batch", &receipt.input_id)
            .await
            .unwrap();
        host.steering_boundary(&root, "sealed-final", SteeringBoundary::Final)
            .await
            .unwrap();
        let durable = host.store.events(&root.session_id, 0).await.unwrap();
        let updates: Vec<_> = durable
            .iter()
            .filter_map(|record| match &record.event {
                Event::SteeringUpdated { input_id } => Some((record.sequence, input_id.as_str())),
                _ => None,
            })
            .collect();
        assert_eq!(
            updates.iter().map(|(_, input)| *input).collect::<Vec<_>>(),
            vec![
                "",
                &receipt.input_id,
                &receipt.input_id,
                &receipt.input_id,
                ""
            ]
        );
        for (sequence, _) in updates {
            loop {
                let record = events.recv().await.unwrap();
                if record.sequence == sequence {
                    assert_eq!(record.run_id, root.id);
                    assert_eq!(record.session_id, root.session_id);
                    break;
                }
            }
        }
        assert!(!host.steering(&root.id).await.unwrap().accepting);
        let child = child(&host, &root, "no-child-steering-events", "demo hold").await;
        host.negotiate_steering(&child, true).await.unwrap();
        assert!(
            !host
                .store
                .events(&child.session_id, 0)
                .await
                .unwrap()
                .iter()
                .any(|record| matches!(record.event, Event::SteeringUpdated { .. }))
        );
        host.shutdown().await;
    }

    #[tokio::test]
    async fn steering_requires_capability_root_and_exact_activation_with_independent_receipts() {
        use crate::steering::SteeringStatus;
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let root = held(&host).await;
        let request = Uuid::new_v4().to_string();
        assert!(host.steer(&root.id, "first", &request).await.is_err());
        assert!(host.steering(&root.id).await.unwrap().receipts.is_empty());
        host.negotiate_steering(&root, true).await.unwrap();
        assert!(
            host.steer(&root.id, &"é".repeat(16_001), &Uuid::new_v4().to_string())
                .await
                .is_err()
        );
        assert!(
            host.steer(&root.id, " \n ", &Uuid::new_v4().to_string())
                .await
                .is_err()
        );
        let first = host.steer(&root.id, "first", &request).await.unwrap();
        assert_eq!(first.status, SteeringStatus::Pending);
        assert_eq!(
            host.steer(&root.id, "first", &request.to_uppercase())
                .await
                .unwrap(),
            first
        );
        assert_eq!(
            host.steer(&root.id, "first", &request).await.unwrap(),
            first
        );
        assert!(host.steer(&root.id, "changed", &request).await.is_err());
        let other = held(&host).await;
        host.negotiate_steering(&other, true).await.unwrap();
        assert!(host.steer(&other.id, "first", &request).await.is_err());
        assert!(
            host.steer("foreign-run", "first", &Uuid::new_v4().to_string())
                .await
                .is_err()
        );
        let child = child(&host, &root, "steering-child", "demo hold").await;
        host.negotiate_steering(&child, true).await.unwrap();
        assert!(
            host.steer(&child.id, "escalate", &Uuid::new_v4().to_string())
                .await
                .is_err()
        );
        assert!(host.steering(&child.id).await.unwrap().receipts.is_empty());
        let forged = Run {
            session_id: other.session_id.clone(),
            ..root.clone()
        };
        assert!(
            host.steering_boundary(&forged, "boundary", SteeringBoundary::AfterTools)
                .await
                .is_err()
        );
        let second = host
            .steer(&root.id, "second", &Uuid::new_v4().to_string())
            .await
            .unwrap();
        for expected in [&first, &second] {
            let boundary = Uuid::new_v4().to_string();
            let offered = host
                .steering_boundary(&root, &boundary, SteeringBoundary::AfterTools)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(offered, expected.input());
            assert!(
                host.steering_applied(&root, &boundary, "wrong-id")
                    .await
                    .is_err()
            );
            assert!(
                host.steering_applied(&other, &boundary, &offered.input_id)
                    .await
                    .is_err()
            );
            assert!(
                host.steering_applied(&root, "wrong-boundary", &offered.input_id)
                    .await
                    .is_err()
            );
            host.steering_applied(&root, &boundary, &offered.input_id)
                .await
                .unwrap();
            assert!(
                host.steering_applied(&root, &boundary, &offered.input_id)
                    .await
                    .is_err()
            );
        }
        let receipts = host.steering(&root.id).await.unwrap().receipts;
        assert_eq!(
            receipts
                .iter()
                .map(|receipt| receipt.input_id.as_str())
                .collect::<Vec<_>>(),
            [first.input_id, second.input_id]
        );
        assert!(
            receipts
                .iter()
                .all(|receipt| receipt.status == SteeringStatus::Applied)
        );
        assert!(
            host.queue(&root.session_id)
                .await
                .unwrap()
                .entries
                .is_empty()
        );
        host.shutdown().await;
    }

    #[tokio::test]
    async fn final_empty_seals_admission_before_the_reply_can_be_written() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        for _ in 0..16 {
            let run = held(&host).await;
            host.negotiate_steering(&run, true).await.unwrap();
            let request = Uuid::new_v4().to_string();
            // Exercise both contenders under the same admission mutex. There is
            // intentionally no runtime writer here: closure must precede IO.
            let gate = Arc::new(tokio::sync::Barrier::new(2));
            let owner = host.clone();
            let identity = run.clone();
            let start = gate.clone();
            let boundary = tokio::spawn(async move {
                start.wait().await;
                owner
                    .steering_boundary(
                        &identity,
                        &Uuid::new_v4().to_string(),
                        SteeringBoundary::Final,
                    )
                    .await
            });
            gate.wait().await;
            let admitted = host.steer(&run.id, "racing", &request).await;
            let offered = boundary.await.unwrap().unwrap();
            if let Some(offered) = offered {
                let receipt = admitted.unwrap();
                assert_eq!(receipt.input_id, offered.input_id);
                let state = host.steering(&run.id).await.unwrap();
                host.steering_applied(&run, &state.receipts[0].boundary_id, &offered.input_id)
                    .await
                    .unwrap();
                assert!(
                    host.steering_boundary(
                        &run,
                        &Uuid::new_v4().to_string(),
                        SteeringBoundary::Final
                    )
                    .await
                    .unwrap()
                    .is_none()
                );
            } else {
                assert!(admitted.is_err());
                assert!(host.steering(&run.id).await.unwrap().receipts.is_empty());
            }
            assert!(!host.steering(&run.id).await.unwrap().accepting);
            assert!(
                host.steer(&run.id, "too late", &Uuid::new_v4().to_string())
                    .await
                    .is_err()
            );
            host.cancel(&run.id).await.unwrap();
            finished(&host, &run).await;
        }
        host.shutdown().await;
    }

    #[tokio::test]
    async fn cancellation_seals_and_settles_without_rebinding_or_runtime_delivery() {
        use crate::steering::SteeringStatus;
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let root = held(&host).await;
        host.negotiate_steering(&root, true).await.unwrap();
        let offered = host
            .steer(&root.id, "possibly persisted", &Uuid::new_v4().to_string())
            .await
            .unwrap();
        let pending = host
            .steer(&root.id, "not delivered", &Uuid::new_v4().to_string())
            .await
            .unwrap();
        let boundary = Uuid::new_v4().to_string();
        host.steering_boundary(&root, &boundary, SteeringBoundary::AfterTools)
            .await
            .unwrap();
        host.cancel(&root.id).await.unwrap();
        assert!(
            host.steer(&root.id, "cancel race", &Uuid::new_v4().to_string())
                .await
                .is_err()
        );
        assert!(
            host.steering_applied(&root, &boundary, &offered.input_id)
                .await
                .is_err()
        );
        finished(&host, &root).await;
        let state = host.steering(&root.id).await.unwrap();
        assert_eq!(state.receipts[0].status, SteeringStatus::Uncertain);
        assert_eq!(state.receipts[1].status, SteeringStatus::Undelivered);
        assert_eq!(
            host.steer(&root.id, &pending.prompt, &pending.request_id)
                .await
                .unwrap()
                .input_id,
            pending.input_id
        );
        host.control_queue(&root.session_id, QueueAction::Resume)
            .await
            .unwrap();
        let next = host
            .start(
                &root.session_id,
                "demo hold",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny,
            )
            .await
            .unwrap();
        host.negotiate_steering(&next, true).await.unwrap();
        assert!(
            host.steering_boundary(&next, &Uuid::new_v4().to_string(), SteeringBoundary::Final)
                .await
                .unwrap()
                .is_none()
        );
        host.shutdown().await;
    }
}
