use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use ngn_protocol::{Event, EventRecord, RunStatus, TaskInfo, VERSION};
use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::{
    subagents::{ChildAdmission, TaskOutcome},
    tools::bounded,
};

const TASK_SELECT: &str = "SELECT t.child_run_id, t.child_session_id, t.parent_run_id, t.parent_session_id, t.root_run_id, t.root_session_id, t.call_id, t.title, t.depth, t.read_only, r.status, t.result, t.error FROM subagents t JOIN runs r ON r.id = t.child_run_id";

fn task_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<TaskOutcome> {
    let status: String = row.get(10)?;
    let status = serde_json::from_value(serde_json::Value::String(status)).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(10, rusqlite::types::Type::Text, Box::new(error))
    })?;
    Ok(TaskOutcome {
        task: TaskInfo {
            run_id: row.get(0)?,
            session_id: row.get(1)?,
            parent_run_id: row.get(2)?,
            parent_session_id: row.get(3)?,
            root_run_id: row.get(4)?,
            root_session_id: row.get(5)?,
            call_id: row.get(6)?,
            title: row.get(7)?,
            depth: row.get(8)?,
            read_only: row.get(9)?,
            status,
        },
        result: row.get(11)?,
        error: row.get(12)?,
    })
}

fn insert_event(db: &Connection, run: &Run, event: Event) -> Result<EventRecord> {
    insert_record(db, &run.session_id, Some(&run.id), event)
}

pub(crate) fn insert_record(
    db: &Connection,
    session: &str,
    run_id: Option<&str>,
    event: Event,
) -> Result<EventRecord> {
    let timestamp_ms = now_ms();
    db.execute(
        "INSERT INTO events(run_id, session_id, timestamp_ms, payload) VALUES (?, ?, ?, ?)",
        params![
            run_id,
            session,
            timestamp_ms,
            serde_json::to_string(&event)?
        ],
    )?;
    let sequence = db.last_insert_rowid();
    db.execute(
        "UPDATE sessions SET updated_at_ms = ? WHERE id = ?",
        params![timestamp_ms, session],
    )?;
    Ok(EventRecord {
        schema_version: VERSION,
        sequence,
        run_id: run_id.unwrap_or_default().to_owned(),
        session_id: session.to_owned(),
        timestamp_ms,
        event,
    })
}

