use std::{
    collections::{HashMap, VecDeque},
    time::Instant,
};

use crate::model::{Event, EventRecord, RunStatus, TaskInfo};
use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};
use tokio::sync::oneshot;

use super::super::footer::{Field, Phase, WIDTH};
use crate::engine::{Approval, ChildTextPhase, ChildToolDispatch, Engine, TaskIdentity};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum Progress {
    #[default]
    Running,
    Tool,
    Responding,
}

#[derive(Clone)]
enum Signal {
    Start,
    Text(bool),
    ToolStarted(String),
    Request { call: String, approval: String },
    Decision { approval: String, allowed: bool },
    ToolEnded(String),
    Finished(RunStatus),
}

impl Signal {
    fn from_event(event: &Event) -> Option<Self> {
        Some(match event {
            Event::RunStarted { .. } => Self::Start,
            Event::TextDelta { text } if !text.is_empty() => Self::Text(true),
            Event::TextCompleted { .. } => Self::Text(false),
            Event::ToolStarted { call } => Self::ToolStarted(call.call_id.clone()),
            Event::ApprovalRequested {
                approval_id, call, ..
            } => Self::Request {
                call: call.call_id.clone(),
                approval: approval_id.clone(),
            },
            Event::ApprovalResolved {
                approval_id,
                allowed,
            } => Self::Decision {
                approval: approval_id.clone(),
                allowed: *allowed,
            },
            Event::ToolCompleted { result, .. } => Self::ToolEnded(result.call_id.clone()),
            Event::RunFinished { status, .. } => Self::Finished(*status),
            _ => return None,
        })
    }

    fn text_edge(&self) -> Option<bool> {
        // Keep the projection aligned with the host's ChildTextPhase::edge.
        match self {
            Self::Text(responding) => Some(*responding),
            Self::Start | Self::ToolStarted(_) | Self::Request { .. } | Self::ToolEnded(_) => {
                Some(false)
            }
            _ => None,
        }
    }
}

#[derive(PartialEq, Eq)]
enum Decision {
    Pending,
    Allowed,
    Denied,
}

#[derive(Default)]
struct Evidence {
    responding: bool,
    text_after: i64,
    tools: HashMap<String, (String, Decision)>,
    finished: Option<RunStatus>,
}

impl Evidence {
    fn observe_text(&mut self, sequence: i64, responding: bool) -> bool {
        if sequence <= self.text_after {
            return false;
        }
        self.text_after = sequence;
        self.responding = responding;
        true
    }

    fn record(&mut self, signal: Signal) {
        match signal {
            // Registration already isolated this exact run. Its durable
            // start must not discard an approval seen through the live snapshot.
            Signal::Start | Signal::Text(_) => (),
            Signal::ToolStarted(call) => {
                // A provisional start may be rejected or wait for approval.
                if self
                    .tools
                    .get(&call)
                    .is_some_and(|(_, decision)| *decision != Decision::Pending)
                {
                    self.tools.remove(&call);
                }
            }
            Signal::Request { call, approval } => {
                self.tools.insert(call, (approval, Decision::Pending));
            }
            Signal::Decision { approval, allowed } => {
                for (id, decision) in self.tools.values_mut() {
                    if *id == approval && *decision == Decision::Pending {
                        *decision = if allowed {
                            Decision::Allowed
                        } else {
                            Decision::Denied
                        };
                    }
                }
            }
            Signal::ToolEnded(call) => {
                self.tools.remove(&call);
            }
            Signal::Finished(status) => self.finished = Some(status),
        }
    }

    fn progress(&self, dispatch: Option<&str>) -> Progress {
        if dispatch.is_some_and(|call| {
            self.tools
                .get(call)
                .is_none_or(|(_, decision)| *decision == Decision::Allowed)
        }) {
            Progress::Tool
        } else if self.responding && dispatch.is_none() {
            Progress::Responding
        } else {
            Progress::Running
        }
    }

    fn pending(&self) -> bool {
        self.tools
            .values()
            .any(|(_, decision)| *decision == Decision::Pending)
    }
}

struct Child {
    run: String,
    registration: u64,
    revision: u64,
    after: i64,
    needs_recovery: bool,
    evidence: Evidence,
    dispatch: Option<String>,
    progress: Progress,
    waiting: bool,
    field: Field,
    frame_at: Instant,
}

impl Child {
    fn new(run: String, registration: u64, now: Instant) -> Self {
        Self {
            run,
            registration,
            revision: 0,
            after: 0,
            needs_recovery: true,
            evidence: Evidence::default(),
            dispatch: None,
            progress: Progress::Running,
            waiting: false,
            field: Field::new(),
            frame_at: now,
        }
    }

