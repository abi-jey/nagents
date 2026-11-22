//! The Python engine client. The caller owns attachment and any process lease.

#[cfg(test)]
mod tests;

use std::{future::Future, time::Duration};

use anyhow::{Context, Result, ensure};
use futures_util::{Stream, future::try_join_all, stream};
use ngn_api::{Client, Error, Events, models as wire};
use ngn_protocol::{Event, EventRecord, RunStatus, TaskInfo, ToolCall, ToolResult, VERSION};
use reqwest::StatusCode;
use tokio::time::Instant;

use super::{
    Approval, ChildToolDispatch, ClientDisplay, ConversationActivity, Delivery, Engine, FeedNotice,
    HistoryPage, InputAdmission, InputReceipt, LiveUpdate, LogPaths, ProviderDisplay, QueueAction,
    QueueEntry, QueueState, Run, Session, SessionSelection, SteeringReceipt, SteeringState,
    SteeringStatus, Submission, TaskIdentity, Update,
};

const PAGE_LIMIT: u64 = 256;
const READ_TIMEOUT: Duration = Duration::from_secs(4);
const RECONNECT_TIMEOUT: Duration = Duration::from_secs(3);
const RECONNECT_ATTEMPTS: u32 = 5;

pub(crate) struct Remote {
    client: Client,
    display: ClientDisplay,
    tasks_available: bool,
}

impl Remote {
    /// `Client::connect` has already checked the engine protocol version.
    pub(crate) async fn new(client: Client) -> Result<Self> {
        let (config, info) = tokio::try_join!(read(client.config()), read(client.info()))?;
        if info.protocol_version != ngn_api::PROTOCOL_VERSION {
            return Err(Error::Incompatible {
                client: ngn_api::PROTOCOL_VERSION,
                engine: info.protocol_version,
            }
            .into());
        }
        let provider = if config.provider.is_empty() {
            ProviderDisplay::Unselected
        } else {
            let kind = read(client.providers())
                .await?
                .into_iter()
                .find(|provider| provider.id == config.provider)
                .map_or_else(|| config.provider.clone(), |provider| provider.kind);
            ProviderDisplay::Configured {
                id: config.provider,
                kind,
                model: config.model,
                // The public config does not disclose credential mode.
                auth: None,
            }
        };
        Ok(Self {
            client,
            display: ClientDisplay {
                workspace: config.workspace.into(),
                read_only: config.read_only,
                provider,
                ..ClientDisplay::default()
            },
            tasks_available: info
                .capabilities
                .iter()
                .any(|capability| capability == "tasks"),
        })
    }
}

impl Engine for Remote {
    fn display(&self) -> &ClientDisplay {
        &self.display
    }

