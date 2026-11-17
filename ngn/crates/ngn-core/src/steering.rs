//! Run-scoped operator steering. Never a next-run queue or a replay mechanism.

use anyhow::{Context, Result, ensure};
use ngn_protocol::{RunStatus, SteeringInput};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::store::{Run, Store, now_ms};

/// Lifetime admissions per activation, including already applied receipts.
pub const MAX_RUN_INPUTS: usize = 32;
/// Host-wide inputs awaiting persistence confirmation.
pub const MAX_HOST_INPUTS: usize = 256;
pub const MAX_PROMPT_BYTES: usize = 32_000;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SteeringStatus {
    Pending,
    Offered,
    Applied,
    Uncertain,
    Undelivered,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SteeringReceipt {
    pub input_id: String,
    pub request_id: String,
    pub run_id: String,
    pub session_id: String,
    pub position: i64,
    pub prompt: String,
    pub status: SteeringStatus,
    pub boundary_id: String,
    pub accepted_at_ms: u64,
    pub updated_at_ms: u64,
}

impl SteeringReceipt {
    pub(crate) fn input(&self) -> SteeringInput {
        SteeringInput {
            input_id: self.input_id.clone(),
            request_id: self.request_id.clone(),
            position: self.position,
            prompt: self.prompt.clone(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct SteeringState {
    pub run_id: String,
    pub session_id: String,
    pub run_status: RunStatus,
    pub supported: bool,
    pub accepting: bool,
    pub receipts: Vec<SteeringReceipt>,
    pub run_limit: usize,
    pub host_limit: usize,
    pub prompt_byte_limit: usize,
}

const SELECT: &str = "SELECT input_id, request_id, run_id, session_id, position, prompt, status, boundary_id, accepted_at_ms, updated_at_ms FROM steering_inputs";

fn receipt(row: &rusqlite::Row<'_>) -> rusqlite::Result<SteeringReceipt> {
    let status: String = row.get(6)?;
    let status = serde_json::from_value(serde_json::Value::String(status)).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(6, rusqlite::types::Type::Text, Box::new(error))
    })?;
    Ok(SteeringReceipt {
        input_id: row.get(0)?,
        request_id: row.get(1)?,
        run_id: row.get(2)?,
        session_id: row.get(3)?,
        position: row.get(4)?,
        prompt: row.get(5)?,
        status,
        boundary_id: row.get(7)?,
        accepted_at_ms: row.get(8)?,
        updated_at_ms: row.get(9)?,
    })
}

/// An offer may have reached persistence before an interruption. Never replay it.
pub(crate) fn settle(db: &Connection, run: Option<&str>) -> Result<()> {
    db.execute(
        "UPDATE steering_inputs SET status = CASE status WHEN 'offered' THEN 'uncertain' ELSE 'undelivered' END, updated_at_ms = ? WHERE status IN ('pending', 'offered') AND (? IS NULL OR run_id = ?)",
        params![now_ms(), run, run],
    )?;
    Ok(())
}

impl Store {
    pub(crate) async fn steering_run(&self, run_id: &str) -> Result<Run> {
        let run_id = run_id.to_owned();
        self.with(move |db| {
            db.query_row(
                "SELECT id, session_id, request_id, status FROM runs WHERE id = ?",
                [run_id],
                |row| {
                    let status: String = row.get(3)?;
                    Ok((row.get(0)?, row.get(1)?, row.get(2)?, status))
                },
            )
            .optional()?
            .map(|(id, session_id, request_id, status)| -> Result<Run> {
                Ok(Run {
                    id,
                    session_id,
                    request_id,
                    status: serde_json::from_value(serde_json::Value::String(status))?,
                })
            })
            .transpose()?
            .context("Run not found in this workspace")
        })
        .await
    }

    pub(crate) async fn existing_steering(
        &self,
        run: &str,
        prompt: &str,
        request: &str,
    ) -> Result<Option<SteeringReceipt>> {
        let (run, prompt, request) = (run.to_owned(), prompt.to_owned(), request.to_owned());
        self.with(move |db| {
            let saved = db
                .query_row(
                    &format!("{SELECT} WHERE request_id = ?"),
                    [request],
                    receipt,
                )
                .optional()?;
            if let Some(saved) = &saved {
                ensure!(
                    saved.run_id == run && saved.prompt == prompt,
                    "Steering request_id already belongs to another run or prompt"
                );
            }
            Ok(saved)
        })
        .await
    }

    pub(crate) async fn admit_steering(
        &self,
        run: &Run,
        prompt: &str,
        request: &str,
    ) -> Result<SteeringReceipt> {
        let (run, prompt, request) = (run.clone(), prompt.to_owned(), request.to_owned());
        self.with(move |db| {
            let tx = db.transaction()?;
            let root: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM runs r WHERE r.id = ? AND r.session_id = ? AND r.status = 'running' AND NOT EXISTS(SELECT 1 FROM subagents t WHERE t.child_run_id = r.id))", params![run.id, run.session_id], |row| row.get(0))?;
            ensure!(root && run.status == RunStatus::Running, "Steering requires the exact running root");
            let per_run: usize = tx.query_row("SELECT COUNT(*) FROM steering_inputs WHERE run_id = ?", [&run.id], |row| row.get(0))?;
            let total: usize = tx.query_row("SELECT COUNT(*) FROM steering_inputs WHERE status IN ('pending', 'offered')", [], |row| row.get(0))?;
            ensure!(per_run < MAX_RUN_INPUTS, "Run steering limit reached (32 lifetime inputs)");
            ensure!(total < MAX_HOST_INPUTS, "Host steering limit reached (256 unconfirmed inputs)");
            let id = format!("steer-{}", Uuid::new_v4().simple());
            let now = now_ms();
            tx.execute("INSERT INTO steering_inputs(input_id, request_id, run_id, session_id, prompt, status, boundary_id, accepted_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?)", params![id, request, run.id, run.session_id, prompt, now, now])?;
            let saved = tx.query_row(&format!("{SELECT} WHERE input_id = ?"), [id], receipt)?;
            tx.commit()?;
            Ok(saved)
        }).await
    }

    pub(crate) async fn steering_receipts(&self, run: &str) -> Result<Vec<SteeringReceipt>> {
        let run = run.to_owned();
        self.with(move |db| {
            let mut query = db.prepare(&format!("{SELECT} WHERE run_id = ? ORDER BY position"))?;
            Ok(query.query_map([run], receipt)?.collect::<Result<_, _>>()?)
        })
        .await
    }

    /// Commit the offer before the caller writes any bytes to runtime stdin.
    pub(crate) async fn offer_steering(
        &self,
        run: &str,
        boundary: &str,
    ) -> Result<Option<SteeringReceipt>> {
        let (run, boundary) = (run.to_owned(), boundary.to_owned());
        self.with(move |db| {
            let tx = db.transaction()?;
            let inflight: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM steering_inputs WHERE run_id = ? AND status = 'offered')", [&run], |row| row.get(0))?;
            ensure!(!inflight, "Runtime requested a boundary before acknowledging its offer");
            let saved = tx.query_row(&format!("{SELECT} WHERE run_id = ? AND status = 'pending' ORDER BY position LIMIT 1"), [&run], receipt).optional()?;
            let offered = if let Some(saved) = saved {
                tx.execute("UPDATE steering_inputs SET status = 'offered', boundary_id = ?, updated_at_ms = ? WHERE input_id = ?", params![boundary, now_ms(), saved.input_id])?;
                Some(tx.query_row(&format!("{SELECT} WHERE input_id = ?"), [&saved.input_id], receipt)?)
            } else { None };
            tx.commit()?;
            Ok(offered)
        }).await
    }

    pub(crate) async fn apply_steering(
        &self,
        run: &str,
        boundary: &str,
        input: &str,
    ) -> Result<()> {
        let (run, boundary, input) = (run.to_owned(), boundary.to_owned(), input.to_owned());
        self.with(move |db| {
            let changed = db.execute("UPDATE steering_inputs SET status = 'applied', updated_at_ms = ? WHERE run_id = ? AND boundary_id = ? AND input_id = ? AND status = 'offered'", params![now_ms(), run, boundary, input])?;
            ensure!(changed == 1, "Runtime acknowledgment is not bound to its offered steering input");
            Ok(())
        }).await
    }

    pub(crate) async fn settle_steering(&self, run: &str) -> Result<()> {
        let run = run.to_owned();
        self.with(move |db| settle(db, Some(&run))).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn crash_and_failure_never_replay_an_offer_or_pending_input_and_retries_survive_restart()
    {
        for status in [None, Some(RunStatus::Failed), Some(RunStatus::Cancelled)] {
            let directory = tempfile::tempdir().unwrap();
            let store = Store::open(directory.path()).unwrap();
            let session = store.create_session().await.unwrap();
            let (run, _) = store.accept(&session.id, "start", "initial").await.unwrap();
            let first = store
                .admit_steering(&run, "offered", &Uuid::new_v4().to_string())
                .await
                .unwrap();
            let second = store
                .admit_steering(&run, "pending", &Uuid::new_v4().to_string())
                .await
                .unwrap();
            store.offer_steering(&run.id, "boundary").await.unwrap();
            if let Some(status) = status {
                store.finish(&run, status, "", "redacted").await.unwrap();
            }
            drop(store);
            let store = Store::open(directory.path()).unwrap();
            let saved = store.steering_receipts(&run.id).await.unwrap();
            assert_eq!(saved[0].status, SteeringStatus::Uncertain);
            assert_eq!(saved[1].status, SteeringStatus::Undelivered);
            for original in [&first, &second] {
                assert_eq!(
                    store
                        .existing_steering(&run.id, &original.prompt, &original.request_id)
                        .await
                        .unwrap()
                        .unwrap()
                        .input_id,
                    original.input_id
                );
            }
            assert!(
                store
                    .apply_steering(&run.id, "boundary", &first.input_id)
                    .await
                    .is_err()
            );
            assert!(
                store
                    .offer_steering(&run.id, "replay")
                    .await
                    .unwrap()
                    .is_none()
            );
            let (next, _) = store
                .accept(&session.id, "next", "explicit new run")
                .await
                .unwrap();
            assert!(
                store
                    .offer_steering(&next.id, "new-boundary")
                    .await
                    .unwrap()
                    .is_none()
            );
            assert!(
                store
                    .existing_steering(&next.id, &first.prompt, &first.request_id)
                    .await
                    .is_err()
            );
        }
    }

    #[tokio::test]
    async fn bounded_receipts_are_separate_from_queue_idempotency() {
        let directory = tempfile::tempdir().unwrap();
        let store = Store::open(directory.path()).unwrap();
        let mut first = None;
        for _ in 0..8 {
            let session = store.create_session().await.unwrap();
            let request = Uuid::new_v4().to_string();
            let (run, _) = store
                .accept(&session.id, &request, "initial")
                .await
                .unwrap();
            // Reusing a normal-run UUID does not reuse its receipt namespace.
            let accepted = store.admit_steering(&run, "steer", &request).await.unwrap();
            first.get_or_insert(accepted);
            for _ in 1..MAX_RUN_INPUTS {
                store
                    .admit_steering(&run, "pending", &Uuid::new_v4().to_string())
                    .await
                    .unwrap();
            }
            assert!(
                store
                    .admit_steering(&run, "overflow", &Uuid::new_v4().to_string())
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("Run steering limit")
            );
        }
        let session = store.create_session().await.unwrap();
        let (run, _) = store.accept(&session.id, "ninth", "initial").await.unwrap();
        assert!(
            store
                .admit_steering(&run, "overflow", &Uuid::new_v4().to_string())
                .await
                .unwrap_err()
                .to_string()
                .contains("Host steering limit")
        );
        let first = first.unwrap();
        assert_eq!(
            store
                .existing_steering(&first.run_id, &first.prompt, &first.request_id)
                .await
                .unwrap()
                .unwrap(),
            first
        );
    }
}