    fn observe(&mut self, sequence: i64, signal: Signal) {
        if sequence > self.after && self.evidence.finished.is_none() {
            self.revision += 1;
            self.after = sequence;
            // A decision from a partially observed run may need its request.
            // An unresolved, snapshot-bound decision can likewise need the tail
            // if the host cleared its modal before live delivery caught up.
            let missing_request = match &signal {
                Signal::Decision { approval, .. } => {
                    !self.evidence.tools.values().any(|(id, _)| id == approval)
                }
                _ => false,
            };
            if let Signal::ToolEnded(call) = &signal
                && self.dispatch.as_deref() == Some(call)
            {
                self.dispatch = None;
            }
            if let Some(responding) = signal.text_edge() {
                self.evidence.observe_text(sequence, responding);
            }
            self.evidence.record(signal);
            if self.evidence.finished.is_some() || (!self.evidence.pending() && !missing_request) {
                self.needs_recovery = false;
            }
        }
    }

    fn moving(&self) -> bool {
        !self.waiting && self.evidence.finished.is_none()
    }

    fn can_recover(&self) -> bool {
        self.needs_recovery && self.moving()
    }

    fn advance(&mut self, now: Instant) {
        if self.moving() {
            self.field.advance(
                now.saturating_duration_since(self.frame_at)
                    .min(super::super::footer::FRAME * 3)
                    .as_secs_f64(),
            );
        }
        self.frame_at = now;
    }

    fn select(&mut self, waiting: bool, now: Instant, animated: bool) {
        if waiting != self.waiting {
            self.revision += 1;
            self.frame_at = now; // Never advance a field through an approval wait.
            self.waiting = waiting;
            if !waiting && self.evidence.pending() {
                self.needs_recovery = true;
            }
        }
        self.progress = self.evidence.progress(self.dispatch.as_deref());
        if animated {
            self.field.select(
                match self.progress {
                    Progress::Tool => Phase::Tool,
                    Progress::Responding => Phase::Responding,
                    Progress::Running => Phase::Working,
                },
                false,
            );
        }
    }
}

const RECOVERY_BATCH: usize = 4;
const PENDING_LIMIT: usize = 256;

struct Pending {
    identity: TaskIdentity,
    sequence: i64,
    signal: Signal,
}

struct Recovery {
    targets: Vec<RecoveryTarget>,
    result: oneshot::Receiver<Option<Vec<Vec<EventRecord>>>>,
    stale: bool,
}

#[derive(Clone)]
struct RecoveryTarget {
    identity: TaskIdentity,
    registration: u64,
    revision: u64,
}

#[derive(Default)]
pub(in crate::tui) struct Activity {
    // Every child has a host-owned private session. Still check its exact
    // run ID both on ingest and lookup; neither title nor row order is identity.
    children: HashMap<String, Child>,
    pending: VecDeque<Pending>,
    recovery: Option<Recovery>,
    registrations: u64,
    animated: bool,
}

impl Activity {
    pub(in crate::tui) fn set_animated(&mut self, animated: bool) {
        self.animated = animated;
    }

    pub(in crate::tui) fn ingest(&mut self, record: &EventRecord) {
        let Some(signal) = Signal::from_event(&record.event) else {
            return;
        };
        if let Some(child) = self.children.get_mut(&record.session_id) {
            if child.run == record.run_id {
                child.observe(record.sequence, signal);
                child.select(child.waiting, Instant::now(), self.animated);
            }
        } else {
            if self.pending.len() == PENDING_LIMIT {
                self.pending.pop_front();
            }
            self.pending.push_back(Pending {
                identity: (record.session_id.clone(), record.run_id.clone()),
                sequence: record.sequence,
                signal,
            });
        }
    }

    pub(in crate::tui) fn invalidate(&mut self) {
        if let Some(recovery) = &mut self.recovery {
            recovery.stale = true;
        }
        self.pending.clear();
        for child in self.children.values_mut() {
            // Lag is global, not evidence that this child changed phase. Keep
            // its confirmed state until an exact newer event or live closure.
            // Still invalidate in-flight jobs and request a bounded fresh tail.
            child.revision += 1;
            child.needs_recovery = child.evidence.finished.is_none();
        }
    }