    fn log_paths(&self) -> Option<LogPaths<'_>> {
        None
    }

    fn refresh_interval(&self) -> Duration {
        Duration::from_secs(1)
    }

    async fn subscribe(&self) -> Result<impl Stream<Item = Update> + Send> {
        let events = self.client.subscribe(0, None).await?;
        Ok(stream::unfold(
            Feed {
                client: self.client.clone(),
                events: Some(events),
                after: 0,
                retries: 0,
                connected_at: Instant::now(),
                ended: false,
            },
            |mut feed| async move {
                let update = feed.next().await?;
                Some((update, feed))
            },
        ))
    }

    async fn open_session(&self, selection: SessionSelection<'_>) -> Result<String> {
        match selection {
            SessionSelection::Resume(id) => {
                let session = read(self.client.session(id)).await?;
                ensure!(session.id == id, "Engine returned a different session");
                return Ok(session.id);
            }
            SessionSelection::LatestOrNew => {
                let latest = read(self.client.sessions())
                    .await?
                    .into_iter()
                    .filter(|session| !session.archived)
                    .reduce(|latest, candidate| {
                        if candidate.updated_at_ms > latest.updated_at_ms {
                            candidate
                        } else {
                            latest
                        }
                    });
                if let Some(session) = latest {
                    return Ok(session.id);
                }
            }
            SessionSelection::New => (),
        }
        Ok(self
            .client
            .create_session(&wire::SessionCreate { title: None })
            .await?
            .id)
    }

    async fn sessions(&self) -> Result<Vec<Session>> {
        Ok(read(self.client.sessions())
            .await?
            .into_iter()
            .map(|session| Session {
                id: session.id,
                title: session.title,
                updated_at_ms: session.updated_at_ms,
            })
            .collect())
    }

    async fn history(&self, session: &str, after: i64) -> Result<HistoryPage> {
        history(&self.client, session, after, PAGE_LIMIT).await
    }

    async fn conversation_activity(&self, session: &str) -> Result<ConversationActivity> {
        let state = read(self.client.snapshot()).await?;
        Ok(ConversationActivity {
            active: state
                .runs
                .into_iter()
                .find(|run| run.session_id == session && run.status == wire::RunStatus::Running)
                .map(|run| Run {
                    id: run.id,
                    session_id: run.session_id,
                    request_id: run.request_id,
                    status: RunStatus::Running,
                }),
            child_text: Vec::new(),
            approvals: state
                .approvals
                .into_iter()
                .map(|approval| Approval {
                    id: approval.id,
                    run_id: approval.run_id,
                    session_id: approval.session_id,
                    root_session_id: approval.root_session_id,
                    call: tool_call(approval.call),
                    preview: approval.preview,
                    agent_title: approval.agent_title,
                    depth: depth(approval.depth),
                })
                .collect(),
        })
    }

    async fn child_tool_dispatches(&self) -> Result<Vec<ChildToolDispatch>> {
        // The snapshot does not report live child dispatches. A tool-started
        // event alone is not evidence that an awaiting tool was executed.
        Ok(Vec::new())
    }

    async fn tasks(&self, session: &str) -> Result<Option<Vec<TaskInfo>>> {
        if !self.tasks_available {
            return Ok(None);
        }
        match read(self.client.tasks(session)).await {
            Ok(tasks) => Ok(Some(
                tasks
                    .into_iter()
                    .filter_map(|entry| task(entry, session))
                    .collect(),
            )),
            Err(error) if http_status(&error) == Some(StatusCode::NOT_IMPLEMENTED) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn task_activity_tail(
        &self,
        targets: Vec<TaskIdentity>,
    ) -> impl Future<Output = Result<Vec<Vec<EventRecord>>>> + Send + 'static {
        let client = self.client.clone();
        async move {
            ensure!(targets.len() <= 4, "Too many activity tails");
            try_join_all(targets.into_iter().map(|(session, run)| {
                let client = &client;
                async move {
                    // v1 has no backwards cursor; only use a complete, bounded
                    // page as a best-effort tail, never an old partial page.
                    let page = history(client, &session, 0, 64).await?;
                    if page.has_more {
                        return Ok(Vec::new());
                    }
                    let mut records = page.records;
                    records.retain(|record| record.run_id == run);
                    records.drain(..records.len().saturating_sub(8));
                    Ok(records)
                }
            }))
            .await
        }
    }

    async fn submit(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
        submission: Submission,
    ) -> InputAdmission {
        let body = wire::RunStart {
            interactive: Some(true),
            mode: Some(match submission {
                Submission::Idle => wire::RunMode::Idle,
                Submission::Queue => wire::RunMode::Queue,
            }),
            prompt: prompt.to_owned(),
            request_id: request.to_owned(),
        };
        match self.client.start_run(session, &body).await {
            Ok(receipt) => match input_receipt(receipt, session, request) {
                Ok((receipt, warning)) => InputAdmission::Accepted { receipt, warning },
                Err(error) => InputAdmission::Unconfirmed(error),
            },
            Err(error) => classify_rejection(error, submission),
        }
    }

    async fn input_receipt(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
    ) -> Result<Option<InputReceipt>> {
        let body = wire::InputLookup {
            prompt: prompt.to_owned(),
            request_id: request.to_owned(),
        };
        let found = read(self.client.input_receipt(session, &body)).await?;
        found
            .map(|receipt| input_receipt(receipt, session, request).map(|(receipt, _)| receipt))
            .transpose()
    }

    async fn queue(&self, session: &str) -> Result<QueueState> {
        let queue = read(self.client.queue(session)).await?;
        ensure!(
            queue.session_id == session,
            "Engine returned another session's queue"
        );
        Ok(QueueState {
            paused: queue.paused,
            entries: queue
                .entries
                .into_iter()
                .map(|entry| QueueEntry {
                    prompt: entry.prompt,
                    request_id: entry.request_id,
                    run_id: entry.run_id,
                })
                .collect(),
        })
    }

    async fn control_queue(&self, session: &str, action: QueueAction) -> Result<()> {
        let action = match action {
            QueueAction::Pause => wire::QueueAction::Pause,
            QueueAction::Resume => wire::QueueAction::Resume,
            QueueAction::Clear => wire::QueueAction::Clear,
        };
        self.client
            .control_queue(session, &wire::QueueUpdate { action })
            .await?;
        Ok(())
    }

    async fn steering(&self, run: &str) -> Result<SteeringState> {
        let state = read(self.client.steering(run)).await?;
        ensure!(
            state.run_id == run,
            "Engine returned another run's steering state"
        );
        Ok(SteeringState {
            run_id: state.run_id,
            session_id: state.session_id.clone(),
            supported: state.supported,
            accepting: state.accepting,
            receipts: state
                .receipts
                .into_iter()
                .filter(|receipt| receipt.run_id == run && receipt.session_id == state.session_id)
                .filter_map(|receipt| {
                    Some(SteeringReceipt {
                        input_id: receipt.input_id,
                        run_id: receipt.run_id,
                        prompt: receipt.prompt,
                        position: i64::try_from(receipt.position).ok()?,
                        status: steering_status(receipt.status),
                    })
                })
                .collect(),
        })
    }

    async fn steer(&self, run: &str, prompt: &str, request: &str) -> Result<SteeringStatus> {
        let receipt = self
            .client
            .steer(
                run,
                &wire::SteeringInput {
                    prompt: prompt.to_owned(),
                    request_id: request.to_owned(),
                },
            )
            .await?;
        ensure!(
            receipt.run_id == run && receipt.request_id == request && receipt.prompt == prompt,
            "Engine returned a different steering receipt"
        );
        Ok(steering_status(receipt.status))
    }

    async fn decide(&self, run: &str, approval: &str, call: &str, allow: bool) -> Result<()> {
        ensure!(
            !call.is_empty(),
            "Approval no longer identifies a tool call"
        );
        let receipt = self
            .client
            .decide_approval(
                approval,
                &wire::ApprovalDecision {
                    run_id: run.to_owned(),
                    call_id: call.to_owned(),
                    allow,
                },
            )
            .await?;
        ensure!(
            receipt.approval_id == approval && receipt.allowed == allow,
            "Engine returned a different approval decision"
        );
        Ok(())
    }

    async fn cancel(&self, run: &str) -> Result<()> {
        let stopped = self.client.cancel_run(run).await?;
        ensure!(stopped.id == run, "Engine returned a different run");
        Ok(())
    }

    async fn shutdown(&self) {
        // A client neither owns nor stops the remote server.
    }
}