fn task_run(id: &str, session: &str) -> Run {
    Run {
        id: id.into(),
        session_id: session.into(),
        request_id: String::new(),
        status: RunStatus::Running,
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub id: String,
    pub title: String,
    pub updated_at_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Run {
    pub id: String,
    pub session_id: String,
    pub request_id: String,
    pub status: RunStatus,
}

/// Durable admission identity, even if the saved run status cannot be decoded.
#[derive(Debug, Serialize)]
pub struct InputReceipt {
    pub id: String,
    pub session_id: String,
    pub request_id: String,
    pub status: Option<RunStatus>,
}

impl From<Run> for InputReceipt {
    fn from(run: Run) -> Self {
        Self {
            id: run.id,
            session_id: run.session_id,
            request_id: run.request_id,
            status: Some(run.status),
        }
    }
}

impl InputReceipt {
    pub(crate) fn into_run(self) -> Result<Run> {
        Ok(Run {
            status: self.status.with_context(|| {
                format!(
                    "Input accepted as {}; saved run status is unreadable",
                    self.id
                )
            })?,
            id: self.id,
            session_id: self.session_id,
            request_id: self.request_id,
        })
    }
}

#[derive(Clone)]
pub struct Store {
    connection: Arc<Mutex<Connection>>,
    path: Arc<PathBuf>,
    _lock: Arc<StateLock>,
}

struct StateLock(File);

impl Drop for StateLock {
    fn drop(&mut self) {
        // Explicitly unlock the open-file description. Relying only on close
        // can retain a flock across the fork/exec window of an unrelated shell
        // child on another thread, making immediate workspace reopen flaky.
        let _ = FileExt::unlock(&self.0);
    }
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn private_file(path: &Path) -> Result<File> {
    if let Ok(metadata) = path.symlink_metadata() {
        ensure!(
            metadata.is_file() && !metadata.file_type().is_symlink(),
            "State file must be a regular file, not a symlink"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            ensure!(
                metadata.nlink() == 1,
                "State files must not have hard links"
            );
        }
    }
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    ensure!(
        file.metadata()?.is_file(),
        "State file must be a regular file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    Ok(file)
}

impl Store {
    /// Build a self-contained SQLite file in private staging, never in a live workspace.
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    pub(crate) fn stage_archive(
        directory: &Path,
        write: impl FnOnce(&rusqlite::Transaction<'_>) -> Result<()>,
    ) -> Result<()> {
        let store = Self::open(directory)?;
        let mut connection = store
            .connection
            .lock()
            .map_err(|_| anyhow::anyhow!("State lock poisoned"))?;
        let transaction = connection.transaction()?;
        write(&transaction)?;
        transaction.commit()?;
        let mode: String = connection.query_row("PRAGMA journal_mode=DELETE", [], |r| r.get(0))?;
        ensure!(
            mode == "delete",
            "Cannot close staged Rust state as a single file"
        );
        Ok(())
    }

    pub fn open(directory: &Path) -> Result<Self> {
        fs::create_dir_all(directory)?;
        let metadata = directory.symlink_metadata()?;
        ensure!(
            !metadata.file_type().is_symlink(),
            "State directory cannot be a symlink"
        );
        // SQLite NOFOLLOW rejects symlinks anywhere in the path, including
        // macOS /var and /tmp. Resolve the directory once so the lock, writer
        // and later readers share one spelling even if an alias is retargeted.
        // Never resolve the database or lock file itself.
        let directory = directory.canonicalize()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            let resolved = directory.symlink_metadata()?;
            ensure!(
                resolved.is_dir()
                    && (resolved.dev(), resolved.ino()) == (metadata.dev(), metadata.ino()),
                "State directory changed while resolving its path"
            );
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
        }
        let lock = private_file(&directory.join("host.lock"))?;
        lock.try_lock_exclusive()
            .context("Another ngn process owns this workspace's Rust state; stop it first")?;
        let lock = StateLock(lock);
        let path = directory.join("host.sqlite3");
        private_file(&path)?;
        let mut connection = Connection::open_with_flags(
            &path,
            OpenFlags::default() | OpenFlags::SQLITE_OPEN_NOFOLLOW,
        )?;
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        let version: u32 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
        ensure!(
            version <= 5,
            "Rust host database was created by a newer unsupported schema version"
        );
        connection.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(session_id, request_id));
            CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS session_events ON events(session_id, sequence);
            CREATE TABLE IF NOT EXISTS subagents (
                child_run_id TEXT PRIMARY KEY REFERENCES runs(id), child_session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
                parent_run_id TEXT NOT NULL REFERENCES runs(id), parent_session_id TEXT NOT NULL REFERENCES sessions(id),
                root_run_id TEXT NOT NULL REFERENCES runs(id), root_session_id TEXT NOT NULL REFERENCES sessions(id),
                call_id TEXT NOT NULL, title TEXT NOT NULL, depth INTEGER NOT NULL, read_only INTEGER NOT NULL,
                result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', UNIQUE(parent_run_id, call_id));
            CREATE INDEX IF NOT EXISTS subagent_root ON subagents(root_run_id);
            CREATE INDEX IF NOT EXISTS subagent_parent ON subagents(parent_run_id);")?;
        // Recovery is observable and never re-executes a tool or prompt.
        let transaction = connection.transaction()?;
        if version < 3 {
            // Queue controls belong to sessions, including empty conversations.
            // Preserve every durable cursor while permitting NULL run identities.
            transaction.execute_batch("ALTER TABLE events RENAME TO old_events;
                CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
                INSERT INTO events SELECT * FROM old_events;
                DROP TABLE old_events;
                CREATE INDEX session_events ON events(session_id, sequence);")?;
        }
        transaction.execute_batch("CREATE TABLE IF NOT EXISTS session_queues (session_id TEXT PRIMARY KEY REFERENCES sessions(id), paused INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS input_queue (position INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), interactive INTEGER NOT NULL CHECK(interactive IN (0, 1)));")?;
        transaction.execute_batch("CREATE TABLE IF NOT EXISTS steering_inputs (
            position INTEGER PRIMARY KEY AUTOINCREMENT, input_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL REFERENCES runs(id),
            session_id TEXT NOT NULL REFERENCES sessions(id), prompt TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'offered', 'applied', 'uncertain', 'undelivered')),
            boundary_id TEXT NOT NULL, accepted_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS steering_run ON steering_inputs(run_id, position);
            CREATE INDEX IF NOT EXISTS steering_pending ON steering_inputs(status);")?;
        if version < 5 {
            transaction.execute_batch(
                "CREATE TABLE archived_sessions (
                     session_id TEXT PRIMARY KEY NOT NULL REFERENCES sessions(id),
                     python_id TEXT UNIQUE NOT NULL);
                 CREATE TRIGGER archive_no_runs BEFORE INSERT ON runs
                     WHEN EXISTS (SELECT 1 FROM archived_sessions WHERE session_id = NEW.session_id)
                     BEGIN SELECT RAISE(ABORT, 'Python archives are read-only; start a new session'); END;
                 CREATE TRIGGER archive_no_queue BEFORE INSERT ON session_queues
                     WHEN EXISTS (SELECT 1 FROM archived_sessions WHERE session_id = NEW.session_id)
                     BEGIN SELECT RAISE(ABORT, 'Python archives are read-only; start a new session'); END;",
            )?;
            transaction.pragma_update(None, "user_version", 5)?;
        }
        crate::steering::settle(&transaction, None)?;
        let interrupted: Vec<(String, String)> = {
            let mut statement =
                transaction.prepare("SELECT id, session_id FROM runs WHERE status = 'running'")?;
            statement
                .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<_, _>>()?
        };
        for (run, session) in interrupted {
            let event = Event::RunFinished { status: RunStatus::Interrupted, error: "Host stopped before the run completed; completed tool actions are not rolled back or retried.".into() };
            transaction.execute(
                "INSERT INTO events(run_id, session_id, timestamp_ms, payload) VALUES (?, ?, ?, ?)",
                params![run, session, now_ms(), serde_json::to_string(&event)?],
            )?;
        }
        transaction.execute(
            "UPDATE runs SET status = 'interrupted' WHERE status = 'running'",
            [],
        )?;
        let recovered: Vec<TaskOutcome> = {
            let mut statement = transaction.prepare(&format!(
                "{TASK_SELECT} WHERE r.status = 'interrupted' AND t.error = ''"
            ))?;
            statement
                .query_map([], task_row)?
                .collect::<Result<_, _>>()?
        };
        for mut outcome in recovered {
            outcome.error =
                "Host stopped before the subagent completed; it was not replayed.".into();
            transaction.execute(
                "UPDATE subagents SET result = '', error = ? WHERE child_run_id = ?",
                params![outcome.error, outcome.task.run_id],
            )?;
            let parent = task_run(&outcome.task.parent_run_id, &outcome.task.parent_session_id);
            insert_event(
                &transaction,
                &parent,
                Event::TaskFinished {
                    task: outcome.task.clone(),
                    error: outcome.error,
                },
            )?;
            if outcome.task.root_run_id != outcome.task.parent_run_id {
                let root = task_run(&outcome.task.root_run_id, &outcome.task.root_session_id);
                insert_event(
                    &transaction,
                    &root,
                    Event::TaskUpdated { task: outcome.task },
                )?;
            }
        }
        crate::queues::pause_pending(&transaction)?;
        transaction.commit()?;
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
            path: Arc::new(path),
            _lock: Arc::new(lock),
        })
    }

    pub(crate) async fn with<T, F>(&self, action: F) -> Result<T>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T> + Send + 'static,
    {
        let connection = self.connection.clone();
        let ownership = self._lock.clone();
        tokio::task::spawn_blocking(move || {
            // An aborted async caller must not release workspace ownership while
            // its already-admitted blocking database operation is still running.
            let _ownership = ownership;
            let mut connection = connection
                .lock()
                .map_err(|_| anyhow::anyhow!("State lock poisoned"))?;
            action(&mut connection)
        })
        .await?
    }

    pub async fn create_session(&self) -> Result<Session> {
        self.with(|db| {
            let session = Session {
                id: format!("ngn-{}", Uuid::new_v4().simple()),
                title: "New session".into(),
                updated_at_ms: now_ms(),
            };
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                params![session.id, session.title, session.updated_at_ms],
            )?;
            Ok(session)
        })
        .await
    }

    pub async fn sessions(&self) -> Result<Vec<Session>> {
        self.with(|db| {
            let mut statement = db.prepare("SELECT id, title, updated_at_ms FROM sessions
                WHERE NOT EXISTS (SELECT 1 FROM subagents t WHERE t.child_session_id = sessions.id)
                ORDER BY EXISTS (SELECT 1 FROM archived_sessions a WHERE a.session_id = sessions.id),
                    updated_at_ms DESC, rowid DESC LIMIT 200")?;
            Ok(statement.query_map([], |row| Ok(Session { id: row.get(0)?, title: row.get(1)?, updated_at_ms: row.get(2)? }))?.collect::<Result<_, _>>()?)
        }).await
    }

    pub async fn latest_native_session(&self) -> Result<Option<String>> {
        self.with(|db| {
            Ok(db.query_row(
                "SELECT id FROM sessions
                 WHERE NOT EXISTS (SELECT 1 FROM subagents t WHERE t.child_session_id = sessions.id)
                   AND NOT EXISTS (SELECT 1 FROM archived_sessions a WHERE a.session_id = sessions.id)
                 ORDER BY updated_at_ms DESC, rowid DESC LIMIT 1",
                [],
                |row| row.get(0),
            ).optional()?)
        }).await
    }

    pub(crate) async fn ensure_not_archive(&self, session: &str) -> Result<()> {
        let session = session.to_owned();
        self.with(move |db| {
            let archived: bool = db.query_row(
                "SELECT EXISTS (SELECT 1 FROM archived_sessions WHERE session_id = ?)",
                [&session],
                |row| row.get(0),
            )?;
            ensure!(
                !archived,
                "Python archives are read-only; start a new session"
            );
            Ok(())
        })
        .await
    }

    pub async fn session(&self, id: &str) -> Result<Session> {
        let id = id.to_owned();
        self.with(move |db| {
            db.query_row(
                "SELECT id, title, updated_at_ms FROM sessions WHERE id = ?",
                [id],
                |row| {
                    Ok(Session {
                        id: row.get(0)?,
                        title: row.get(1)?,
                        updated_at_ms: row.get(2)?,
                    })
                },
            )
            .optional()?
            .context("Session not found in this workspace")
        })
        .await
    }

    pub async fn existing_request(
        &self,
        session: &str,
        request: &str,
        prompt: &str,
    ) -> Result<Option<Run>> {
        self.input_receipt(session, request, prompt)
            .await?
            .map(InputReceipt::into_run)
            .transpose()
    }

    pub(crate) async fn input_receipt(
        &self,
        session: &str,
        request: &str,
        prompt: &str,
    ) -> Result<Option<InputReceipt>> {
        let (session, request, prompt) =
            (session.to_owned(), request.to_owned(), prompt.to_owned());
        self.with(move |db| {
            // SQLite TEXT affinity does not convert BLOBs. Probe both storage
            // classes with the original bytes on each side of the existing
            // (session_id, request_id) index, without casting table columns.
            let mut statement = db.prepare_cached(
                "SELECT id, prompt, status, session_id, request_id FROM runs
                 WHERE session_id IN (?1, ?2) AND request_id IN (?3, ?4)
                 LIMIT 2",
            )?;
            let mut rows = statement.query(params![
                session,
                session.as_bytes(),
                request,
                request.as_bytes()
            ])?;
            let Some(row) = rows.next()? else {
                return Ok(None);
            };
            ensure!(
                row.get_ref(3)?.as_str().is_ok() && row.get_ref(4)?.as_str().is_ok(),
                "FIFO receipt key has matching bytes but an invalid storage type"
            );
            let id = row.get(0).context("Unreadable FIFO receipt run ID")?;
            let saved: String = row.get(1).context("Unreadable FIFO receipt prompt")?;
            let status = row.get_ref(2)?.as_str().ok().and_then(|status| {
                serde_json::from_value(serde_json::Value::String(status.to_owned())).ok()
            });
            ensure!(
                rows.next()?.is_none(),
                "Multiple FIFO receipts match the same session and request bytes"
            );
            ensure!(
                saved == prompt,
                "request_id already belongs to different prompt text"
            );
            Ok(Some(InputReceipt {
                id,
                session_id: session,
                request_id: request,
                status,
            }))
        })
        .await
    }

    pub async fn accept(
        &self,
        session: &str,
        request: &str,
        prompt: &str,
    ) -> Result<(Run, EventRecord)> {
        let (session, request, prompt) =
            (session.to_owned(), request.to_owned(), prompt.to_owned());
        self.with(move |db| {
            let transaction = db.transaction()?;
            let run = Run { id: format!("run-{}", Uuid::new_v4().simple()), session_id: session.clone(), request_id: request, status: RunStatus::Running };
            transaction.execute("INSERT INTO runs VALUES (?, ?, ?, ?, 'running')", params![run.id, session, run.request_id, prompt])?;
            let title: String = prompt.lines().next().unwrap_or("New session").chars().take(80).collect();
            transaction.execute("UPDATE sessions SET title = CASE WHEN title = 'New session' THEN ? ELSE title END, updated_at_ms = ? WHERE id = ?", params![title, now_ms(), session])?;
            let event = Event::RunStarted { prompt };
            let timestamp_ms = now_ms();
            transaction.execute("INSERT INTO events(run_id, session_id, timestamp_ms, payload) VALUES (?, ?, ?, ?)", params![run.id, session, timestamp_ms, serde_json::to_string(&event)?])?;
            let sequence = transaction.last_insert_rowid();
            transaction.commit()?;
            let record = EventRecord { schema_version: VERSION, sequence, run_id: run.id.clone(), session_id: session, timestamp_ms, event };
            Ok((run, record))
        }).await
    }

    pub async fn append(&self, run: &Run, event: Event) -> Result<EventRecord> {
        let run = run.clone();
        self.with(move |db| {
            let transaction = db.transaction()?;
            let timestamp_ms = now_ms();
            transaction.execute(
                "INSERT INTO events(run_id, session_id, timestamp_ms, payload) VALUES (?, ?, ?, ?)",
                params![
                    run.id,
                    run.session_id,
                    timestamp_ms,
                    serde_json::to_string(&event)?
                ],
            )?;
            let sequence = transaction.last_insert_rowid();
            if let Event::RunFinished { status, .. } = &event {
                let status = serde_json::to_value(status)?
                    .as_str()
                    .context("Invalid run status")?
                    .to_owned();
                transaction.execute(
                    "UPDATE runs SET status = ? WHERE id = ?",
                    params![status, run.id],
                )?;
            }
            transaction.execute(
                "UPDATE sessions SET updated_at_ms = ? WHERE id = ?",
                params![timestamp_ms, run.session_id],
            )?;
            transaction.commit()?;
            Ok(EventRecord {
                schema_version: VERSION,
                sequence,
                run_id: run.id,
                session_id: run.session_id,
                timestamp_ms,
                event,
            })
        })
        .await
    }

    pub async fn is_child_session(&self, session: &str) -> Result<bool> {
        let session = session.to_owned();
        self.with(move |db| {
            Ok(db.query_row(
                "SELECT EXISTS(SELECT 1 FROM subagents WHERE child_session_id = ?)",
                [session],
                |row| row.get(0),
            )?)
        })
        .await
    }

    pub(crate) async fn admit_child(
        &self,
        admission: ChildAdmission,
    ) -> Result<(Run, TaskInfo, Vec<EventRecord>)> {
        self.with(move |db| {
            let transaction = db.transaction()?;
            for owner in [&admission.parent, &admission.root] {
                let status: String = transaction.query_row("SELECT status FROM runs WHERE id = ? AND session_id = ?", params![owner.id, owner.session_id], |row| row.get(0))?;
                ensure!(status == "running", "Delegation owner is no longer running");
            }
            ensure!(admission.depth <= admission.limits.max_depth, "Subagent depth limit reached");
            let (executions, running): (u16, u16) = transaction.query_row(
                "SELECT COUNT(*), COALESCE(SUM(r.status = 'running'), 0) FROM subagents t JOIN runs r ON r.id = t.child_run_id WHERE t.root_run_id = ?", [&admission.root.id], |row| Ok((row.get(0)?, row.get(1)?)))?;
            ensure!(executions < admission.limits.max_executions, "Subagent execution budget exhausted for this root run");
            ensure!(running < admission.limits.max_concurrent, "Subagent concurrency budget exhausted for this root run");
            let session_id = format!("ngn-{}", Uuid::new_v4().simple());
            transaction.execute("INSERT INTO sessions VALUES (?, ?, ?)", params![session_id, admission.title, now_ms()])?;
            let run = Run { id: format!("run-{}", Uuid::new_v4().simple()), session_id: session_id.clone(), request_id: Uuid::new_v4().to_string(), status: RunStatus::Running };
            transaction.execute("INSERT INTO runs VALUES (?, ?, ?, ?, 'running')", params![run.id, session_id, run.request_id, admission.prompt])?;
            let task = TaskInfo {
                run_id: run.id.clone(), session_id, parent_run_id: admission.parent.id.clone(), parent_session_id: admission.parent.session_id.clone(), root_run_id: admission.root.id.clone(), root_session_id: admission.root.session_id.clone(), call_id: admission.call_id,
                title: admission.title, depth: admission.depth, read_only: admission.read_only, status: RunStatus::Running,
            };
            transaction.execute("INSERT INTO subagents(child_run_id, child_session_id, parent_run_id, parent_session_id, root_run_id, root_session_id, call_id, title, depth, read_only) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params![task.run_id, task.session_id, task.parent_run_id, task.parent_session_id, task.root_run_id, task.root_session_id, task.call_id, task.title, task.depth, task.read_only])?;
            let mut records = vec![
                insert_event(&transaction, &run, Event::RunStarted { prompt: admission.prompt })?,
                insert_event(&transaction, &admission.parent, Event::TaskStarted { task: task.clone() })?,
            ];
            if task.root_run_id != task.parent_run_id {
                records.push(insert_event(&transaction, &admission.root, Event::TaskUpdated { task: task.clone() })?);
            }
            transaction.commit()?;
            Ok((run, task, records))
        }).await
    }

    pub async fn tasks(&self, session: &str) -> Result<Vec<TaskInfo>> {
        let session = session.to_owned();
        self.with(move |db| {
            // Catalogs are metadata-only; polling a task pane must not repeatedly
            // load every child's potentially large final response.
            let select = TASK_SELECT.replace("t.result, t.error", "'', ''");
            let mut statement = db.prepare(&format!("{select} WHERE t.root_session_id = ? OR t.parent_session_id = ? ORDER BY t.rowid DESC LIMIT 1000"))?;
            Ok(statement.query_map(params![session, session], |row| Ok(task_row(row)?.task))?.collect::<Result<_, _>>()?)
        }).await
    }

    pub async fn task(&self, run_id: &str) -> Result<TaskOutcome> {
        let run_id = run_id.to_owned();
        self.with(move |db| {
            db.query_row(
                &format!("{TASK_SELECT} WHERE t.child_run_id = ?"),
                [run_id],
                task_row,
            )
            .optional()?
            .context("Subagent not found")
        })
        .await
    }

    pub(crate) async fn finish(
        &self,
        run: &Run,
        status: RunStatus,
        text: &str,
        error: &str,
    ) -> Result<Vec<EventRecord>> {
        let (run, text, error) = (run.clone(), bounded(text, 32_000), bounded(error, 4096));
        self.with(move |db| {
            let transaction = db.transaction()?;
            let status_text = serde_json::to_value(status)?
                .as_str()
                .context("Invalid run status")?
                .to_owned();
            crate::steering::settle(&transaction, Some(&run.id))?;
            transaction.execute(
                "UPDATE runs SET status = ? WHERE id = ?",
                params![status_text, run.id],
            )?;
            let mut records = vec![insert_event(
                &transaction,
                &run,
                Event::RunFinished {
                    status,
                    error: error.clone(),
                },
            )?];
            if status != RunStatus::Completed {
                let changed = transaction.execute(
                    "UPDATE session_queues SET paused = 1 WHERE session_id = ? AND paused = 0",
                    [&run.session_id],
                )?;
                if changed != 0 {
                    records.push(insert_record(
                        &transaction,
                        &run.session_id,
                        None,
                        Event::QueueUpdated { paused: true },
                    )?);
                }
            }
            let changed = transaction.execute(
                "UPDATE subagents SET result = ?, error = ? WHERE child_run_id = ?",
                params![
                    if status == RunStatus::Completed {
                        &text
                    } else {
                        ""
                    },
                    error,
                    run.id
                ],
            )?;
            if changed != 0 {
                let outcome = transaction.query_row(
                    &format!("{TASK_SELECT} WHERE t.child_run_id = ?"),
                    [&run.id],
                    task_row,
                )?;
                let parent = task_run(&outcome.task.parent_run_id, &outcome.task.parent_session_id);
                records.push(insert_event(
                    &transaction,
                    &parent,
                    Event::TaskFinished {
                        task: outcome.task.clone(),
                        error: error.clone(),
                    },
                )?);
                if outcome.task.root_run_id != outcome.task.parent_run_id {
                    let root = task_run(&outcome.task.root_run_id, &outcome.task.root_session_id);
                    records.push(insert_event(
                        &transaction,
                        &root,
                        Event::TaskUpdated { task: outcome.task },
                    )?);
                }
            }
            transaction.commit()?;
            Ok(records)
        })
        .await
    }

    /// Bounded durable cursor replay. Subscribe before calling, then de-duplicate.
    pub async fn events(&self, session: &str, after: i64) -> Result<Vec<EventRecord>> {
        let session = session.to_owned();
        self.with(move |db| {
            let mut statement = db.prepare("SELECT sequence, COALESCE(run_id, ''), session_id, timestamp_ms, payload FROM events WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT 1000")?;
            let rows = statement.query_map(params![session, after], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?, row.get::<_, String>(2)?, row.get::<_, u64>(3)?, row.get::<_, String>(4)?)))?;
            rows.map(|row| { let (sequence, run_id, session_id, timestamp_ms, payload) = row?; Ok(EventRecord { schema_version: VERSION, sequence, run_id, session_id, timestamp_ms, event: serde_json::from_str(&payload)? }) }).collect()
        }).await
    }

    /// A small indexed tail for best-effort live task activity after joining
    /// or losing broadcast records. This is not a conversation replay.
    pub async fn task_activity_tail(
        &self,
        targets: Vec<(String, String)>,
    ) -> Result<Vec<Vec<EventRecord>>> {
        ensure!(targets.len() <= 4, "Too many activity tails");
        let store = self.clone();
        tokio::task::spawn_blocking(move || {
            let _ownership = store._lock;
            let _writer = store.connection;
            // An optional TUI hint must not take the primary connection's
            // mutex or queue behind its busy timeout. Read the already-owned
            // WAL database through a separate, read-only connection.
            let db = Connection::open_with_flags(
                store.path.as_path(),
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
            )?;
            db.busy_timeout(std::time::Duration::ZERO)?;
            let mut statement = db.prepare(
                "SELECT sequence, COALESCE(run_id, ''), timestamp_ms,
                        CASE WHEN length(CAST(payload AS BLOB)) <= 8192 THEN payload END
                 FROM events WHERE session_id = ? ORDER BY sequence DESC LIMIT 8",
            )?;
            targets
                .into_iter()
                .map(|(session, run)| {
                    let mut rows = statement.query([&session])?;
                    let mut records = Vec::new();
                    while let Some(row) = rows.next()? {
                        let run_id: String = row.get(1)?;
                        if run_id != run {
                            continue;
                        }
                        let Some(payload) = row.get::<_, Option<String>>(3)? else {
                            // A potentially phase-changing record is missing.
                            return Ok(Vec::new());
                        };
                        records.push(EventRecord {
                            schema_version: VERSION,
                            sequence: row.get(0)?,
                            run_id,
                            session_id: session.clone(),
                            timestamp_ms: row.get(2)?,
                            event: serde_json::from_str(&payload)?,
                        });
                    }
                    records.reverse();
                    Ok(records)
                })
                .collect()
        })
        .await?
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn task_activity_tail_is_small_ordered_and_exact() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let store = Store::open(directory.path()).unwrap();
        let session = store.create_session().await.unwrap();
        let (run, _) = store.accept(&session.id, "request", "go").await.unwrap();
        for n in 0..24 {
            store
                .append(
                    &run,
                    Event::TextDelta {
                        text: n.to_string(),
                    },
                )
                .await
                .unwrap();
        }
        let exact = (session.id.clone(), run.id.clone());
        let (held, holding) = tokio::sync::oneshot::channel();
        let (release, released) = std::sync::mpsc::channel();
        let writer = store.clone();
        let blocker = tokio::spawn(async move {
            writer
                .with(move |_| {
                    let _ = held.send(());
                    let _ = released.recv_timeout(std::time::Duration::from_secs(5));
                    Ok(())
                })
                .await
        });
        holding.await.unwrap();
        let batches = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            store.task_activity_tail(vec![exact.clone(), (session.id, "different-run".into())]),
        )
        .await;
        release.send(()).unwrap();
        blocker.await.unwrap().unwrap();
        let batches = batches.unwrap().unwrap();
        assert_eq!(batches[0].len(), 8);
        assert!(batches[0].windows(2).all(|p| p[0].sequence < p[1].sequence));
        assert!(matches!(&batches[0][7].event, Event::TextDelta { text } if text == "23"));
        assert!(batches[1].is_empty());
        assert!(
            store
                .task_activity_tail(vec![exact.clone(); 5])
                .await
                .is_err()
        );

        store
            .append(
                &run,
                Event::TextDelta {
                    text: "ä".repeat(4096),
                },
            )
            .await
            .unwrap();
        assert!(store.task_activity_tail(vec![exact]).await.unwrap()[0].is_empty());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn task_activity_tail_pins_state_behind_an_ancestor_alias() {
        use std::os::unix::fs::symlink;

        let directory = crate::test_support::private_tempdir().unwrap();
        let original = directory.path().join("original");
        let alternate = directory.path().join("alternate");
        let alias = directory.path().join("alias");
        fs::create_dir(&original).unwrap();
        symlink(&original, &alias).unwrap();
        assert!(Store::open(&alias).is_err(), "state leaf cannot be a link");
        let store = Store::open(&alias.join("state")).unwrap();
        let other = Store::open(&alternate.join("state")).unwrap();
        let session = store.create_session().await.unwrap();
        let (run, _) = store.accept(&session.id, "request", "go").await.unwrap();
        let event = store
            .append(
                &run,
                Event::TextDelta {
                    text: "live".into(),
                },
            )
            .await
            .unwrap();
        let target = (session.id, run.id);
        let batches = store
            .task_activity_tail(vec![target.clone()])
            .await
            .unwrap();
        assert_eq!(batches[0].last().unwrap().sequence, event.sequence);

        fs::remove_file(&alias).unwrap();
        symlink(&alternate, &alias).unwrap();
        assert!(Store::open(&original.join("state")).is_err());
        assert!(
            other
                .task_activity_tail(vec![target.clone()])
                .await
                .unwrap()[0]
                .is_empty()
        );
        let batches = store.task_activity_tail(vec![target]).await.unwrap();
        assert_eq!(batches[0].last().unwrap().sequence, event.sequence);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn task_activity_tail_refuses_database_leaf_symlinks() {
        use std::os::unix::fs::symlink;

        let directory = crate::test_support::private_tempdir().unwrap();
        let state = directory.path().canonicalize().unwrap().join("state");
        let store = Store::open(&state).unwrap();
        let alternate = state.with_file_name("alternate");
        drop(Store::open(&alternate).unwrap());
        let target = alternate.join("host.sqlite3");
        let before = fs::read(&target).unwrap();
        let db = state.join("host.sqlite3");
        let original = state.join("original.sqlite3");
        fs::rename(&db, &original).unwrap();
        symlink(&target, &db).unwrap();
        let result = store.task_activity_tail(Vec::new()).await;
        fs::remove_file(&db).unwrap();
        fs::rename(&original, &db).unwrap();
        let error = result.unwrap_err();
        assert!(
            matches!(
                error.downcast_ref::<rusqlite::Error>(),
                Some(rusqlite::Error::SqliteFailure(code, _))
                    if code.extended_code == rusqlite::ffi::SQLITE_CANTOPEN_SYMLINK
            ),
            "{error:#}"
        );

        drop(store);
        fs::remove_file(&db).unwrap();
        symlink(&target, &db).unwrap();
        let Err(error) = Store::open(&state) else {
            panic!("writer followed the database leaf symlink");
        };
        assert!(error.to_string().contains("not a symlink"));
        assert_eq!(fs::read(&target).unwrap(), before);
    }

    #[tokio::test]
    async fn recovery_and_exclusive_ownership() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let store = Store::open(directory.path()).unwrap();
        assert!(Store::open(directory.path()).is_err());
        let session = store.create_session().await.unwrap();
        let (run, _) = store.accept(&session.id, "request", "hello").await.unwrap();
        assert!(
            store
                .existing_request(&session.id, "request", "different")
                .await
                .is_err()
        );
        drop(store);
        let reopened = Store::open(directory.path()).unwrap();
        let events = reopened.events(&session.id, 0).await.unwrap();
        assert!(matches!(
            events.last().unwrap().event,
            Event::RunFinished {
                status: RunStatus::Interrupted,
                ..
            }
        ));
        assert_eq!(events.last().unwrap().run_id, run.id);
    }

    #[tokio::test]
    async fn fifo_receipt_distinguishes_absence_from_byte_matching_corrupt_identity() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let store = Store::open(directory.path()).unwrap();
        let session = store.create_session().await.unwrap().id;
        let unrelated = store.create_session().await.unwrap().id;
        let empty = store.create_session().await.unwrap().id;
        let request = Uuid::new_v4().to_string();
        let prompt = "exact FIFO draft";
        let (run, _) = store.accept(&session, &request, prompt).await.unwrap();
        let (other, _) = store.accept(&unrelated, &request, prompt).await.unwrap();
        let lookup = || store.input_receipt(&session, &request, prompt);
        assert_eq!(lookup().await.unwrap().unwrap().id, run.id);
        assert!(
            store
                .input_receipt(&empty, &request, prompt)
                .await
                .unwrap()
                .is_none()
        );

        let inject = Connection::open(directory.path().join("host.sqlite3")).unwrap();
        // Simulate a pre-existing bad row, including a damaged foreign key.
        inject.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
        for (sql, diagnostic) in [
            (
                "UPDATE runs SET request_id = CAST(request_id AS BLOB) WHERE id = ?",
                "matching bytes but an invalid storage type",
            ),
            (
                "UPDATE runs SET session_id = CAST(session_id AS BLOB), request_id = CAST(request_id AS BLOB) WHERE id = ?",
                "matching bytes but an invalid storage type",
            ),
            (
                "UPDATE runs SET session_id = CAST(session_id AS BLOB) WHERE id = ?",
                "matching bytes but an invalid storage type",
            ),
            (
                "UPDATE runs SET prompt = CAST(prompt AS BLOB) WHERE id = ?",
                "Unreadable FIFO receipt prompt",
            ),
            (
                "UPDATE runs SET id = CAST(id AS BLOB) WHERE id = ?",
                "Unreadable FIFO receipt run ID",
            ),
            (
                "UPDATE runs SET prompt = 'different text' WHERE id = ?",
                "already belongs to different prompt text",
            ),
        ] {
            assert_eq!(inject.execute(sql, [&run.id]).unwrap(), 1);
            let error = lookup().await.unwrap_err().to_string();
            assert!(error.contains(diagnostic), "{error}");
            assert_eq!(
                store
                    .input_receipt(&unrelated, &request, prompt)
                    .await
                    .unwrap()
                    .unwrap()
                    .id,
                other.id
            );
            assert!(
                store
                    .input_receipt(&empty, &request, prompt)
                    .await
                    .unwrap()
                    .is_none()
            );
            inject
                .execute(
                    "UPDATE runs SET id = ?1, session_id = ?2, request_id = ?3, prompt = ?4
                 WHERE id IN (?1, CAST(?1 AS BLOB))",
                    params![run.id, session, request, prompt],
                )
                .unwrap();
        }

        inject
            .execute(
                "UPDATE runs SET status = CAST(status AS BLOB) WHERE id = ?",
                [&run.id],
            )
            .unwrap();
        let receipt = lookup().await.unwrap().unwrap();
        assert_eq!(receipt.id, run.id);
        assert!(receipt.status.is_none());
        inject.execute(
            "INSERT INTO runs SELECT 'run-duplicate-bytes', session_id, CAST(request_id AS BLOB), prompt, status
             FROM runs WHERE id = ?",
            [&run.id],
        ).unwrap();
        assert!(lookup().await.is_err()); // TEXT and BLOB can coexist in the UNIQUE index.
    }

    #[tokio::test]
    async fn subagent_recovery_preserves_topology_without_replay_or_success_claims() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let store = Store::open(directory.path()).unwrap();
        let session = store.create_session().await.unwrap();
        let (parent, _) = store.accept(&session.id, "parent", "hold").await.unwrap();
        let (child, _, _) = store
            .admit_child(ChildAdmission {
                parent: parent.clone(),
                root: parent.clone(),
                call_id: "call".into(),
                prompt: "work".into(),
                title: "Reviewer".into(),
                depth: 1,
                read_only: true,
                limits: crate::subagents::SubagentLimits::default(),
            })
            .await
            .unwrap();
        drop(store);
        let reopened = Store::open(directory.path()).unwrap();
        let recovered = reopened.task(&child.id).await.unwrap();
        assert_eq!(recovered.task.status, RunStatus::Interrupted);
        assert_eq!(recovered.task.parent_run_id, parent.id);
        assert!(recovered.task.read_only);
        assert!(recovered.result.is_empty());
        assert!(recovered.error.contains("not replayed"));
        assert_eq!(reopened.sessions().await.unwrap().len(), 1);
        assert!(
            reopened
                .events(&session.id, 0)
                .await
                .unwrap()
                .iter()
                .any(|record| matches!(record.event, Event::TaskFinished { .. }))
        );
    }

    #[tokio::test]
    async fn version_one_host_state_migrates_without_touching_the_runtime_database() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let database = Connection::open(directory.path().join("host.sqlite3")).unwrap();
        database.execute_batch("PRAGMA user_version=1;
            CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(session_id, request_id));
            CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            INSERT INTO sessions VALUES ('ngn-legacy', 'Existing title', 1);").unwrap();
        drop(database);
        std::fs::write(
            directory.path().join("nagents.sqlite3"),
            b"opaque-runtime-data",
        )
        .unwrap();
        let store = Store::open(directory.path()).unwrap();
        assert_eq!(store.sessions().await.unwrap()[0].title, "Existing title");
        assert!(store.tasks("ngn-legacy").await.unwrap().is_empty());
        assert_eq!(
            std::fs::read(directory.path().join("nagents.sqlite3")).unwrap(),
            b"opaque-runtime-data"
        );
        let version: u32 = store
            .with(|db| Ok(db.pragma_query_value(None, "user_version", |row| row.get(0))?))
            .await
            .unwrap();
        assert_eq!(version, 5);
    }

    #[tokio::test]
    async fn schema_three_upgrades_preserving_queue_and_cursors_and_newer_state_is_never_downgraded()
     {
        let directory = crate::test_support::private_tempdir().unwrap();
        let path = directory.path().join("host.sqlite3");
        let db = Connection::open(&path).unwrap();
        db.execute_batch(r#"PRAGMA user_version=3;
            CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(session_id, request_id));
            CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE session_queues(session_id TEXT PRIMARY KEY REFERENCES sessions(id), paused INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE input_queue(position INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), interactive INTEGER NOT NULL CHECK(interactive IN (0,1)));
            INSERT INTO sessions VALUES ('s3', 'Preserved', 1);
            INSERT INTO runs VALUES ('r3', 's3', 'request3', 'next run only', 'queued');
            INSERT INTO input_queue VALUES (9, 'r3', 0);
            INSERT INTO session_queues VALUES ('s3', 1);
            INSERT INTO events VALUES (42, NULL, 's3', 1, '{"event":"queue_updated","paused":true}');"#).unwrap();
        drop(db);
        std::fs::write(
            directory.path().join("nagents.sqlite3"),
            b"opaque schema; never open",
        )
        .unwrap();
        let store = Store::open(directory.path()).unwrap();
        assert_eq!(store.events("s3", 0).await.unwrap()[0].sequence, 42);
        assert_eq!(store.queue("s3").await.unwrap().entries[0].run.id, "r3");
        assert!(store.steering_receipts("r3").await.unwrap().is_empty());
        assert_eq!(
            store
                .with(|db| Ok(
                    db.pragma_query_value::<u32, _>(None, "user_version", |row| row.get(0))?
                ))
                .await
                .unwrap(),
            5
        );
        assert_eq!(
            std::fs::read(directory.path().join("nagents.sqlite3")).unwrap(),
            b"opaque schema; never open"
        );
        drop(store);
        let db = Connection::open(&path).unwrap();
        db.pragma_update(None, "user_version", 6).unwrap();
        drop(db);
        assert!(Store::open(directory.path()).is_err());
        let db = Connection::open(&path).unwrap();
        assert_eq!(
            db.pragma_query_value::<u32, _>(None, "user_version", |row| row.get(0))
                .unwrap(),
            6
        );
    }
}