    fn reconcile(&mut self, tasks: &[TaskInfo], now: Instant) {
        self.children.retain(|session, child| {
            tasks.iter().any(|task| {
                task.status == RunStatus::Running
                    && task.session_id == *session
                    && task.run_id == child.run
            })
        });
        for task in tasks
            .iter()
            .filter(|task| task.status == RunStatus::Running)
        {
            self.children
                .entry(task.session_id.clone())
                .or_insert_with(|| {
                    self.registrations += 1;
                    Child::new(task.run_id.clone(), self.registrations, now)
                });
        }
        self.pending.retain(|pending| {
            let (session, run) = &pending.identity;
            if let Some(child) = self.children.get_mut(session)
                && child.run == *run
            {
                child.observe(pending.sequence, pending.signal.clone());
                false
            } else {
                true
            }
        });
    }

    pub(super) fn catalog(&mut self, tasks: &[TaskInfo]) {
        self.reconcile(tasks, Instant::now());
    }

    pub(in crate::tui) fn sync_approvals(&mut self, approvals: &[Approval]) {
        let now = Instant::now();
        for (session, child) in &mut self.children {
            let approval = approvals
                .iter()
                .find(|approval| approval.session_id == *session && approval.run_id == child.run);
            if let Some(approval) = approval {
                // A live pending snapshot can precede the durable request.
                // Block the exact call even during the later gap between the
                // modal clearing and the durable approval decision arriving.
                let current = child.evidence.tools.get(&approval.call.call_id);
                if current.is_none_or(|(id, _)| *id != approval.id) {
                    child.evidence.record(Signal::Request {
                        call: approval.call.call_id.clone(),
                        approval: approval.id.clone(),
                    });
                    child.revision += 1;
                }
            }
            child.select(approval.is_some(), now, self.animated);
        }
    }

    pub(in crate::tui) fn sync_dispatches(&mut self, dispatches: &[ChildToolDispatch]) {
        let now = Instant::now();
        for (session, child) in &mut self.children {
            let call = dispatches
                .iter()
                .find(|dispatch| dispatch.session_id == *session && dispatch.run_id == child.run)
                .map(|dispatch| dispatch.call_id.as_str());
            if child.dispatch.as_deref() != call {
                child.revision += 1;
                child.dispatch = call.map(str::to_owned);
                child.select(child.waiting, now, self.animated);
            }
        }
    }

    pub(in crate::tui) fn sync_text_phases(&mut self, phases: &[ChildTextPhase]) {
        let now = Instant::now();
        for phase in phases {
            if let Some(child) = self.children.get_mut(&phase.session_id)
                && child.run == phase.run_id
                && child.evidence.finished.is_none()
                && child
                    .evidence
                    .observe_text(phase.sequence, phase.responding)
            {
                // Do not move the journal cursor: older durable approvals and
                // terminal evidence still need ordinary ordered ingestion.
                child.select(child.waiting, now, self.animated);
            }
        }
    }