async fn read<T>(request: impl Future<Output = Result<T, Error>>) -> Result<T> {
    Ok(tokio::time::timeout(READ_TIMEOUT, request)
        .await
        .context("Engine read timed out")??)
}

fn http_status(error: &anyhow::Error) -> Option<StatusCode> {
    match error.downcast_ref::<Error>() {
        Some(Error::Http(status, _)) => Some(*status),
        _ => None,
    }
}

fn classify_rejection(error: Error, submission: Submission) -> InputAdmission {
    match &error {
        Error::Http(
            StatusCode::BAD_REQUEST
            | StatusCode::UNAUTHORIZED
            | StatusCode::FORBIDDEN
            | StatusCode::NOT_FOUND
            | StatusCode::CONFLICT
            | StatusCode::PAYLOAD_TOO_LARGE
            | StatusCode::UNPROCESSABLE_ENTITY
            | StatusCode::TOO_MANY_REQUESTS,
            _,
        )
        | Error::Configuration(_) => {
            let idle_changed = matches!(submission, Submission::Idle)
                && matches!(&error, Error::Http(StatusCode::CONFLICT, detail)
                    if detail.starts_with("This session is busy or paused."));
            InputAdmission::Rejected {
                error: error.into(),
                idle_changed,
            }
        }
        // The request may have reached durable admission. Never auto-submit,
        // retry with a new ID, or tell the UI this binding is safe to release.
        _ => InputAdmission::Unconfirmed(error.into()),
    }
}

