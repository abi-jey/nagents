use std::{
    fs::{self, File, OpenOptions},
    path::Path,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use ngn_protocol::{Event, EventRecord, RunStatus, VERSION};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

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

#[derive(Clone)]
pub struct Store {
    connection: Arc<Mutex<Connection>>,
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
    pub fn open(directory: &Path) -> Result<Self> {
        fs::create_dir_all(directory)?;
        ensure!(
            !directory.symlink_metadata()?.file_type().is_symlink(),
            "State directory cannot be a symlink"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(directory, fs::Permissions::from_mode(0o700))?;
        }
        let lock = private_file(&directory.join("host.lock"))?;
        lock.try_lock_exclusive()
            .context("Another ngn process owns this workspace's Rust state; stop it first")?;
        let lock = StateLock(lock);
        let path = directory.join("host.sqlite3");
        private_file(&path)?;
        let mut connection = Connection::open(path)?;
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        let version: u32 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
        ensure!(
            version <= 1,
            "Rust host database was created by a newer unsupported schema version"
        );
        connection.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, UNIQUE(session_id, request_id));
            CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), session_id TEXT NOT NULL REFERENCES sessions(id), timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS session_events ON events(session_id, sequence);")?;
        connection.pragma_update(None, "user_version", 1)?;
        // Recovery is observable and never re-executes a tool or prompt.
        let transaction = connection.transaction()?;
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
        transaction.commit()?;
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
            _lock: Arc::new(lock),
        })
    }

    async fn with<T, F>(&self, action: F) -> Result<T>
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
            let mut statement = db.prepare("SELECT id, title, updated_at_ms FROM sessions ORDER BY updated_at_ms DESC, rowid DESC LIMIT 200")?;
            Ok(statement.query_map([], |row| Ok(Session { id: row.get(0)?, title: row.get(1)?, updated_at_ms: row.get(2)? }))?.collect::<Result<_, _>>()?)
        }).await
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
        let (session, request, prompt) =
            (session.to_owned(), request.to_owned(), prompt.to_owned());
        self.with(move |db| {
            let found: Option<(String, String, String)> = db
                .query_row(
                    "SELECT id, prompt, status FROM runs WHERE session_id = ? AND request_id = ?",
                    params![session, request],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()?;
            found
                .map(|(id, saved, status)| {
                    ensure!(
                        saved == prompt,
                        "request_id already belongs to different prompt text"
                    );
                    Ok(Run {
                        id,
                        session_id: session,
                        request_id: request,
                        status: serde_json::from_value(serde_json::Value::String(status))?,
                    })
                })
                .transpose()
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

    /// Bounded durable cursor replay. Subscribe before calling, then de-duplicate.
    pub async fn events(&self, session: &str, after: i64) -> Result<Vec<EventRecord>> {
        let session = session.to_owned();
        self.with(move |db| {
            let mut statement = db.prepare("SELECT sequence, run_id, session_id, timestamp_ms, payload FROM events WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT 1000")?;
            let rows = statement.query_map(params![session, after], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?, row.get::<_, String>(2)?, row.get::<_, u64>(3)?, row.get::<_, String>(4)?)))?;
            rows.map(|row| { let (sequence, run_id, session_id, timestamp_ms, payload) = row?; Ok(EventRecord { schema_version: VERSION, sequence, run_id, session_id, timestamp_ms, event: serde_json::from_str(&payload)? }) }).collect()
        }).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn recovery_and_exclusive_ownership() {
        let directory = tempfile::tempdir().unwrap();
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
}