    pub(super) fn recover<'a>(
        &mut self,
        engine: &impl Engine,
        visible: impl Iterator<Item = &'a TaskInfo>,
    ) {
        // Called only on the existing 100ms state tick; a busy stream never
        // spawns recovery. Reduced-motion rows still get live phase evidence.
        if !self.animated {
            return;
        }
        if let Some(recovery) = &mut self.recovery {
            match recovery.result.try_recv() {
                Err(oneshot::error::TryRecvError::Empty) => return,
                result => {
                    let targets = std::mem::take(&mut recovery.targets);
                    let stale = recovery.stale;
                    self.recovery = None;
                    if !stale {
                        if let Ok(Some(batches)) = result {
                            for (target, records) in targets.into_iter().zip(batches) {
                                self.apply_tail(target, &records);
                            }
                        } else {
                            for target in targets {
                                self.apply_tail(target, &[]);
                            }
                        }
                    }
                    // At most one result or request per tick; on contention
                    // or error the optional phase stays generic.
                    return;
                }
            }
        }
        let targets: Vec<_> = visible
            .filter_map(|task| {
                let child = self.child(task)?;
                child
                    .can_recover()
                    .then(|| Self::recovery_target(task, child))
            })
            .take(RECOVERY_BATCH)
            .collect();
        if targets.is_empty() {
            return;
        }
        let (send, result) = oneshot::channel();
        let requested = targets
            .iter()
            .map(|target| target.identity.clone())
            .collect();
        let load = engine.task_activity_tail(requested);
        tokio::spawn(async move {
            let _ = send.send(load.await.ok());
        });
        self.recovery = Some(Recovery {
            targets,
            result,
            stale: false,
        });
    }

    fn recovery_target(task: &TaskInfo, child: &Child) -> RecoveryTarget {
        RecoveryTarget {
            identity: (task.session_id.clone(), task.run_id.clone()),
            registration: child.registration,
            revision: child.revision,
        }
    }

    fn apply_tail(&mut self, target: RecoveryTarget, records: &[EventRecord]) {
        let (session, run) = target.identity;
        if let Some(child) = self.children.get_mut(&session)
            && child.run == run
            && child.registration == target.registration
            && child.revision == target.revision
            && child.can_recover()
        {
            child.needs_recovery = false;
            // A live event received after this tail was sampled wins. A
            // bounded suffix is evidence only for the events it contains.
            let exact =
                |record: &&EventRecord| record.run_id == run && record.session_id == session;
            let records = records.iter().filter(exact);
            let Some(last) = records.clone().next_back() else {
                return;
            };
            let watermark = child.after;
            if last.sequence < watermark {
                return;
            }
            for record in records {
                if let Some(signal) = Signal::from_event(&record.event) {
                    if record.sequence > watermark {
                        child.observe(record.sequence, signal);
                    } else if matches!(signal, Signal::Decision { .. }) {
                        // An exact decision may precede a live execution edge
                        // already observed without its approval event. It can
                        // settle only a still-pending binding, never replay a
                        // phase or change an existing decision.
                        child.evidence.record(signal);
                    }
                }
            }
            child.after = child.after.max(last.sequence);
            child.select(child.waiting, Instant::now(), self.animated);
        }
    }

    pub(super) fn advance(&mut self, task: &TaskInfo, now: Instant) {
        if self.animated
            && let Some(child) = self.children.get_mut(&task.session_id)
            && child.run == task.run_id
        {
            child.advance(now);
        }
    }

    fn child(&self, task: &TaskInfo) -> Option<&Child> {
        self.children
            .get(&task.session_id)
            .filter(|child| child.run == task.run_id && task.status == RunStatus::Running)
    }

    pub(super) fn moving(&self, task: &TaskInfo) -> bool {
        self.animated && self.child(task).is_some_and(Child::moving)
    }

    pub(super) fn indicator(&self, task: &TaskInfo, muted: Color) -> (Line<'static>, &'static str) {
        let child = self.child(task);
        let status = child
            .and_then(|child| child.evidence.finished)
            .unwrap_or(task.status);
        let (label, color) = match status {
            RunStatus::Queued => ("Queued", muted),
            RunStatus::Running if child.is_some_and(|child| child.waiting) => {
                ("Appr.", Color::Yellow)
            }
            RunStatus::Running => {
                let fallback;
                let (field, progress) = if let Some(child) = child {
                    (&child.field, child.progress)
                } else {
                    fallback = Field::new();
                    (&fallback, Progress::Running)
                };
                let suffix = match progress {
                    Progress::Running => "",
                    Progress::Tool => " · Tool",
                    Progress::Responding => " · Responding",
                };
                return (Line::from_iter(field.spans()), suffix);
            }
            RunStatus::Completed => ("Done", Color::Green),
            RunStatus::Failed => ("Failed", Color::Red),
            RunStatus::Cancelled => ("Canc'd", muted),
            RunStatus::Interrupted => ("Intrpt.", Color::Yellow),
        };
        (
            Line::from(Span::styled(
                format!("{label:<WIDTH$}"),
                Style::new().fg(color),
            )),
            "",
        )
    }

    #[cfg(test)]
    pub(super) fn observe(
        &mut self,
        tasks: &[TaskInfo],
        records: &[EventRecord],
        waiting: &[&str],
        now: Instant,
    ) {
        self.reconcile(tasks, now);
        for record in records {
            self.ingest(record);
        }
        for child in self.children.values_mut() {
            child.select(waiting.contains(&child.run.as_str()), now, self.animated);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ToolCall, VERSION};

    fn task(index: usize) -> TaskInfo {
        TaskInfo {
            run_id: format!("run-{index}"),
            session_id: format!("session-{index}"),
            parent_run_id: "root".into(),
            parent_session_id: "root-session".into(),
            root_run_id: "root".into(),
            root_session_id: "root-session".into(),
            call_id: "delegate".into(),
            title: "same title".into(),
            depth: 1,
            read_only: true,
            status: RunStatus::Running,
        }
    }

    fn record(task: &TaskInfo, sequence: i64, event: Event) -> EventRecord {
        EventRecord {
            schema_version: VERSION,
            sequence,
            run_id: task.run_id.clone(),
            session_id: task.session_id.clone(),
            timestamp_ms: 0,
            event,
        }
    }

    fn tool() -> Event {
        Event::ToolStarted {
            call: ToolCall {
                call_id: "call".into(),
                name: "read_file".into(),
                arguments: serde_json::json!({}),
            },
        }
    }

    fn dispatch(task: &TaskInfo) -> ChildToolDispatch {
        ChildToolDispatch {
            call_id: "call".into(),
            run_id: task.run_id.clone(),
            session_id: task.session_id.clone(),
        }
    }

    fn target(activity: &Activity, task: &TaskInfo) -> RecoveryTarget {
        Activity::recovery_target(task, activity.child(task).unwrap())
    }

    fn approval(task: &TaskInfo) -> Approval {
        let Event::ToolStarted { call } = tool() else {
            unreachable!()
        };
        Approval {
            id: "approval".into(),
            run_id: task.run_id.clone(),
            session_id: task.session_id.clone(),
            root_session_id: task.root_session_id.clone(),
            agent_title: task.title.clone(),
            depth: task.depth,
            call,
            preview: "private".into(),
        }
    }

    #[test]
    fn direct_events_before_registration_stay_exact_without_recovery_or_guessed_phase() {
        let mut activity = Activity::default();
        let [a, b, c] = [task(1), task(2), task(3)];
        let start = record(
            &a,
            1,
            Event::RunStarted {
                prompt: "go".into(),
            },
        );
        activity.ingest(&start);
        activity.ingest(&record(&b, 2, tool()));
        let mut cross_run = record(&b, 3, tool());
        cross_run.session_id = a.session_id.clone();
        activity.ingest(&cross_run);
        activity.catalog(&[a.clone(), b.clone(), c.clone()]);
        activity.sync_approvals(&[]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, ""); // Admission is not a phase.
        assert_eq!(activity.indicator(&b, Color::Reset).1, ""); // Preparing is not executing.
        assert_eq!(activity.indicator(&c, Color::Reset).1, ""); // Missing history.
        assert!(!activity.child(&a).unwrap().needs_recovery);
        assert!(!activity.child(&b).unwrap().needs_recovery);
        assert!(activity.child(&c).unwrap().needs_recovery);

        activity.invalidate();
        let old_b = target(&activity, &b);
        assert_eq!(activity.indicator(&b, Color::Reset).1, "");
        // A stale/duplicate broadcast cannot restore a cleared phase.
        activity.ingest(&record(&b, 2, tool()));
        assert_eq!(activity.indicator(&b, Color::Reset).1, "");
        activity.ingest(&record(
            &b,
            4,
            Event::TextDelta {
                text: "seen".into(),
            },
        ));
        assert_eq!(activity.indicator(&b, Color::Reset).1, " · Responding");
        activity.apply_tail(old_b, &[record(&b, 3, tool())]);
        assert_eq!(activity.indicator(&b, Color::Reset).1, " · Responding");
        activity.ingest(&record(
            &b,
            5,
            Event::TextCompleted {
                text: "seen".into(),
            },
        ));
        assert_eq!(activity.indicator(&b, Color::Reset).1, "");

        activity.apply_tail(target(&activity, &c), &[record(&a, 6, tool())]);
        assert_eq!(activity.indicator(&c, Color::Reset).1, "");
    }

    #[test]
    fn live_approval_before_its_first_durable_record_never_guesses_tool_execution() {
        let [a, b] = [task(1), task(2)];
        let mut activity = Activity::default();
        activity.catalog(&[a.clone(), b.clone()]);
        activity.ingest(&record(&a, 1, tool()));
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        activity.sync_approvals(&[approval(&a)]);
        assert_eq!(
            activity.indicator(&a, Color::Reset).0.to_string().trim(),
            "Appr."
        );
        assert_ne!(
            activity.indicator(&b, Color::Reset).0.to_string().trim(),
            "Appr."
        );
        activity.sync_approvals(&[]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        activity.ingest(&record(
            &a,
            2,
            Event::ApprovalResolved {
                approval_id: "approval".into(),
                allowed: true,
            },
        ));
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        activity.sync_dispatches(&[dispatch(&a)]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Tool");
    }

    #[test]
    fn stale_recovery_cannot_cross_approval_decisions_terminal_or_registration() {
        for allowed in [false, true] {
            let [a, b] = [task(1), task(2)];
            let mut activity = Activity::default();
            activity.catalog(&[a.clone(), b.clone()]);
            let old = target(&activity, &a);
            let tail = [record(&a, 41, tool())];

            activity.sync_approvals(&[approval(&a)]);
            activity.apply_tail(old.clone(), &tail);
            assert_eq!(
                activity.indicator(&a, Color::Reset).0.to_string().trim(),
                "Appr."
            );
            assert_ne!(
                activity.indicator(&b, Color::Reset).0.to_string().trim(),
                "Appr."
            );
            // The modal can clear before any durable approval record arrives.
            activity.sync_approvals(&[]);
            activity.apply_tail(old.clone(), &tail);
            assert_eq!(activity.indicator(&a, Color::Reset).1, "");
            assert!(activity.child(&a).unwrap().can_recover()); // A fresh tail may settle it.
            assert_eq!(activity.child(&a).unwrap().after, 0);
            activity.ingest(&record(&a, 41, tool()));
            assert_eq!(activity.indicator(&a, Color::Reset).1, "");
            activity.ingest(&record(
                &b,
                42,
                Event::ApprovalResolved {
                    approval_id: "approval".into(),
                    allowed: true,
                },
            ));
            assert_eq!(activity.indicator(&a, Color::Reset).1, "");
            assert_eq!(activity.indicator(&b, Color::Reset).1, "");
            activity.ingest(&record(
                &a,
                43,
                Event::ApprovalResolved {
                    approval_id: "approval".into(),
                    allowed,
                },
            ));
            assert_eq!(activity.indicator(&a, Color::Reset).1, "");
            if allowed {
                activity.sync_dispatches(&[dispatch(&a)]);
            }
            activity.apply_tail(old.clone(), &tail);
            assert_eq!(
                activity.indicator(&a, Color::Reset).1,
                if allowed { " · Tool" } else { "" }
            );

            activity.ingest(&record(
                &a,
                45,
                Event::RunFinished {
                    status: RunStatus::Cancelled,
                    error: String::new(),
                },
            ));
            activity.apply_tail(old.clone(), &tail);
            assert_eq!(
                activity.indicator(&a, Color::Reset).0.to_string().trim(),
                "Canc'd"
            );
            assert!(!activity.moving(&a));

            let mut replacement = a.clone();
            replacement.run_id = "replacement".into();
            activity.catalog(std::slice::from_ref(&replacement));
            activity.apply_tail(old.clone(), &tail);
            assert_eq!(activity.child(&replacement).unwrap().after, 0);
            let mut other_session = a.clone();
            other_session.session_id = "replacement-session".into();
            activity.catalog(std::slice::from_ref(&other_session));
            activity.apply_tail(old.clone(), &tail);
            assert_eq!(activity.child(&other_session).unwrap().after, 0);
            // Even removal and re-registration of the same exact identity
            // cannot reattach a job issued for the previous registration.
            activity.catalog(std::slice::from_ref(&a));
            activity.apply_tail(old, &tail);
            assert_eq!(activity.child(&a).unwrap().after, 0);
            assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        }
    }

    #[test]
    fn fresh_tail_merges_exact_allow_without_replaying_older_phases() {
        let [a, b] = [task(1), task(2)];
        let mut activity = Activity::default();
        activity.catalog(&[a.clone(), b.clone()]);
        activity.sync_approvals(&[approval(&a)]);
        activity.sync_approvals(&[]);
        let decision = |id: &str| Event::ApprovalResolved {
            approval_id: id.into(),
            allowed: true,
        };
        let text = Event::TextDelta { text: "old".into() };
        // The live host dispatch arrived before its durable approval record was
        // observed by this UI. Pending stays authoritative until an exact match.
        activity.ingest(&record(&a, 50, tool()));
        activity.sync_dispatches(&[dispatch(&a)]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        activity.apply_tail(
            target(&activity, &a),
            &[
                record(&a, 40, text.clone()),
                record(&a, 41, decision("unrelated")),
                record(&b, 42, decision("approval")),
                record(&a, 50, tool()),
                record(
                    &a,
                    51,
                    Event::Notice {
                        text: "not a phase".into(),
                    },
                ),
            ],
        );
        assert_eq!(activity.child(&a).unwrap().after, 51);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");

        // A later recovery attempt may prove the pending approval even though
        // its sequence is behind the already-observed execution watermark.
        activity.invalidate();
        activity.ingest(&record(&a, 54, Event::TextCompleted { text: "old".into() }));
        activity.apply_tail(
            target(&activity, &a),
            &[
                record(&a, 40, text.clone()),
                record(&a, 49, decision("approval")),
                record(&a, 54, Event::TextCompleted { text: "old".into() }),
                record(
                    &a,
                    55,
                    Event::Notice {
                        text: "latest".into(),
                    },
                ),
            ],
        );
        assert_eq!(activity.child(&a).unwrap().after, 55);
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Tool");
        activity.ingest(&record(&a, 54, text.clone()));
        assert!(!activity.child(&a).unwrap().evidence.responding);

        activity.invalidate();
        activity.apply_tail(
            target(&activity, &a),
            &[
                record(&a, 40, text.clone()),
                record(&a, 54, Event::TextCompleted { text: "old".into() }),
                record(
                    &a,
                    55,
                    Event::Notice {
                        text: "same watermark".into(),
                    },
                ),
            ],
        );
        assert_eq!(activity.child(&a).unwrap().after, 55);
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Tool");
        activity.sync_dispatches(&[]);
        activity.ingest(&record(&a, 53, text));
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
    }

    #[test]
    fn live_dispatch_survives_lag_at_the_watermark_but_history_cannot_invent_it() {
        let a = task(1);
        let mut activity = Activity::default();
        activity.catalog(std::slice::from_ref(&a));
        activity.apply_tail(target(&activity, &a), &[record(&a, 1, tool())]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        let mut wrong = dispatch(&a);
        wrong.session_id = "different".into();
        activity.sync_dispatches(&[wrong]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        activity.sync_dispatches(&[dispatch(&a)]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Tool");
        activity.invalidate();
        activity.apply_tail(
            target(&activity, &a),
            &[
                record(&a, 1, tool()),
                record(
                    &a,
                    2,
                    Event::Notice {
                        text: "still open".into(),
                    },
                ),
            ],
        );
        assert_eq!(activity.child(&a).unwrap().after, 2);
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Tool");
        activity.sync_dispatches(&[]);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
    }

    #[tokio::test]
    async fn unrelated_broadcast_lag_keeps_responding_until_exact_new_completion() {
        let [a, b] = [task(1), task(2)];
        let (send, mut recv) = tokio::sync::broadcast::channel(1);
        let delta = record(
            &a,
            1,
            Event::TextDelta {
                text: "stream".into(),
            },
        );
        let notice = record(
            &a,
            4,
            Event::Notice {
                text: "still open".into(),
            },
        );
        let mut activity = Activity::default();
        activity.catalog(&[a.clone(), b.clone()]);
        send.send(delta.clone()).unwrap();
        activity.ingest(&recv.recv().await.unwrap());
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Responding");
        for sequence in [2, 3] {
            send.send(record(
                &b,
                sequence,
                Event::TextDelta {
                    text: "sibling".into(),
                },
            ))
            .unwrap();
        }
        assert!(matches!(
            recv.recv().await,
            Err(tokio::sync::broadcast::error::RecvError::Lagged(1))
        ));
        activity.invalidate();
        activity.apply_tail(target(&activity, &a), &[delta.clone(), notice.clone()]);
        assert_eq!(activity.child(&a).unwrap().after, 4);
        assert_eq!(activity.indicator(&a, Color::Reset).1, " · Responding");

        activity.invalidate();
        let closed = record(
            &a,
            5,
            Event::TextCompleted {
                text: "stream".into(),
            },
        );
        activity.apply_tail(
            target(&activity, &a),
            &[delta.clone(), notice.clone(), closed],
        );
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
        activity.invalidate();
        activity.apply_tail(target(&activity, &a), &[delta, notice]);
        assert_eq!(activity.child(&a).unwrap().after, 5);
        assert_eq!(activity.indicator(&a, Color::Reset).1, "");
    }

    #[tokio::test]
    async fn live_text_snapshot_closes_completion_dropped_behind_an_exact_noisy_tail() {
        use crate::engine::fake::FakeEngine;

        let engine = FakeEngine::default();
        let session = engine.create_session();
        let root = engine.start(&session, "hold");
        let tasks: Vec<_> = ["closed", "held"]
            .into_iter()
            .map(|title| TaskInfo {
                run_id: format!("run-{title}"),
                session_id: format!("session-{title}"),
                parent_run_id: root.id.clone(),
                parent_session_id: session.clone(),
                root_run_id: root.id.clone(),
                root_session_id: session.clone(),
                call_id: title.into(),
                title: title.into(),
                depth: 1,
                read_only: true,
                status: RunStatus::Running,
            })
            .collect();
        for task in &tasks {
            engine.add_task(task.clone());
        }
        let tasks = engine.tasks(&session).await.unwrap().unwrap();
        let closed = tasks.iter().find(|task| task.title == "closed").unwrap();
        let held = tasks.iter().find(|task| task.title == "held").unwrap();
        let emit = |task: &TaskInfo, event| engine.emit(&task.session_id, &task.run_id, event);
        let delta = || Event::TextDelta {
            text: "stream".into(),
        };
        let notice = || Event::Notice {
            text: "not a phase".into(),
        };

        let mut activity = Activity::default();
        activity.catalog(&tasks);
        let mut old = Vec::new();
        for task in [closed, held] {
            let record = emit(task, delta());
            activity.ingest(&record);
            old.push(record);
            assert_eq!(activity.indicator(task, Color::Reset).1, " · Responding");
        }
        let old_phases = engine
            .conversation_activity(&session)
            .await
            .unwrap()
            .child_text;
        // The live feed lags and drops the exact child's committed close; only
        // the noise behind it is delivered later.
        emit(
            closed,
            Event::TextCompleted {
                text: "stream".into(),
            },
        );
        let mut noise = Vec::new();
        for task in [closed, held] {
            for _ in 0..8 {
                noise.push(emit(task, notice()));
            }
        }
        let tails = engine
            .task_activity_tail(
                [closed, held]
                    .into_iter()
                    .map(|task| (task.session_id.clone(), task.run_id.clone()))
                    .collect(),
            )
            .await
            .unwrap();
        assert!(tails.iter().all(|tail| {
            tail.len() == 8
                && tail
                    .iter()
                    .all(|row| matches!(row.event, Event::Notice { .. }))
        }));

        activity.invalidate();
        let phases = engine
            .conversation_activity(&session)
            .await
            .unwrap()
            .child_text;
        assert_eq!(phases.len(), 2);
        for (task, tail) in [closed, held].into_iter().zip(tails) {
            activity.apply_tail(target(&activity, task), &tail);
            assert_eq!(activity.child(task).unwrap().after, tail[7].sequence);
        }
        let watermark = activity.child(closed).unwrap().after;
        let close = phases
            .iter()
            .find(|phase| phase.run_id == closed.run_id)
            .unwrap();
        assert!(old[0].sequence < close.sequence && close.sequence < watermark);
        activity.sync_text_phases(&phases);
        assert_eq!(activity.child(closed).unwrap().after, watermark);
        assert_eq!(activity.indicator(closed, Color::Reset).1, "");
        assert_eq!(activity.indicator(held, Color::Reset).1, " · Responding");
        for record in &noise {
            activity.ingest(record);
        }
        // Old evidence and the right run paired with the wrong session cannot
        // undo authoritative closure or clear a still-open sibling.
        activity.invalidate();
        activity.apply_tail(target(&activity, closed), &old);
        activity.sync_text_phases(&old_phases);
        activity.sync_text_phases(&[
            ChildTextPhase {
                run_id: held.run_id.clone(),
                session_id: closed.session_id.clone(),
                sequence: i64::MAX,
                responding: false,
            },
            ChildTextPhase {
                run_id: closed.run_id.clone(),
                session_id: held.session_id.clone(),
                sequence: i64::MAX,
                responding: false,
            },
        ]);
        assert_eq!(activity.indicator(closed, Color::Reset).1, "");
        assert_eq!(activity.indicator(held, Color::Reset).1, " · Responding");
        emit(closed, delta());
        let reopened = engine
            .conversation_activity(&session)
            .await
            .unwrap()
            .child_text;
        activity.sync_text_phases(&reopened);
        assert_eq!(activity.child(closed).unwrap().after, watermark);
        assert_eq!(activity.indicator(closed, Color::Reset).1, " · Responding");
    }

    #[tokio::test]
    async fn hidden_and_reduced_motion_never_start_recovery_and_batch_is_bounded() {
        let engine = crate::engine::fake::FakeEngine::default();
        let tasks: Vec<_> = (0..24).map(task).collect();
        let mut activity = Activity::default();
        activity.catalog(&tasks);
        activity.recover(&engine, tasks.iter()); // Reduced motion.
        assert!(activity.recovery.is_none());
        activity.set_animated(true);
        activity.recover(&engine, std::iter::empty()); // Hidden or clipped.
        assert!(activity.recovery.is_none());

        activity.recover(&engine, tasks.iter().skip(5)); // Viewport scroll/resize.
        let queued = activity.recovery.as_ref().unwrap();
        assert_eq!(queued.targets.len(), 4);
        assert_eq!(queued.targets[0].identity.0, "session-5");
        activity.invalidate(); // Even lag must wait for the in-flight result.
        assert!(activity.recovery.as_ref().unwrap().stale);
        activity.recover(&engine, tasks.iter());
        if let Some(queued) = &activity.recovery {
            assert!(queued.stale);
            assert_eq!(queued.targets[0].identity.0, "session-5");
        }
    }
}
