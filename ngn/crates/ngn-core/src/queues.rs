//! Durable operator inputs, not runtime steering or automatically retried runs.

use anyhow::{Context, Result, ensure};
use ngn_protocol::{Event, EventRecord, RunStatus};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::store::{Run, Store, insert_record, now_ms};

pub const MAX_SESSION_INPUTS: usize = 32;
pub const MAX_HOST_INPUTS: usize = 256;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueueEntry {
    pub run: Run,
    pub prompt: String,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub struct QueueState {
    pub session_id: String,
    pub paused: bool,
    pub entries: Vec<QueueEntry>,
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueueAction {
    Pause,
    Resume,
    Clear,
}

pub(crate) struct PendingInput {
    pub entry: QueueEntry,
    pub interactive: bool,
}

const INPUT_SELECT: &str = "SELECT r.id, r.session_id, r.request_id, r.prompt, q.interactive FROM input_queue q JOIN runs r ON r.id = q.run_id";

fn pending_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<PendingInput> {
    Ok(PendingInput {
        entry: QueueEntry {
            run: Run {
                id: row.get(0)?,
                session_id: row.get(1)?,
                request_id: row.get(2)?,
                status: RunStatus::Queued,
            },
            prompt: row.get(3)?,
        },
        interactive: row.get(4)?,
    })
}

/// Recovery and shutdown stop only unstarted inputs. Started work is never requeued.
pub(crate) fn pause_pending(db: &Connection) -> Result<Vec<EventRecord>> {
    let sessions: Vec<String> = {
        let mut statement = db.prepare("SELECT s.session_id FROM session_queues s WHERE s.paused = 0 AND EXISTS(SELECT 1 FROM input_queue q JOIN runs r ON r.id = q.run_id WHERE r.session_id = s.session_id)")?;
        statement
            .query_map([], |row| row.get(0))?
            .collect::<Result<_, _>>()?
    };
    sessions
        .into_iter()
        .map(|session| {
            db.execute(
                "UPDATE session_queues SET paused = 1 WHERE session_id = ?",
                [&session],
            )?;
            insert_record(db, &session, None, Event::QueueUpdated { paused: true })
        })
        .collect()
}

impl Store {
    pub(crate) async fn queue(&self, session: &str) -> Result<QueueState> {
        let session = session.to_owned();
        self.with(move |db| {
            let paused = db.query_row(
                "SELECT COALESCE((SELECT paused FROM session_queues WHERE session_id = ?), 0)",
                [&session],
                |row| row.get(0),
            )?;
            let mut statement = db.prepare(&format!(
                "{INPUT_SELECT} WHERE r.session_id = ? ORDER BY q.position"
            ))?;
            let entries = statement
                .query_map([&session], |row| Ok(pending_row(row)?.entry))?
                .collect::<Result<_, _>>()?;
            Ok(QueueState {
                session_id: session,
                paused,
                entries,
            })
        })
        .await
    }

    pub(crate) async fn enqueue(
        &self,
        session: &str,
        prompt: &str,
        request: &str,
        interactive: bool,
    ) -> Result<(Run, EventRecord)> {
        let (session, prompt, request) =
            (session.to_owned(), prompt.to_owned(), request.to_owned());
        self.with(move |db| {
            let transaction = db.transaction()?;
            let (total, per_session): (usize, usize) = transaction.query_row(
                "SELECT COUNT(*), COALESCE(SUM(r.session_id = ?), 0) FROM input_queue q JOIN runs r ON r.id = q.run_id", [&session], |row| Ok((row.get(0)?, row.get(1)?)))?;
            ensure!(per_session < MAX_SESSION_INPUTS, "Session queue limit reached (32 pending inputs)");
            ensure!(total < MAX_HOST_INPUTS, "Host queue limit reached (256 pending inputs)");
            let run = Run { id: format!("run-{}", Uuid::new_v4().simple()), session_id: session.clone(), request_id: request, status: RunStatus::Queued };
            transaction.execute("INSERT INTO runs VALUES (?, ?, ?, ?, 'queued')", params![run.id, session, run.request_id, prompt])?;
            transaction.execute("INSERT INTO session_queues(session_id) VALUES (?) ON CONFLICT DO NOTHING", [&session])?;
            transaction.execute("INSERT INTO input_queue(run_id, interactive) VALUES (?, ?)", params![run.id, interactive])?;
            let title: String = prompt.lines().next().unwrap_or("New session").chars().take(80).collect();
            transaction.execute("UPDATE sessions SET title = CASE WHEN title = 'New session' THEN ? ELSE title END, updated_at_ms = ? WHERE id = ?", params![title, now_ms(), session])?;
            let record = insert_record(&transaction, &session, Some(&run.id), Event::RunQueued { prompt })?;
            transaction.commit()?;
            Ok((run, record))
        }).await
    }

    /// One FIFO head per ready session, globally ordered for bounded host admission.
    pub(crate) async fn ready_inputs(&self) -> Result<Vec<PendingInput>> {
        self.with(|db| {
            let mut statement = db.prepare(&format!("{INPUT_SELECT} JOIN session_queues s ON s.session_id = r.session_id
                WHERE s.paused = 0 AND r.status = 'queued'
                AND NOT EXISTS(SELECT 1 FROM runs busy WHERE busy.session_id = r.session_id AND busy.status = 'running')
                AND q.position = (SELECT MIN(q2.position) FROM input_queue q2 JOIN runs r2 ON r2.id = q2.run_id WHERE r2.session_id = r.session_id)
                ORDER BY q.position"))?;
            Ok(statement.query_map([], pending_row)?.collect::<Result<_, _>>()?)
        }).await
    }

    pub(crate) async fn claim_input(&self, run: &Run) -> Result<EventRecord> {
        let run = run.clone();
        self.with(move |db| {
            let transaction = db.transaction()?;
            let prompt: String = transaction
                .query_row(
                    "SELECT r.prompt FROM runs r JOIN input_queue q ON q.run_id = r.id JOIN session_queues s ON s.session_id = r.session_id
                     WHERE r.id = ? AND r.status = 'queued' AND s.paused = 0
                     AND NOT EXISTS(SELECT 1 FROM runs busy WHERE busy.session_id = r.session_id AND busy.status = 'running')
                     AND q.position = (SELECT MIN(q2.position) FROM input_queue q2 JOIN runs r2 ON r2.id = q2.run_id WHERE r2.session_id = r.session_id)",
                    [&run.id],
                    |row| row.get(0),
                )
                .context("Input is not the ready FIFO head of an idle session")?;
            transaction.execute("DELETE FROM input_queue WHERE run_id = ?", [&run.id])?;
            transaction.execute("UPDATE runs SET status = 'running' WHERE id = ?", [&run.id])?;
            let record = insert_record(
                &transaction,
                &run.session_id,
                Some(&run.id),
                Event::RunStarted { prompt },
            )?;
            transaction.commit()?;
            Ok(record)
        })
        .await
    }

    pub(crate) async fn queue_control(
        &self,
        session: &str,
        action: QueueAction,
    ) -> Result<Vec<EventRecord>> {
        let session = session.to_owned();
        self.with(move |db| {
            let transaction = db.transaction()?;
            let paused = !matches!(action, QueueAction::Resume);
            transaction.execute("INSERT INTO session_queues(session_id, paused) VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET paused = excluded.paused", params![session, paused])?;
            let mut records = Vec::new();
            if matches!(action, QueueAction::Clear) {
                let inputs = {
                    let mut statement = transaction.prepare(&format!("{INPUT_SELECT} WHERE r.session_id = ? ORDER BY q.position"))?;
                    statement.query_map([&session], pending_row)?.collect::<Result<Vec<_>, _>>()?
                };
                for input in inputs {
                    let run = &input.entry.run;
                    transaction.execute("DELETE FROM input_queue WHERE run_id = ?", [&run.id])?;
                    transaction.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", [&run.id])?;
                    records.push(insert_record(&transaction, &session, Some(&run.id), Event::RunFinished { status: RunStatus::Cancelled, error: "Queued input cleared before execution".into() })?);
                }
            }
            records.push(insert_record(&transaction, &session, None, Event::QueueUpdated { paused })?);
            transaction.commit()?;
            Ok(records)
        }).await
    }

    pub(crate) async fn cancel_input(&self, id: &str) -> Result<Vec<EventRecord>> {
        let id = id.to_owned();
        self.with(move |db| {
            let transaction = db.transaction()?;
            let session: String = transaction.query_row("SELECT r.session_id FROM input_queue q JOIN runs r ON r.id = q.run_id WHERE r.id = ? AND r.status = 'queued'", [&id], |row| row.get(0)).context("Run is neither active nor queued")?;
            transaction.execute("DELETE FROM input_queue WHERE run_id = ?", [&id])?;
            transaction.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", [&id])?;
            let record = insert_record(&transaction, &session, Some(&id), Event::RunFinished { status: RunStatus::Cancelled, error: "Queued input cancelled before execution".into() })?;
            transaction.commit()?;
            Ok(vec![record])
        }).await
    }

    pub(crate) async fn pause_pending(&self) -> Result<Vec<EventRecord>> {
        self.with(|db| {
            let transaction = db.transaction()?;
            let records = pause_pending(&transaction)?;
            transaction.commit()?;
            Ok(records)
        })
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ApprovalMode, Config, Host, config::Backend};
    use std::{sync::Arc, time::Duration};

    fn host(directory: &std::path::Path) -> Arc<Host> {
        Host::open(
            Config {
                workspace: directory.into(),
                data_dir: directory.join("state"),
                backend: Backend::Demo,
                ..Config::default()
            }
            .resolve()
            .unwrap(),
        )
        .unwrap()
    }

    async fn submit(host: &Arc<Host>, session: &str, prompt: &str) -> Run {
        host.submit(
            session,
            prompt,
            &Uuid::new_v4().to_string(),
            ApprovalMode::Deny,
        )
        .await
        .unwrap()
    }

    async fn terminal(host: &Host, run: &Run, prompt: &str) -> RunStatus {
        tokio::time::timeout(Duration::from_secs(8), async {
            loop {
                let saved = host
                    .store
                    .existing_request(&run.session_id, &run.request_id, prompt)
                    .await
                    .unwrap()
                    .unwrap();
                if !matches!(saved.status, RunStatus::Running | RunStatus::Queued) {
                    return saved.status;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap()
    }

    #[tokio::test]
    async fn fifo_and_concurrent_deduplication_preserve_approval_mode() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let session = host.store.create_session().await.unwrap().id;
        host.control_queue(&session, QueueAction::Pause)
            .await
            .unwrap();
        let request = Uuid::new_v4().to_string();
        let (left, right) = tokio::join!(
            host.submit(&session, "demo approval", &request, ApprovalMode::Deny),
            host.submit(&session, "demo approval", &request, ApprovalMode::Deny)
        );
        let first = left.unwrap();
        assert_eq!(first.id, right.unwrap().id);
        assert_eq!(first.status, RunStatus::Queued);
        assert_eq!(
            host.submit(
                &session,
                "demo approval",
                &request,
                ApprovalMode::Interactive
            )
            .await
            .unwrap()
            .id,
            first.id
        );
        assert!(
            host.submit(&session, "different", &request, ApprovalMode::Deny)
                .await
                .is_err()
        );
        // Reusing the request cannot upgrade the already stored noninteractive policy.
        let second = submit(&host, &session, "demo second").await;
        let third = submit(&host, &session, "demo third").await;
        assert_eq!(host.queue(&session).await.unwrap().entries.len(), 3);
        assert!(host.active().await.is_empty());
        assert!(
            host.start(
                &session,
                "bypass",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny
            )
            .await
            .is_err()
        );
        host.control_queue(&session, QueueAction::Resume)
            .await
            .unwrap();
        assert_eq!(
            terminal(&host, &third, "demo third").await,
            RunStatus::Completed
        );
        let records = host.store.events(&session, 0).await.unwrap();
        let starts: Vec<_> = records
            .iter()
            .filter(|record| matches!(record.event, Event::RunStarted { .. }))
            .map(|record| record.run_id.as_str())
            .collect();
        assert_eq!(
            starts,
            [first.id.as_str(), second.id.as_str(), third.id.as_str()]
        );
        assert!(records.iter().any(|record| matches!(&record.event, Event::ToolCompleted { result, .. } if result.name == "demo_preview" && result.error.contains("non-interactive"))));
        assert!(host.approvals().await.is_empty());
        assert!(host.queue(&session).await.unwrap().entries.is_empty());
        host.shutdown().await;
    }

    #[tokio::test]
    async fn cancellation_and_clear_do_not_execute_pending_inputs_or_cancel_other_work() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let session = host.store.create_session().await.unwrap().id;
        let root = submit(&host, &session, "demo hold").await;
        let remove = submit(&host, &session, "remove me").await;
        let retained = submit(&host, &session, "demo retained").await;
        host.cancel(&remove.id).await.unwrap();
        assert_eq!(
            terminal(&host, &remove, "remove me").await,
            RunStatus::Cancelled
        );
        assert!(!host.queue(&session).await.unwrap().paused);
        assert!(host.active().await.iter().any(|run| run.id == root.id));
        host.cancel(&root.id).await.unwrap();
        assert_eq!(
            terminal(&host, &root, "demo hold").await,
            RunStatus::Cancelled
        );
        let queue = host.queue(&session).await.unwrap();
        assert!(queue.paused);
        assert_eq!(queue.entries[0].run.id, retained.id);
        assert!(host.active().await.is_empty());
        let other = host.store.create_session().await.unwrap().id;
        let independent = submit(&host, &other, "demo hold").await;
        host.control_queue(&session, QueueAction::Clear)
            .await
            .unwrap();
        assert_eq!(
            terminal(&host, &retained, "demo retained").await,
            RunStatus::Cancelled
        );
        let duplicate = host
            .submit(
                &session,
                "demo retained",
                &retained.request_id,
                ApprovalMode::Deny,
            )
            .await
            .unwrap();
        assert_eq!(duplicate.status, RunStatus::Cancelled);
        assert!(
            host.active()
                .await
                .iter()
                .any(|run| run.id == independent.id)
        );
        let records = host.store.events(&session, 0).await.unwrap();
        assert!(
            records
                .iter()
                .filter(|record| matches!(record.event, Event::RunStarted { .. }))
                .all(|record| record.run_id == root.id)
        );
        let next = submit(&host, &session, "demo next").await;
        assert_eq!(next.status, RunStatus::Queued);
        host.control_queue(&session, QueueAction::Resume)
            .await
            .unwrap();
        assert_eq!(
            terminal(&host, &next, "demo next").await,
            RunStatus::Completed
        );
        host.shutdown().await;
    }

    #[tokio::test]
    async fn failed_runs_pause_the_queue_and_children_cannot_submit_or_control_it() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let session = host.store.create_session().await.unwrap().id;
        host.control_queue(&session, QueueAction::Pause)
            .await
            .unwrap();
        let failed = submit(&host, &session, "demo orphan").await;
        let pending = submit(&host, &session, "demo next").await;
        host.control_queue(&session, QueueAction::Resume)
            .await
            .unwrap();
        assert_eq!(
            terminal(&host, &failed, "demo orphan").await,
            RunStatus::Failed
        );
        let queue = host.queue(&session).await.unwrap();
        assert!(queue.paused);
        assert_eq!(queue.entries[0].run.id, pending.id);
        let child = host.store.tasks(&session).await.unwrap().remove(0);
        assert!(
            host.submit(
                &child.session_id,
                "escalate",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Interactive
            )
            .await
            .is_err()
        );
        assert!(
            host.control_queue(&child.session_id, QueueAction::Resume)
                .await
                .is_err()
        );
        host.control_queue(&session, QueueAction::Resume)
            .await
            .unwrap();
        assert_eq!(
            terminal(&host, &pending, "demo next").await,
            RunStatus::Completed
        );
        host.shutdown().await;
    }

    #[tokio::test]
    async fn global_capacity_dispatches_waiting_sessions_after_a_slot_is_freed() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let mut roots = Vec::new();
        for _ in 0..8 {
            let session = host.store.create_session().await.unwrap();
            roots.push(submit(&host, &session.id, "demo hold").await);
        }
        let first = host.store.create_session().await.unwrap();
        let first = submit(&host, &first.id, "demo hold").await;
        let second = host.store.create_session().await.unwrap();
        let second = submit(&host, &second.id, "demo hold").await;
        assert_eq!(first.status, RunStatus::Queued);
        assert_eq!(second.status, RunStatus::Queued);
        host.cancel(&roots[0].id).await.unwrap();
        terminal(&host, &roots[0], "demo hold").await;
        let active = host.active().await;
        assert_eq!(active.len(), 8);
        assert!(active.iter().any(|run| run.id == first.id));
        assert!(!active.iter().any(|run| run.id == second.id));
        host.shutdown().await;
        assert!(host.queue(&second.session_id).await.unwrap().paused);
        assert!(
            host.queue(&second.session_id)
                .await
                .unwrap()
                .entries
                .iter()
                .any(|entry| entry.run.id == second.id)
        );
    }

    #[tokio::test]
    async fn queue_limits_are_atomic_and_full_queue_retries_still_deduplicate() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let host = host(directory.path());
        let session = host.store.create_session().await.unwrap().id;
        host.control_queue(&session, QueueAction::Pause)
            .await
            .unwrap();
        let first = submit(&host, &session, "first").await;
        for _ in 1..MAX_SESSION_INPUTS {
            submit(&host, &session, "pending").await;
        }
        assert!(
            host.submit(
                &session,
                "overflow",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny
            )
            .await
            .unwrap_err()
            .to_string()
            .contains("queue limit")
        );
        assert_eq!(
            host.submit(&session, "first", &first.request_id, ApprovalMode::Deny)
                .await
                .unwrap()
                .id,
            first.id
        );
        for _ in 1..(MAX_HOST_INPUTS / MAX_SESSION_INPUTS) {
            let session = host.store.create_session().await.unwrap().id;
            host.control_queue(&session, QueueAction::Pause)
                .await
                .unwrap();
            for _ in 0..MAX_SESSION_INPUTS {
                submit(&host, &session, "pending").await;
            }
        }
        let other = host.store.create_session().await.unwrap().id;
        assert!(
            host.submit(
                &other,
                "overflow",
                &Uuid::new_v4().to_string(),
                ApprovalMode::Deny
            )
            .await
            .unwrap_err()
            .to_string()
            .contains("Host queue limit")
        );
        assert!(host.queue(&other).await.unwrap().entries.is_empty());
        host.shutdown().await;
    }

    #[tokio::test]
    async fn restart_keeps_unstarted_inputs_paused_and_never_replays_claimed_work() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let store = Store::open(directory.path()).unwrap();
        let session = store.create_session().await.unwrap().id;
        let (started, _) = store
            .enqueue(&session, "once", "started", false)
            .await
            .unwrap();
        store.claim_input(&started).await.unwrap();
        let (queued, _) = store
            .enqueue(&session, "later", "pending", true)
            .await
            .unwrap();
        drop(store); // Simulate host loss after the atomic claim but before completion.
        let store = Store::open(directory.path()).unwrap();
        assert_eq!(
            store
                .existing_request(&session, "started", "once")
                .await
                .unwrap()
                .unwrap()
                .status,
            RunStatus::Interrupted
        );
        let queue = store.queue(&session).await.unwrap();
        assert!(queue.paused);
        assert_eq!(queue.entries[0].run.id, queued.id);
        assert!(store.ready_inputs().await.unwrap().is_empty());
        let records = store.events(&session, 0).await.unwrap();
        assert_eq!(
            records
                .iter()
                .filter(|record| matches!(record.event, Event::RunStarted { .. }))
                .count(),
            1
        );
        drop(store);
        let store = Store::open(directory.path()).unwrap();
        assert_eq!(
            store.events(&session, 0).await.unwrap().len(),
            records.len()
        );
        store
            .queue_control(&session, QueueAction::Resume)
            .await
            .unwrap();
        let ready = store.ready_inputs().await.unwrap();
        assert_eq!(ready.len(), 1);
        assert!(ready[0].interactive);
        assert_eq!(ready[0].entry.run.id, queued.id);
    }

    #[tokio::test]
    async fn schema_two_upgrade_preserves_cursors_and_allows_empty_session_queue_events() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let db = Connection::open(directory.path().join("host.sqlite3")).unwrap();
        db.execute_batch("PRAGMA user_version=2;
            CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(session_id, request_id));
            CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX session_events ON events(session_id, sequence);
            INSERT INTO sessions VALUES ('session-old', 'Preserved', 1);
            INSERT INTO runs VALUES ('run-old', 'session-old', 'request-old', 'old', 'completed');
            INSERT INTO events VALUES (71, 'run-old', 'session-old', 1, '{\"event\":\"run_started\",\"prompt\":\"old\"}');").unwrap();
        drop(db);
        std::fs::write(
            directory.path().join("nagents.sqlite3"),
            b"opaque-runtime-state",
        )
        .unwrap();
        let store = Store::open(directory.path()).unwrap();
        let events = store.events("session-old", 0).await.unwrap();
        assert_eq!(events[0].sequence, 71);
        let empty = store.create_session().await.unwrap();
        let records = store
            .queue_control(&empty.id, QueueAction::Pause)
            .await
            .unwrap();
        assert!(records[0].sequence > 71);
        assert!(records[0].run_id.is_empty());
        assert!(matches!(
            store.events(&empty.id, 0).await.unwrap()[0].event,
            Event::QueueUpdated { paused: true }
        ));
        assert_eq!(
            std::fs::read(directory.path().join("nagents.sqlite3")).unwrap(),
            b"opaque-runtime-state"
        );
    }
}
