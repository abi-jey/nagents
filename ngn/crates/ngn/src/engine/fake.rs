//! A scriptable, in-memory engine for client tests.
//!
//! It models only what the terminal client observes: sessions, committed
//! records, the per-session FIFO, the active root, steering receipts, pending
//! approvals and child tasks. Tests script the outcomes they need and assert on
//! the recorded calls instead of on engine internals.

use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    future::Future,
    sync::{Mutex, MutexGuard},
};

use anyhow::{Result, anyhow, bail};
use futures_util::{Stream, stream};

use super::{
    Approval, ChildTextPhase, ChildToolDispatch, ClientDisplay, ConversationActivity, Engine,
    HistoryPage, InputAdmission, InputReceipt, LogPaths, QueueAction, QueueEntry, QueueState, Run,
    Session, SessionSelection, SteeringReceipt, SteeringState, SteeringStatus, Submission,
    TaskIdentity, Update,
};
use crate::model::{Event, EventRecord, RunStatus, TaskInfo, VERSION};

/// The text the fake uses when an idle send finds a running root.
pub(crate) const BUSY: &str =
    "This session is busy or paused. An active turn started before this idle send";
/// The error recorded on a denied tool call.
pub(crate) const DENIED: &str = "Tool approval denied, expired or cancelled";

/// A scripted outcome for the next `submit`, consumed in order.
pub(crate) enum ScriptedSubmit {
    /// Admit durably but report a dispatch failure; the input stays queued.
    Warn(String),
    /// Refuse admission without recording the request.
    Reject { error: String, idle_changed: bool },
    /// The outcome is unknown; nothing is recorded.
    Unconfirmed(String),
}

/// A client call with its exact arguments.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Call {
    Submit {
        session: String,
        prompt: String,
        request: String,
        idle: bool,
    },
    InputReceipt {
        session: String,
        request: String,
    },
    ControlQueue {
        session: String,
        action: QueueAction,
    },
    Steer {
        run: String,
        prompt: String,
        request: String,
    },
    Decide {
        run: String,
        approval: String,
        call: String,
        allow: bool,
    },
    Cancel {
        run: String,
    },
}

struct Input {
    run: String,
    session: String,
    prompt: String,
    request: String,
    status: RunStatus,
}

#[derive(Default)]
struct Steering {
    supported: bool,
    accepting: bool,
    receipts: Vec<(String, String, String)>,
}

#[derive(Default)]
struct State {
    sessions: Vec<Session>,
    records: Vec<EventRecord>,
    inputs: Vec<Input>,
    queued: Vec<String>,
    /// Admitted runs whose dispatch failed; they block the FIFO behind them.
    held: BTreeSet<String>,
    paused: BTreeSet<String>,
    steering: BTreeMap<String, Steering>,
    approvals: Vec<Approval>,
    tasks: Vec<TaskInfo>,
    dispatches: Vec<ChildToolDispatch>,
    submit_script: VecDeque<ScriptedSubmit>,
    calls: Vec<Call>,
    next_id: u64,
}

impl State {
    fn id(&mut self, prefix: &str) -> String {
        self.next_id += 1;
        format!("{prefix}-{:08x}-{}", self.next_id, uuid::Uuid::new_v4())
    }

    fn emit(&mut self, session: &str, run: &str, event: Event) -> EventRecord {
        let record = EventRecord {
            schema_version: VERSION,
            sequence: self.records.last().map_or(1, |last| last.sequence + 1),
            run_id: run.into(),
            session_id: session.into(),
            timestamp_ms: 0,
            event,
        };
        self.records.push(record.clone());
        record
    }

    fn input(&mut self, run: &str) -> Option<&mut Input> {
        self.inputs.iter_mut().find(|input| input.run == run)
    }

    fn running(&self, session: &str) -> Option<&Input> {
        self.inputs
            .iter()
            .find(|input| input.session == session && input.status == RunStatus::Running)
    }