fn input_receipt(
    receipt: wire::InputReceipt,
    session: &str,
    request: &str,
) -> Result<(InputReceipt, Option<anyhow::Error>)> {
    ensure!(
        receipt.session_id == session && receipt.request_id == request,
        "Engine returned a different input receipt"
    );
    let status = run_status(receipt.status);
    let warning = receipt
        .warning
        .filter(|warning| !warning.is_empty())
        .map(|warning| anyhow::anyhow!("{warning}"))
        .or_else(|| {
            status
                .is_none()
                .then(|| anyhow::anyhow!("Saved run status is unreadable"))
        });
    Ok((
        InputReceipt {
            run_id: receipt.run_id,
            request_id: receipt.request_id,
            status,
        },
        warning,
    ))
}

fn run_status(status: wire::RunStatus) -> Option<RunStatus> {
    Some(match status {
        wire::RunStatus::Queued => RunStatus::Queued,
        wire::RunStatus::Running => RunStatus::Running,
        wire::RunStatus::Completed => RunStatus::Completed,
        wire::RunStatus::Failed => RunStatus::Failed,
        wire::RunStatus::Cancelled => RunStatus::Cancelled,
        wire::RunStatus::Interrupted => RunStatus::Interrupted,
        wire::RunStatus::Unknown(_) => return None,
    })
}

fn steering_status(status: wire::SteeringStatus) -> SteeringStatus {
    match status {
        wire::SteeringStatus::Pending => SteeringStatus::Pending,
        wire::SteeringStatus::Offered => SteeringStatus::Offered,
        wire::SteeringStatus::Applied => SteeringStatus::Applied,
        wire::SteeringStatus::Uncertain => SteeringStatus::Uncertain,
        wire::SteeringStatus::Undelivered => SteeringStatus::Undelivered,
        wire::SteeringStatus::Unknown(_) => SteeringStatus::Unknown,
    }
}

fn depth(depth: u64) -> u8 {
    u8::try_from(depth).unwrap_or(u8::MAX)
}

fn tool_call(call: wire::ToolCall) -> ToolCall {
    ToolCall {
        call_id: call.call_id,
        name: call.name,
        arguments: serde_json::Value::Object(call.arguments.into_iter().collect()),
    }
}

fn task(task: wire::TaskInfo, session: &str) -> Option<TaskInfo> {
    if task.root_session_id != session && task.parent_session_id != session {
        return None;
    }
    Some(TaskInfo {
        run_id: task.run_id,
        session_id: task.session_id,
        parent_run_id: task.parent_run_id,
        parent_session_id: task.parent_session_id,
        root_run_id: task.root_run_id,
        root_session_id: task.root_session_id,
        call_id: task.call_id,
        title: task.title,
        depth: u8::try_from(task.depth).ok()?,
        read_only: task.read_only,
        status: run_status(task.status)?,
    })
}

fn display_record(record: wire::EventRecord) -> Option<EventRecord> {
    use wire::EngineEvent as E;
    let event = match record.event {
        E::RunQueued(body) => Event::RunQueued {
            prompt: body.prompt,
        },
        E::QueueUpdated(body) => Event::QueueUpdated {
            paused: body.paused,
        },
        E::RunStarted(body) => Event::RunStarted {
            prompt: body.prompt,
        },
        E::SteeringUpdated(body) => Event::SteeringUpdated {
            input_id: body.input_id,
        },
        E::TextDelta(body) => Event::TextDelta { text: body.text },
        E::TextCompleted(body) => Event::TextCompleted { text: body.text },
        E::ToolStarted(body) => Event::ToolStarted {
            call: tool_call(body.call),
        },
        E::ToolCompleted(body) => Event::ToolCompleted {
            result: ToolResult {
                call_id: body.result.call_id,
                name: body.result.name,
                result: body.result.result,
                error: body.result.error,
            },
            duration_ms: body.duration_ms,
        },
        E::ToolApprovalTiming(body) => Event::ToolApprovalTiming {
            call_id: body.call_id,
            elapsed_ms: body.elapsed_ms,
            waiting: body.waiting,
        },
        E::ApprovalRequested(body) => Event::ApprovalRequested {
            approval_id: body.approval_id,
            call: tool_call(body.call),
            preview: body.preview,
        },
        E::ApprovalResolved(body) => Event::ApprovalResolved {
            approval_id: body.approval_id,
            allowed: body.allowed,
        },
        E::Notice(body) => Event::Notice { text: body.text },
        E::TaskStarted(body) => Event::TaskStarted {
            task: task(body.task, &record.session_id)?,
        },
        E::TaskUpdated(body) => Event::TaskUpdated {
            task: task(body.task, &record.session_id)?,
        },
        E::TaskFinished(body) => Event::TaskFinished {
            task: task(body.task, &record.session_id)?,
            error: body.error,
        },
        E::RunFinished(body) => Event::RunFinished {
            status: run_status(body.status)?,
            error: body.error,
        },
        E::EngineConnected(_) | E::SessionUpdated(_) | E::Unknown(..) => return None,
    };
    Some(EventRecord {
        schema_version: VERSION,
        sequence: i64::try_from(record.sequence).ok()?,
        session_id: record.session_id,
        run_id: record.run_id,
        timestamp_ms: record.timestamp_ms,
        event,
    })
}

