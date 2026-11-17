use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Context, Result, ensure};
use ngn_protocol::{Event, EventRecord, RunStatus, ToolCall, ToolResult};
use serde::Serialize;
use serde_json::Value;
use tokio::sync::{Mutex, broadcast, oneshot};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::{
    Config,
    config::Backend,
    runner,
    store::{Run, Store},
    tools::Tools,
};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ApprovalMode {
    Deny,
    Interactive,
}

struct ActiveRun {
    run: Run,
    cancel: CancellationToken,
}

#[derive(Clone, Serialize)]
pub struct Approval {
    pub id: String,
    pub run_id: String,
    pub session_id: String,
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
            !self.closed.load(Ordering::Acquire),
            "Host is shutting down"
        );
        ensure!(
            !prompt.trim().is_empty() && prompt.len() <= 32_000,
            "Prompt must contain 1..32000 UTF-8 bytes"
        );
        Uuid::parse_str(request_id).context(
            "request_id must be a UUID; reuse it only when retrying the same submission",
        )?;
        self.store.session(session).await?;
        let mut active = self.active.lock().await;
        // Serialize idempotency and admission together, not only task creation.
        if let Some(existing) = self
            .store
            .existing_request(session, request_id, prompt)
            .await?
        {
            return Ok(existing);
        }
        ensure!(
            !active.values().any(|run| run.run.session_id == session),
            "Session already has an active run"
        );
        ensure!(active.len() < 8, "Host already has eight active runs");
        if self.config.backend == Backend::Nagents {
            self.config.selected_provider()?;
        }
        let _emission = self.emission.lock().await;
        let (run, record) = self.store.accept(session, request_id, prompt).await?;
        let _ = self.events.send(record);
        drop(_emission);
        let cancel = CancellationToken::new();
        active.insert(
            run.id.clone(),
            ActiveRun {
                run: run.clone(),
                cancel: cancel.clone(),
            },
        );
        let host = self.clone();
        let owned_run = run.clone();
        let prompt = prompt.to_owned();
        let mut workers = self.workers.lock().await;
        workers.retain(|worker| !worker.is_finished());
        workers.push(tokio::spawn(async move {
            let result = match host.config.backend {
                Backend::Demo => runner::demo(&host, &owned_run, &prompt, approval, cancel.clone()).await,
                Backend::Nagents => runner::stdio(&host, &owned_run, &prompt, approval, cancel.clone()).await,
            };
            let (status, error) = if cancel.is_cancelled() {
                (RunStatus::Cancelled, String::new())
            } else { match result { Ok((status, error)) => (status, error), Err(error) => (RunStatus::Failed, error.to_string()) } };
            // Commit terminal state and retire ownership before notifying clients.
            // Otherwise a client can observe finished yet retain a stale busy UI.
            host.pending.lock().await.retain(|_, pending| pending.approval.run_id != owned_run.id);
            let mut active = host.active.lock().await;
            let _emission = host.emission.lock().await;
            let final_record = host.store.append(&owned_run, Event::RunFinished { status, error }).await;
            active.remove(&owned_run.id);
            if let Ok(record) = final_record { let _ = host.events.send(record); }
            else {
                eprintln!("ngn: unable to persist final run status; it will be marked interrupted on restart");
            }
        }));
        Ok(run)
    }

    pub async fn emit(&self, run: &Run, event: Event) -> Result<()> {
        let _emission = self.emission.lock().await;
        let record = self.store.append(run, event).await?;
        let _ = self.events.send(record);
        Ok(())
    }

    pub async fn active(&self) -> Vec<Run> {
        self.active
            .lock()
            .await
            .values()
            .map(|active| active.run.clone())
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
        active
            .get(run)
            .context("Run is not active")?
            .cancel
            .cancel();
        Ok(())
    }

    pub async fn tool(
        &self,
        run: &Run,
        call: ToolCall,
        approval_mode: ApprovalMode,
        cancel: CancellationToken,
    ) -> Result<ToolResult> {
        self.emit(run, Event::ToolStarted { call: call.clone() })
            .await?;
        let started = Instant::now();
        let result: Result<Value> = async {
            let tools = self.tools.clone();
            let owned = call.clone();
            let prepared = tokio::task::spawn_blocking(move || tools.prepare(&owned)).await??;
            if prepared.needs_approval {
                ensure!(
                    approval_mode == ApprovalMode::Interactive,
                    "Denied: non-interactive runs cannot grant tool approval"
                );
                let id = format!("approval-{}", Uuid::new_v4().simple());
                let (answer, receive) = oneshot::channel();
                let approval = Approval {
                    id: id.clone(),
                    run_id: run.id.clone(),
                    session_id: run.session_id.clone(),
                    call: call.clone(),
                    preview: prepared.preview.clone(),
                };
                self.pending
                    .lock()
                    .await
                    .insert(id.clone(), Pending { approval, answer });
                if let Err(error) = self
                    .emit(
                        run,
                        Event::ApprovalRequested {
                            approval_id: id.clone(),
                            call: call.clone(),
                            preview: prepared.preview.clone(),
                        },
                    )
                    .await
                {
                    self.pending.lock().await.remove(&id);
                    return Err(error);
                }
                let allowed = tokio::select! {
                    answer = receive => answer.unwrap_or(false),
                    _ = cancel.cancelled() => false,
                    _ = tokio::time::sleep(Duration::from_secs(300)) => false,
                };
                self.pending.lock().await.remove(&id);
                self.emit(
                    run,
                    Event::ApprovalResolved {
                        approval_id: id,
                        allowed,
                    },
                )
                .await?;
                ensure!(
                    allowed && !cancel.is_cancelled(),
                    "Tool approval denied, expired or cancelled"
                );
            }
            self.tools.execute(prepared, cancel).await
        }
        .await;
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
                duration_ms: started.elapsed().as_millis() as u64,
            },
        )
        .await?;
        Ok(result)
    }

    pub async fn shutdown(&self) {
        self.closed.store(true, Ordering::Release);
        let active = self.active.lock().await;
        for active in active.values() {
            active.cancel.cancel();
        }
        drop(active);
        let workers = std::mem::take(&mut *self.workers.lock().await);
        for mut worker in workers {
            if tokio::time::timeout(Duration::from_secs(10), &mut worker)
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
    use std::path::Path;

    #[tokio::test]
    async fn submissions_are_idempotent_and_concurrency_is_per_session() {
        let directory = tempfile::tempdir().unwrap();
        let host = host(directory.path());
        let session = host.store.create_session().await.unwrap();
        let request = Uuid::new_v4().to_string();
        let first = host
            .start(&session.id, "hello", &request, ApprovalMode::Deny)
            .await
            .unwrap();
        let again = host
            .start(&session.id, "hello", &request, ApprovalMode::Deny)
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
        let (run, _) = host
            .store
            .accept(&session.id, "manual", "test")
            .await
            .unwrap();
        let call = ToolCall {
            call_id: "c".into(),
            name: "demo_preview".into(),
            arguments: json!({}),
        };
        let result = host
            .tool(&run, call, ApprovalMode::Deny, CancellationToken::new())
            .await
            .unwrap();
        assert!(result.error.contains("non-interactive"));
        assert!(!directory.path().join("demo.txt").exists());
        assert!(host.approvals().await.is_empty());
    }
}