    fn start(&mut self, run: &str) {
        self.held.remove(run);
        self.queued.retain(|queued| queued != run);
        let input = self.input(run).expect("known run");
        input.status = RunStatus::Running;
        let (session, prompt) = (input.session.clone(), input.prompt.clone());
        self.emit(&session, run, Event::RunStarted { prompt });
    }

    /// Start the FIFO head when the session is idle and not paused.
    fn dispatch(&mut self, session: &str) {
        if self.paused.contains(session) || self.running(session).is_some() {
            return;
        }
        let head = self.queued.iter().find(|run| {
            self.inputs
                .iter()
                .any(|input| &input.run == *run && input.session == session)
        });
        if let Some(run) = head.cloned()
            && !self.held.contains(&run)
        {
            self.start(&run);
        }
    }

    fn finish(&mut self, run: &str, status: RunStatus) {
        self.queued.retain(|queued| queued != run);
        let input = self.input(run).expect("known run");
        input.status = status;
        let session = input.session.clone();
        if let Some(steering) = self.steering.get_mut(run) {
            steering.accepting = false;
        }
        self.emit(
            &session,
            run,
            Event::RunFinished {
                status,
                error: String::new(),
            },
        );
    }

    fn receipt(input: &Input) -> InputReceipt {
        InputReceipt {
            run_id: Some(input.run.clone()),
            request_id: input.request.clone(),
            status: Some(input.status),
        }
    }

    fn admit(
        &mut self,
        session: &str,
        prompt: &str,
        request: &str,
        dispatch: bool,
    ) -> InputReceipt {
        let run = self.id("run");
        self.inputs.push(Input {
            run: run.clone(),
            session: session.into(),
            prompt: prompt.into(),
            request: request.into(),
            status: RunStatus::Queued,
        });
        self.queued.push(run.clone());
        self.emit(
            session,
            &run,
            Event::RunQueued {
                prompt: prompt.into(),
            },
        );
        if dispatch {
            self.dispatch(session);
        } else {
            self.held.insert(run.clone());
        }
        Self::receipt(self.input(&run).expect("admitted run"))
    }
}

/// The in-memory engine. All state sits behind one lock that is never held
/// across an await.
#[derive(Default)]
pub(crate) struct FakeEngine {
    display: ClientDisplay,
    state: Mutex<State>,
}

impl FakeEngine {
    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().expect("fake engine state")
    }

    pub(crate) fn create_session(&self) -> String {
        let mut state = self.state();
        let id = state.id("session");
        state.sessions.push(Session {
            id: id.clone(),
            title: String::new(),
            updated_at_ms: 0,
        });
        id
    }

    pub(crate) fn session_count(&self) -> usize {
        self.state().sessions.len()
    }

    /// Admit and immediately run a root, bypassing the FIFO.
    pub(crate) fn start(&self, session: &str, prompt: &str) -> Run {
        let mut state = self.state();
        let request = uuid::Uuid::new_v4().to_string();
        let receipt = state.admit(session, prompt, &request, false);
        let run = receipt.run_id.expect("fake receipts carry a run");
        state.start(&run);
        Run {
            id: run,
            session_id: session.into(),
            request_id: request,
            status: RunStatus::Running,
        }
    }

    pub(crate) fn set_steering(&self, run: &str, supported: bool, accepting: bool) {
        let mut state = self.state();
        let steering = state.steering.entry(run.into()).or_default();
        steering.supported = supported;
        steering.accepting = accepting;
    }

    pub(crate) fn script_submit(&self, outcome: ScriptedSubmit) {
        self.state().submit_script.push_back(outcome);
    }

    pub(crate) fn emit(&self, session: &str, run: &str, event: Event) -> EventRecord {
        self.state().emit(session, run, event)
    }

    pub(crate) fn add_task(&self, task: TaskInfo) {
        let mut state = self.state();
        let input = Input {
            run: task.run_id.clone(),
            session: task.session_id.clone(),
            prompt: task.title.clone(),
            request: uuid::Uuid::new_v4().to_string(),
            status: task.status,
        };
        state.inputs.push(input);
        state.emit(
            &task.root_session_id.clone(),
            &task.root_run_id.clone(),
            Event::TaskStarted { task: task.clone() },
        );
        state.tasks.push(task);
    }

    pub(crate) fn finish_task(&self, run: &str, status: RunStatus) {
        let mut state = self.state();
        state.finish(run, status);
        let Some(task) = state.tasks.iter_mut().find(|task| task.run_id == run) else {
            return;
        };
        task.status = status;
        let task = task.clone();
        state.emit(
            &task.root_session_id.clone(),
            &task.root_run_id.clone(),
            Event::TaskFinished {
                task,
                error: String::new(),
            },
        );
    }

    pub(crate) fn request_approval(&self, approval: Approval) {
        let mut state = self.state();
        state.emit(
            &approval.session_id,
            &approval.run_id,
            Event::ApprovalRequested {
                approval_id: approval.id.clone(),
                call: approval.call.clone(),
                preview: approval.preview.clone(),
            },
        );
        state.approvals.push(approval);
    }

    pub(crate) fn calls(&self) -> Vec<Call> {
        self.state().calls.clone()
    }

    pub(crate) fn active(&self) -> Vec<String> {
        self.state()
            .inputs
            .iter()
            .filter(|input| input.status == RunStatus::Running)
            .map(|input| input.run.clone())
            .collect()
    }

    pub(crate) fn steering_prompts(&self, run: &str) -> Vec<String> {
        self.state()
            .steering
            .get(run)
            .map(|steering| {
                steering
                    .receipts
                    .iter()
                    .map(|(_, prompt, _)| prompt.clone())
                    .collect()
            })
            .unwrap_or_default()
    }
}