async fn history(client: &Client, session: &str, after: i64, limit: u64) -> Result<HistoryPage> {
    let after = u64::try_from(after)?;
    let page = read(client.history(session, after, limit)).await?;
    ensure!(
        page.cursor >= after && (!page.has_more || page.cursor > after),
        "Engine history cursor did not advance"
    );
    let mut previous = after;
    for event in &page.events {
        ensure!(
            event.session_id == session
                && event.sequence > previous
                && event.sequence <= page.cursor,
            "Engine returned out-of-order or foreign history"
        );
        previous = event.sequence;
    }
    Ok(HistoryPage {
        cursor: i64::try_from(page.cursor)?,
        has_more: page.has_more,
        records: page
            .events
            .into_iter()
            .filter(is_durable)
            .filter_map(display_record)
            .collect(),
    })
}

struct Feed {
    client: Client,
    events: Option<Events>,
    after: u64,
    retries: u32,
    connected_at: Instant,
    ended: bool,
}

impl Feed {
    async fn next(&mut self) -> Option<Update> {
        if self.ended {
            return None;
        }
        if self.events.is_none() {
            return Some(Err(self.reconnect().await));
        }
        loop {
            let events = self.events.as_mut()?;
            let event = match events.next().await {
                Ok(Some(event)) => event,
                Err(error @ Error::Incompatible { .. }) => {
                    self.ended = true;
                    self.events = None;
                    return Some(Err(FeedNotice::Disconnected(error.to_string())));
                }
                Ok(None) | Err(_) => {
                    if self.connected_at.elapsed() >= Duration::from_secs(30) {
                        self.retries = 0;
                    }
                    self.events = None;
                    return Some(Err(FeedNotice::Reconnecting));
                }
            };
            if event.sequence <= self.after || event.sequence > i64::MAX as u64 {
                tokio::task::consume_budget().await;
                continue;
            }
            // The engine reserves each sequence in SQLite before publishing,
            // including live-only deltas. They advance the socket cursor but
            // never enter the HTTP history or become durable transcript rows.
            self.after = event.sequence;
            self.retries = 0;
            let delivery = if is_durable(&event) {
                Delivery::Durable
            } else {
                Delivery::Transient
            };
            // Unknown durable events still contribute to the resume cursor.
            if let Some(record) = display_record(event) {
                return Some(Ok(LiveUpdate { record, delivery }));
            }
            // A long replay containing only newer, unknown event variants
            // must not monopolize the terminal loop.
            tokio::task::consume_budget().await;
        }
    }

    async fn reconnect(&mut self) -> FeedNotice {
        while self.retries < RECONNECT_ATTEMPTS {
            tokio::time::sleep(Duration::from_millis(125 * 2_u64.pow(self.retries))).await;
            self.retries += 1;
            let connection =
                tokio::time::timeout(RECONNECT_TIMEOUT, self.client.subscribe(self.after, None))
                    .await;
            match connection {
                Ok(Ok(events)) => {
                    self.events = Some(events);
                    self.connected_at = Instant::now();
                    return FeedNotice::ResyncRequired;
                }
                Ok(Err(error @ Error::Incompatible { .. })) => {
                    self.ended = true;
                    return FeedNotice::Disconnected(error.to_string());
                }
                _ => (),
            }
        }
        self.ended = true;
        FeedNotice::Disconnected("Engine event connection lost; reopen to reconnect".into())
    }
}

fn is_durable(event: &wire::EventRecord) -> bool {
    // v1 journals completed text and lifecycle events, not partial text.
    !matches!(
        event.event,
        wire::EngineEvent::TextDelta(_) | wire::EngineEvent::EngineConnected(_)
    )
}
