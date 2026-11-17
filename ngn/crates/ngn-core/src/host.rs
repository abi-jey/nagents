use std::{
    collections::HashMap,
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Context, Result, ensure};
use ngn_protocol::{Event, EventRecord, RunStatus, ToolCall, ToolDefinition, ToolResult};
use serde::Serialize;
use serde_json::Value;
use tokio::sync::{Mutex, broadcast, oneshot, watch};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::{
    Config,
    config::Backend,
    queues::{QueueAction, QueueState},
    runner::{self, RunOutput},
    store::{Run, Store},
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

struct ActiveRun {
    execution: Execution,
    accepting_children: bool,
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

pub struct Host {
    pub config: Config,
    pub store: Store,
    pub tools: Tools,
    pub events: broadcast::Sender<EventRecord>,
    active: Mutex<HashMap<String, ActiveRun>>,
    pending: Mutex<HashMap<String, Pending>>,
    workers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    emission: Mutex<()>,
    closed: AtomicBool,
}

impl Host {
    pub fn open(config: Config) -> Result<Arc<Self>> {
        let store = Store::open(&config.state_dir())?;
        let protected = config.data_dir.canonicalize()?;
        let tools = Tools::open(
            &config.workspace,
            &protected,
            config.read_only,
            config.backend == Backend::Demo,
            config.shell_timeout_seconds,
        )?;
        let (events, _) = broadcast::channel(1024);
        Ok(Arc::new(Self {
            config,
            store,
            tools,
            events,
            active: Mutex::new(HashMap::new()),
            pending: Mutex::new(HashMap::new()),
            workers: Mutex::new(Vec::new()),
            emission: Mutex::new(()),
            closed: AtomicBool::new(false),
        }))
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

    /// Accept an input exactly once. Busy or paused sessions retain it durably.
    pub async fn submit(
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
        self.root_session(session).await?;
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
        if self.config.backend == Backend::Nagents {
            self.config.selected_provider()?;
        }
        {
            let _emission = self.emission.lock().await;
            let (_, record) = self
                .store
                .enqueue(
                    session,
                    prompt,
                    request_id,
                    approval == ApprovalMode::Interactive,
                )
                .await?;
            let _ = self.events.send(record);
        }
        self.dispatch(&mut active).await?;
        self.store
            .existing_request(session, request_id, prompt)
            .await?
            .context("Accepted input is missing")
    }

    pub async fn control_queue(
        self: &Arc<Self>,
        session: &str,
        action: QueueAction,
    ) -> Result<QueueState> {
        self.root_session(session).await?;
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
        };
        let (completed, receiver) = watch::channel(false);
        active.insert(
            run.id.clone(),
            ActiveRun {
                execution: execution.clone(),
                accepting_children: true,
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
            let mut output = {
                let runtime = async {
                    match self.config.backend {
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
                    result = &mut runtime => match result { Ok(output) => output, Err(error) => RunOutput::failed(error.to_string()) },
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
            active.remove(&execution.run.id);
            let _ = completed.send(true);
            match records {
                Ok(records) => {
                    for record in records {
                        let _ = self.events.send(record);
                    }
                }
                Err(_) => eprintln!(
                    "ngn: unable to persist final run status; it will be marked interrupted on restart"
                ),
            }
            drop(_emission);
            if self.dispatch(&mut active).await.is_err() {
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
        let active = self.active.lock().await;
        let _emission = self.emission.lock().await;
        if let Some(owner) = active.get(run) {
            if owner.execution.task.is_none() {
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
        };
        let (completed, receiver) = watch::channel(false);
        active.insert(
            run.id.clone(),
            ActiveRun {
                execution: execution.clone(),
                accepting_children: true,
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
    ) -> Result<TaskOutcome> {
        let outcome = self.owned_task(owner, run_id).await?;
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

    pub async fn tool(self: &Arc<Self>, run: &Run, call: ToolCall) -> Result<ToolResult> {
        let owner = self.execution(run).await?;
        ensure!(!owner.cancel.is_cancelled(), "Run was cancelled");
        ensure!(
            !call.call_id.is_empty() && call.call_id.len() <= 200 && call.arguments.is_object(),
            "Invalid tool call"
        );
        self.emit(run, Event::ToolStarted { call: call.clone() })
            .await?;
        let mut clock = ToolClock::new(Instant::now());
        let result: Result<Value> = async {
            match call.name.as_str() {
                "to_agent" => {
                    let input: DelegateInput = serde_json::from_value(call.arguments.clone())?;
                    let background = input.background;
                    let task = self.delegate(&owner, &call, input).await?;
                    if background { return Ok(serde_json::to_value(task)?); }
                    return Ok(serde_json::to_value(self.wait_task(&owner, &task.task.run_id, None).await?)?);
                }
                "agent_status" | "agent_cancel" => {
                    let input: TaskInput = serde_json::from_value(call.arguments.clone())?;
                    let task = self.owned_task(&owner, &input.run_id).await?;
                    if call.name == "agent_cancel" && task.task.status == RunStatus::Running {
                        let _ = self.cancel(&input.run_id).await;
                        return Ok(serde_json::to_value(self.wait_task(&owner, &input.run_id, None).await?)?);
                    }
                    return Ok(serde_json::to_value(self.owned_task(&owner, &input.run_id).await?)?);
                }
                "agent_wait" => {
                    let input: WaitInput = serde_json::from_value(call.arguments.clone())?;
                    ensure!((1..=60).contains(&input.timeout_seconds), "Wait timeout must be 1..60 seconds");
                    return Ok(serde_json::to_value(self.wait_task(&owner, &input.run_id, Some(Duration::from_secs(input.timeout_seconds))).await?)?);
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
            tools.execute(prepared, owner.cancel.clone()).await
        }.await;
        let (result, error) = match result {
            Ok(result) => (result, String::new()),
            Err(error) => (Value::Null, error.to_string()),
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
        self.closed.store(true, Ordering::Release);
        let active = self.active.lock().await;
        {
            let _emission = self.emission.lock().await;
            match self.store.pause_pending().await {
                Ok(records) => {
                    for record in records {
                        let _ = self.events.send(record);
                    }
                }
                Err(_) => eprintln!(
                    "ngn: unable to persist paused queues; recovery will pause unstarted inputs"
                ),
            }
        }
        for active in active.values() {
            active.execution.cancel.cancel();
        }
        drop(active);
        let workers = std::mem::take(&mut *self.workers.lock().await);
        for mut worker in workers {
            if tokio::time::timeout(Duration::from_secs(15), &mut worker)
                .await
                .is_err()
            {
                worker.abort();
                let _ = worker.await;
            }
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
        let directory = tempfile::tempdir().unwrap();
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
        let directory = tempfile::tempdir().unwrap();
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
            let directory = tempfile::tempdir().unwrap();
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
        let directory = tempfile::tempdir().unwrap();
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
        let directory = tempfile::tempdir().unwrap();
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
        let directory = tempfile::tempdir().unwrap();
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
        let directory = tempfile::tempdir().unwrap();
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
        let directory = tempfile::tempdir().unwrap();
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
}