impl Engine for FakeEngine {
    fn display(&self) -> &ClientDisplay {
        &self.display
    }

    fn log_paths(&self) -> Option<LogPaths<'_>> {
        None
    }

    async fn subscribe(&self) -> Result<impl Stream<Item = Update> + Send> {
        Ok(stream::pending())
    }

    async fn open_session(&self, selection: SessionSelection<'_>) -> Result<String> {
        match selection {
            SessionSelection::Resume(id) => {
                if self.state().sessions.iter().any(|session| session.id == id) {
                    return Ok(id.into());
                }
                bail!("Unknown session {id}");
            }
            SessionSelection::LatestOrNew => {
                if let Some(session) = self.state().sessions.last() {
                    return Ok(session.id.clone());
                }
            }
            SessionSelection::New => (),
        }
        Ok(self.create_session())
    }

    async fn sessions(&self) -> Result<Vec<Session>> {
        Ok(self.state().sessions.clone())
    }

    async fn history(&self, session: &str, after: i64) -> Result<HistoryPage> {
        let records: Vec<_> = self
            .state()
            .records
            .iter()
            .filter(|record| record.session_id == session && record.sequence > after)
            .cloned()
            .collect();
        Ok(HistoryPage {
            cursor: records.last().map_or(after, |record| record.sequence),
            records,
            has_more: false,
        })
    }

    async fn conversation_activity(&self, session: &str) -> Result<ConversationActivity> {
        let state = self.state();
        let child_text = state
            .tasks
            .iter()
            .filter(|task| task.root_session_id == session && task.status == RunStatus::Running)
            .filter_map(|task| {
                state.records.iter().rev().find_map(|record| {
                    let responding = match record.event {
                        Event::TextDelta { .. } => true,
                        Event::TextCompleted { .. } => false,
                        _ => return None,
                    };
                    (record.session_id == task.session_id && record.run_id == task.run_id).then(
                        || ChildTextPhase {
                            run_id: task.run_id.clone(),
                            session_id: task.session_id.clone(),
                            sequence: record.sequence,
                            responding,
                        },
                    )
                })
            })
            .collect();
        Ok(ConversationActivity {
            active: state.running(session).map(|input| Run {
                id: input.run.clone(),
                session_id: input.session.clone(),
                request_id: input.request.clone(),
                status: input.status,
            }),
            child_text,
            approvals: state.approvals.clone(),
        })
    }

    async fn child_tool_dispatches(&self) -> Result<Vec<ChildToolDispatch>> {
        Ok(self.state().dispatches.clone())
    }

    async fn tasks(&self, session: &str) -> Result<Option<Vec<TaskInfo>>> {
        Ok(Some(
            self.state()
                .tasks
                .iter()
                .filter(|task| task.root_session_id == session)
                .cloned()
                .collect(),
        ))
    }

    fn task_activity_tail(
        &self,
        targets: Vec<TaskIdentity>,
    ) -> impl Future<Output = Result<Vec<Vec<EventRecord>>>> + Send + 'static {
        let state = self.state();
        let tails = targets
            .into_iter()
            .map(|(session, run)| {
                let mut records: Vec<_> = state
                    .records
                    .iter()
                    .filter(|record| record.session_id == session && record.run_id == run)
                    .cloned()
                    .collect();
                records.drain(..records.len().saturating_sub(8));
                records
            })
            .collect();
        std::future::ready(Ok(tails))
    }

    async fn submit(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
        submission: Submission,
    ) -> InputAdmission {
        let mut state = self.state();
        let idle = matches!(submission, Submission::Idle);
        state.calls.push(Call::Submit {
            session: session.into(),
            prompt: prompt.into(),
            request: request.into(),
            idle,
        });
        let warning = match state.submit_script.pop_front() {
            Some(ScriptedSubmit::Reject {
                error,
                idle_changed,
            }) => {
                return InputAdmission::Rejected {
                    error: anyhow!(error),
                    idle_changed,
                };
            }
            Some(ScriptedSubmit::Unconfirmed(error)) => {
                return InputAdmission::Unconfirmed(anyhow!(error));
            }
            Some(ScriptedSubmit::Warn(warning)) => Some(anyhow!(warning)),
            None => None,
        };
        // Admission is idempotent by exact request identity.
        if let Some(input) = state
            .inputs
            .iter()
            .find(|input| input.session == session && input.request == request)
        {
            if input.prompt != prompt {
                return InputAdmission::Rejected {
                    error: anyhow!("Request {request} was admitted with different text"),
                    idle_changed: false,
                };
            }
            return InputAdmission::Accepted {
                receipt: State::receipt(input),
                warning,
            };
        }
        if idle && state.running(session).is_some() {
            return InputAdmission::Rejected {
                error: anyhow!(BUSY),
                idle_changed: true,
            };
        }
        let receipt = state.admit(session, prompt, request, warning.is_none());
        InputAdmission::Accepted { receipt, warning }
    }

    async fn input_receipt(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
    ) -> Result<Option<InputReceipt>> {
        let mut state = self.state();
        state.calls.push(Call::InputReceipt {
            session: session.into(),
            request: request.into(),
        });
        Ok(state
            .inputs
            .iter()
            .find(|input| {
                input.session == session && input.request == request && input.prompt == prompt
            })
            .map(State::receipt))
    }

    async fn queue(&self, session: &str) -> Result<QueueState> {
        let state = self.state();
        Ok(QueueState {
            paused: state.paused.contains(session),
            entries: state
                .queued
                .iter()
                .filter_map(|run| state.inputs.iter().find(|input| &input.run == run))
                .filter(|input| input.session == session)
                .map(|input| QueueEntry {
                    prompt: input.prompt.clone(),
                    request_id: input.request.clone(),
                    run_id: Some(input.run.clone()),
                })
                .collect(),
        })
    }

    async fn control_queue(&self, session: &str, action: QueueAction) -> Result<()> {
        let mut state = self.state();
        state.calls.push(Call::ControlQueue {
            session: session.into(),
            action,
        });
        match action {
            QueueAction::Pause => {
                state.paused.insert(session.into());
            }
            QueueAction::Resume => {
                state.paused.remove(session);
                state.dispatch(session);
            }
            QueueAction::Clear => {
                // Clearing pauses, so nothing new starts behind the operator.
                state.paused.insert(session.into());
                let cleared: Vec<_> = state
                    .queued
                    .iter()
                    .filter(|run| {
                        state
                            .inputs
                            .iter()
                            .any(|input| &input.run == *run && input.session == session)
                    })
                    .cloned()
                    .collect();
                for run in cleared {
                    state.finish(&run, RunStatus::Cancelled);
                }
            }
        }
        let paused = state.paused.contains(session);
        state.emit(session, "", Event::QueueUpdated { paused });
        Ok(())
    }

    async fn steering(&self, run: &str) -> Result<SteeringState> {
        let state = self.state();
        let input = state
            .inputs
            .iter()
            .find(|input| input.run == run)
            .ok_or_else(|| anyhow!("Unknown run {run}"))?;
        let steering = state.steering.get(run);
        Ok(SteeringState {
            run_id: run.into(),
            session_id: input.session.clone(),
            supported: steering.is_some_and(|steering| steering.supported),
            accepting: input.status == RunStatus::Running
                && steering.is_some_and(|steering| steering.accepting),
            receipts: steering
                .map(|steering| {
                    (1..)
                        .zip(&steering.receipts)
                        .map(|(position, (input_id, prompt, _))| SteeringReceipt {
                            input_id: input_id.clone(),
                            run_id: run.into(),
                            prompt: prompt.clone(),
                            position,
                            status: SteeringStatus::Pending,
                        })
                        .collect()
                })
                .unwrap_or_default(),
        })
    }

    async fn steer(&self, run: &str, prompt: &str, request: &str) -> Result<SteeringStatus> {
        let mut state = self.state();
        state.calls.push(Call::Steer {
            run: run.into(),
            prompt: prompt.into(),
            request: request.into(),
        });
        let Some(session) = state
            .inputs
            .iter()
            .find(|input| input.run == run && input.status == RunStatus::Running)
            .map(|input| input.session.clone())
        else {
            bail!("Run {run} is not the exact running root");
        };
        let steering = state.steering.entry(run.into()).or_default();
        if !steering.supported {
            bail!("Run {run} has not negotiated safe_steering");
        }
        if !steering.accepting {
            bail!("Run {run} is not accepting steering");
        }
        if !steering
            .receipts
            .iter()
            .any(|(_, _, saved)| saved == request)
        {
            let input_id = format!("steer-{request}");
            steering
                .receipts
                .push((input_id.clone(), prompt.into(), request.into()));
            state.emit(&session, run, Event::SteeringUpdated { input_id });
        }
        Ok(SteeringStatus::Pending)
    }

    async fn decide(&self, run: &str, approval: &str, call: &str, allow: bool) -> Result<()> {
        let mut state = self.state();
        state.calls.push(Call::Decide {
            run: run.into(),
            approval: approval.into(),
            call: call.into(),
            allow,
        });
        let Some(index) = state.approvals.iter().position(|pending| {
            pending.id == approval && pending.run_id == run && pending.call.call_id == call
        }) else {
            bail!("Approval {approval} is not pending for run {run} call {call}");
        };
        let pending = state.approvals.remove(index);
        state.emit(
            &pending.session_id,
            run,
            Event::ApprovalResolved {
                approval_id: approval.into(),
                allowed: allow,
            },
        );
        if !allow {
            state.emit(
                &pending.session_id,
                run,
                Event::ToolCompleted {
                    result: crate::model::ToolResult {
                        call_id: call.into(),
                        name: pending.call.name,
                        result: serde_json::Value::Null,
                        error: DENIED.into(),
                    },
                    duration_ms: 0,
                },
            );
        }
        Ok(())
    }

    async fn cancel(&self, run: &str) -> Result<()> {
        let mut state = self.state();
        state.calls.push(Call::Cancel { run: run.into() });
        let Some(input) = state
            .input(run)
            .filter(|input| matches!(input.status, RunStatus::Running | RunStatus::Queued))
        else {
            bail!("Run {run} is not active");
        };
        let session = input.session.clone();
        state.finish(run, RunStatus::Cancelled);
        state.dispatch(&session);
        Ok(())
    }

    async fn shutdown(&self) {}
}
