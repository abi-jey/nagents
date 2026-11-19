//! One-way import of display-only Python history via private snapshots.

use std::path::{Path, PathBuf};

use anyhow::Result;

/// A candidate for the one-time startup copy, not an open SQLite connection.
pub struct PythonHistory {
    source: PathBuf,
}

impl PythonHistory {
    pub fn discover(workspace: &Path) -> Result<Option<Self>> {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        return offline::discover(workspace);
        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        {
            let _ = workspace;
            Ok(None)
        }
    }

    /// Reject an explicitly overlapping native destination before the host opens it.
    pub fn ensure_separate(&self, config: &crate::Config) -> Result<()> {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        return offline::ensure_separate(&self.source, config);
        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        {
            let _ = (&self.source, config);
            Ok(())
        }
    }

    pub async fn append(self, host: &crate::Host) -> Result<usize> {
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        return offline::append(self.source, host).await;
        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        {
            let _ = (self, host);
            Ok(0)
        }
    }
}

#[derive(Debug)]
pub struct UpgradeReport {
    pub sessions: usize,
    pub messages: usize,
    pub skipped_messages: usize,
    pub destination: PathBuf,
    pub directory_synced: bool,
}

pub async fn import_python_backup(
    backup: &Path,
    workspace: Option<&Path>,
    data_dir: Option<&Path>,
) -> Result<UpgradeReport> {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    {
        // Resolve only paths. Never discover connections, auth, or a runner for an import.
        let mut config = crate::Config::default();
        if let Some(workspace) = workspace {
            config.workspace = workspace.to_owned();
        }
        if let Some(data_dir) = data_dir {
            config.data_dir = data_dir.to_owned();
        }
        let backup = backup.to_owned();
        tokio::task::spawn_blocking(move || offline::import(&backup, config)).await?
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        let _ = (backup, workspace, data_dir);
        anyhow::bail!("Upgrade requires Linux or macOS with atomic no-replace directory rename")
    }
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
mod offline {
    use std::{
        collections::{HashMap, HashSet},
        env,
        fs::{self, DirBuilder, File, Metadata, OpenOptions},
        io::{self, Read, Seek},
        os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        path::Component,
        time::{Duration, Instant},
    };

    use anyhow::{Context, anyhow, ensure};
    use ngn_protocol::Event;
    use rusqlite::{Connection, OpenFlags, OptionalExtension, Row, Transaction, params};
    use rustix::fs::{CWD, FlockOperation, RenameFlags, fcntl_lock, renameat_with};
    use serde::{Deserialize, Deserializer};
    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{Config, Host, legacy, paths, store::Store};

    const MAX_CONFIG: u64 = 256 * 1024;
    const MAX_FILE: u64 = 128 * 1024 * 1024;
    const MAX_SESSIONS: usize = 200; // Fits the native picker.
    const MAX_ROWS: usize = 50_000;
    const MAX_MESSAGES: usize = 10_000;
    const MAX_SESSION_MESSAGES: usize = 999; // One archive notice plus the browser's 1,000-item transcript.
    const MAX_TEXT: usize = 32_000;
    const MAX_TOTAL_TEXT: usize = 16 * 1024 * 1024;

    const LIVE_FILENAME: &str = "sessions.db";
    const ARCHIVE_LABEL: &str = "[Python archive] ";

    struct Message {
        role: &'static str,
        text: String,
        timestamp: u64,
    }

    struct Archive {
        python_id: String,
        title: String,
        updated: u64,
        messages: Vec<Message>,
    }

    #[derive(Deserialize)]
    struct PythonSettings {
        #[serde(default, deserialize_with = "data_override")]
        data_dir: Option<String>,
    }

    fn data_override<'de, D: Deserializer<'de>>(
        value: D,
    ) -> std::result::Result<Option<String>, D::Error> {
        String::deserialize(value).map(Some)
    }

    fn expand_home(path: PathBuf) -> Result<PathBuf> {
        if let Ok(relative) = path.strip_prefix("~") {
            return Ok(env::home_dir()
                .context("Set HOME for Python's data_dir")?
                .join(relative));
        }
        ensure!(
            !path.as_os_str().as_encoded_bytes().starts_with(b"~"),
            "Python data_dir with ~user is not supported by automatic import"
        );
        Ok(path)
    }

    fn personal_data_override() -> Result<Option<PathBuf>> {
        let base = env::var_os("XDG_CONFIG_HOME")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .or_else(|| env::home_dir().map(|home| home.join(".config")));
        let Some(base) = base else {
            return Ok(None);
        };
        let directory = std::path::absolute(expand_home(base.join("ngn"))?)?;
        let path = match directory.join("config.json").canonicalize() {
            Ok(path) => path,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(error).context("Cannot resolve Python's personal config.json");
            }
        };
        check_ancestry(
            path.parent().context("Missing Python config parent")?,
            &[],
            false,
        )?;
        let file = open_read_only(&path).context("Cannot read Python's personal config.json")?;
        let info = file.metadata()?;
        ensure!(
            trusted_file(&info) && info.len() <= MAX_CONFIG,
            "Python's personal config.json must be user-owned, non-writable by others, and at most 256 KiB"
        );
        // Read only the data_dir field; ignore other configuration, and never
        // visit project/explicit Python config or paths referenced by the file.
        let settings: PythonSettings = serde_json::from_reader(file.take(MAX_CONFIG + 1))
            .map_err(|_| anyhow!("Invalid Python personal config.json or data_dir"))?;
        settings
            .data_dir
            .map(|value| Ok(directory.join(expand_home(PathBuf::from(value))?)))
            .transpose()
    }

    pub(super) fn discover(workspace: &Path) -> Result<Option<PythonHistory>> {
        ensure!(
            workspace.to_str().is_some(),
            "Python history discovery requires a UTF-8 workspace path"
        );
        // Python applies NGN_DATA_DIR first, then the existing personal JSON
        // file. Relative env paths use the invocation directory, not -C.
        let root = if let Some(path) = personal_data_override()? {
            path
        } else if let Some(path) = env::var_os("NGN_DATA_DIR") {
            expand_home(PathBuf::from(path))?
        } else if let Some(path) = legacy::python_data_directory() {
            expand_home(path)?
        } else {
            return Ok(None);
        };
        let root = if root.as_os_str().is_empty() {
            PathBuf::from(".")
        } else {
            root
        };
        let root = match root.canonicalize() {
            Ok(root) => root,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error).context("Cannot resolve Python data_dir"),
        };
        let scope = root.join(legacy::workspace_id(workspace));
        let source = scope.join(LIVE_FILENAME);
        match source.symlink_metadata() {
            Ok(_) => Ok(Some(PythonHistory { source })),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error).context("Cannot inspect scoped Python sessions.db"),
        }
    }

    pub(super) fn ensure_separate(source: &Path, config: &Config) -> Result<()> {
        // Resolve aliases in the existing part of a path even when the final
        // native workspace hasn't been created yet.
        fn resolve(path: &Path) -> io::Result<PathBuf> {
            match path.canonicalize() {
                Ok(path) => Ok(path),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    match (path.parent(), path.file_name()) {
                        (Some(parent), Some(name)) => Ok(resolve(parent)?.join(name)),
                        _ => Err(error),
                    }
                }
                Err(error) => Err(error),
            }
        }
        let scope = source.parent().unwrap();
        let root = scope.parent().unwrap();
        let physical_scope = scope.canonicalize().ok();
        let native_data = resolve(&config.data_dir)?;
        let native_state = resolve(&config.state_dir())?;
        ensure!(
            native_data != root
                && ![Some(scope), physical_scope.as_deref()]
                    .into_iter()
                    .flatten()
                    .any(|scope| native_data.starts_with(scope) || native_state.starts_with(scope)),
            "Choose a separate Rust --data-dir outside Python ngn's data root and workspace database directory"
        );
        Ok(())
    }

    pub(super) async fn append(source: PathBuf, host: &Host) -> Result<usize> {
        let directory = host.config.state_dir();
        host.store
            .with(move |db| {
                const MARKER: &str = "python_history_startup_import";
                let done: bool = db.query_row(
                    "SELECT EXISTS (SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?)",
                    [MARKER],
                    |row| row.get(0),
                )?;
                if done {
                    return Ok(0);
                }
                check_ancestry(source.parent().unwrap(), &[], false)?;
                let stage = tempfile::Builder::new()
                    .prefix(".ngn-history-")
                    .permissions(fs::Permissions::from_mode(0o700))
                    .tempdir_in(directory)?;
                let snapshot = stage.path().join("snapshot.db");
                snapshot_file(&source, &snapshot, true)?;
                let (mut archives, _, _) = read_archive(&snapshot, Duration::from_millis(750))?;
                stage.close()?;
                sort_archives(&mut archives);
                let transaction = db.transaction()?;
                let added = write_archive(&transaction, &archives)?;
                transaction.execute_batch(&format!(
                    "CREATE TABLE {MARKER} (completed_at INTEGER NOT NULL);
                INSERT INTO {MARKER} VALUES (unixepoch());"
                ))?;
                transaction.commit()?;
                Ok(added)
            })
            .await
    }

    fn sort_archives(archives: &mut [Archive]) {
        // Rowid is the secondary sort key in the native picker.
        archives.sort_by(|left, right| {
            left.updated
                .cmp(&right.updated)
                .then(left.python_id.cmp(&right.python_id))
        });
    }

    pub(super) fn import(backup: &Path, mut config: Config) -> Result<UpgradeReport> {
        config.workspace = config
            .workspace
            .canonicalize()
            .context("Workspace does not exist")?;
        ensure!(config.workspace.is_dir(), "Workspace must be a directory");
        config.data_dir = paths::without_system_aliases(&std::path::absolute(config.data_dir)?);
        ensure!(
            !config
                .data_dir
                .components()
                .any(|part| part == Component::ParentDir),
            "Rust data directory cannot contain parent traversal"
        );
        let legacy_paths = legacy_roots(&config.workspace);
        ensure!(
            !legacy_paths
                .iter()
                .any(|root| config.data_dir.starts_with(root)),
            "Choose a separate Rust data directory outside Python ngn state and settings"
        );
        let destination = config.state_dir();
        absent(
            &destination,
            "Rust workspace state already exists; use a fresh --data-dir",
        )?;

        let parent = destination
            .parent()
            .context("Missing Rust workspace state parent")?;
        check_ancestry(parent, &legacy_paths, true)?;
        absent(
            &destination,
            "Rust workspace state already exists; use a fresh --data-dir",
        )?;
        let stage = tempfile::Builder::new()
            .prefix(".ngn-upgrade-")
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir_in(parent)?;
        let snapshot = stage.path().join("snapshot.db");
        snapshot_backup(backup, &snapshot, &legacy_paths)?;
        let (mut archives, messages, skipped_messages) =
            read_archive(&snapshot, Duration::from_secs(30))?;
        fs::remove_file(snapshot)?;
        let staged_state = stage.path().join("state");
        DirBuilder::new().mode(0o700).create(&staged_state)?;
        sort_archives(&mut archives);
        Store::stage_archive(&staged_state, |dest| {
            write_archive(dest, &archives).map(|_| ())
        })?;
        File::open(staged_state.join("host.sqlite3"))?.sync_all()?;
        File::open(&staged_state)?.sync_all()?;
        let parent_handle = File::open(parent)?;
        renameat_with(
            CWD,
            &staged_state,
            &parent_handle,
            destination.file_name().unwrap(),
            RenameFlags::NOREPLACE,
        )
        .context("Cannot publish to fresh Rust workspace state; nothing was overwritten")?;
        // Rename is the publication point. Never claim rollback if a later directory sync fails.
        let directory_synced = parent_handle.sync_all().is_ok();
        Ok(UpgradeReport {
            sessions: archives.len(),
            messages,
            skipped_messages,
            destination,
            directory_synced,
        })
    }

    fn legacy_roots(workspace: &Path) -> Vec<PathBuf> {
        let mut roots = vec![workspace.join(".ngn")];
        for (variable, fallback) in [
            ("XDG_DATA_HOME", ".local/share"),
            ("XDG_CONFIG_HOME", ".config"),
        ] {
            let mut bases: Vec<PathBuf> = env::var_os(variable)
                .into_iter()
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
                .collect();
            if let Some(home) = env::home_dir() {
                bases.push(home.join(fallback));
            }
            for base in bases {
                if let Ok(root) = std::path::absolute(base.join("ngn")) {
                    let root = paths::without_system_aliases(&root);
                    roots.extend(root.canonicalize().ok());
                    roots.push(root);
                }
            }
        }
        roots
    }

    fn absent(path: &Path, message: &str) -> Result<()> {
        match path.symlink_metadata() {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
            Ok(_) => Err(anyhow!("{message}")),
        }
    }

    fn check_ancestry(path: &Path, legacy_paths: &[PathBuf], create: bool) -> Result<()> {
        let mut current = PathBuf::new();
        let user = nix::unistd::geteuid().as_raw();
        let legacy_ids: Vec<_> = legacy_paths
            .iter()
            .filter_map(|path| path.metadata().ok().map(|info| (info.dev(), info.ino())))
            .collect();
        for part in path.components() {
            current.push(part);
            let metadata = match current.symlink_metadata() {
                Ok(metadata) => metadata,
                Err(error) if create && error.kind() == io::ErrorKind::NotFound => {
                    match DirBuilder::new().mode(0o700).create(&current) {
                        Ok(()) => (),
                        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
                        Err(error) => return Err(error).context("Cannot create Rust state parent"),
                    }
                    current.symlink_metadata()?
                }
                Err(error) => return Err(error).context("Cannot inspect directory ancestry"),
            };
            let sticky_system_parent =
                metadata.uid() == 0 && metadata.mode() & 0o1000 != 0 && current != path;
            ensure!(
                metadata.is_dir()
                    && (metadata.uid() == user || metadata.uid() == 0)
                    && (metadata.mode() & 0o022 == 0 || sticky_system_parent),
                "Path requires non-symlink ancestors not writable by other users: {}",
                current.display()
            );
            ensure!(
                !legacy_ids.contains(&(metadata.dev(), metadata.ino())),
                "Choose a separate Rust data directory outside Python ngn state and settings"
            );
        }
        Ok(())
    }

    fn no_sidecars(source: &Path) -> Result<()> {
        for suffix in ["-wal", "-shm", "-journal"] {
            let mut path = source.as_os_str().to_owned();
            path.push(suffix);
            absent(
                Path::new(&path),
                "SQLite source has sidecars; use a standalone offline backup",
            )?;
        }
        Ok(())
    }

    fn fingerprint(meta: &Metadata) -> (u64, u64, u64, u64, i64, i64, i64, i64) {
        (
            meta.dev(),
            meta.ino(),
            meta.nlink(),
            meta.len(),
            meta.mtime(),
            meta.mtime_nsec(),
            meta.ctime(),
            meta.ctime_nsec(),
        )
    }

    fn snapshot_backup(backup: &Path, snapshot: &Path, legacy_paths: &[PathBuf]) -> Result<()> {
        let name = backup
            .file_name()
            .context("Pass the path of an offline Python database backup")?;
        ensure!(
            !name
                .as_encoded_bytes()
                .eq_ignore_ascii_case(LIVE_FILENAME.as_bytes()),
            "Use a distinctly named offline backup, not sessions.db"
        );
        let source = std::path::absolute(backup)?;
        let source = source
            .parent()
            .context("Missing backup parent")?
            .canonicalize()?
            .join(name);
        ensure!(
            !legacy_paths.iter().any(|root| source.starts_with(root)),
            "Place the offline backup outside Python ngn state and settings"
        );
        snapshot_file(&source, snapshot, false)
    }

    fn open_read_only(path: &Path) -> io::Result<File> {
        OpenOptions::new()
            .read(true)
            .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK)
            .open(path)
    }

    fn trusted_file(info: &Metadata) -> bool {
        info.is_file() && info.uid() == nix::unistd::geteuid().as_raw() && info.mode() & 0o022 == 0
    }

    fn snapshot_file(source: &Path, snapshot: &Path, live: bool) -> Result<()> {
        no_sidecars(source)?;
        let mut options = OpenOptions::new();
        let flags = nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK;
        #[cfg(target_os = "linux")]
        let flags = flags | if live { nix::libc::O_NOATIME } else { 0 };
        let mut input = options
            .read(true)
            .custom_flags(flags)
            .open(source)
            .context("Cannot open the SQLite source as a non-symlink file")?;
        let before = input.metadata()?;
        ensure!(
            before.is_file() && before.nlink() == 1 && (100..=MAX_FILE).contains(&before.len()),
            "Backup must be a singly linked regular SQLite file of at most 128 MiB"
        );
        if live {
            ensure!(
                trusted_file(&before),
                "Python sessions.db must be owned by this user and not writable by others"
            );
            // SQLite's Unix rollback VFS uses POSIX byte-range locks on the
            // main file (including a reserved writer byte). A whole-file read
            // lock conflicts with all of them. Do not ask SQLite to open the
            // source: it might need recovery or create/write a WAL index.
            fcntl_lock(&input, FlockOperation::NonBlockingLockShared).context(
                "Python sessions.db is busy or cannot be safely locked; close Python ngn and retry",
            )?;
            no_sidecars(source)?;
        }
        let mut header = [0; 100];
        input.read_exact(&mut header)?;
        ensure!(
            &header[..16] == b"SQLite format 3\0" && header[18..20] == [1, 1],
            "Backup must be SQLite in rollback-journal format (not WAL); use VACUUM INTO"
        );
        let page_size = match u16::from_be_bytes([header[16], header[17]]) {
            1 => 65_536,
            size => u64::from(size),
        };
        let page_count = u32::from_be_bytes(header[28..32].try_into()?);
        ensure!(
            (512..=65_536).contains(&page_size)
                && page_size.is_power_of_two()
                && u64::from(page_count) * page_size == before.len()
                && header[24..28] == header[92..96],
            "Backup has an inconsistent SQLite header or page count"
        );
        input.rewind()?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(snapshot)?;
        let copied = io::copy(&mut input.by_ref().take(MAX_FILE + 1), &mut output)?;
        no_sidecars(source)?;
        ensure!(
            copied == before.len()
                && fingerprint(&before) == fingerprint(&input.metadata()?)
                && fingerprint(&before) == fingerprint(&source.symlink_metadata()?),
            "Backup changed while being copied; keep it offline"
        );
        Ok(())
    }

    // Check the shape before querying data: do not accept views, virtual tables,
    // generated columns, or an ID with different key semantics.
    fn table(
        db: &Connection,
        name: &str,
        expected: &[(&str, &str, u32)],
        optional: bool,
    ) -> Result<bool> {
        let kind: Option<String> = db
            .query_row(
                "SELECT type FROM pragma_table_list WHERE schema = 'main' AND name = ?",
                [name],
                |row| row.get(0),
            )
            .optional()?;
        if optional && kind.is_none() {
            return Ok(false);
        }
        ensure!(
            kind.as_deref() == Some("table"),
            "Unsupported Python backup schema: expected table {name}"
        );
        let mut statement =
            db.prepare("SELECT name,type,pk,hidden FROM pragma_table_xinfo(?,'main')")?;
        let columns = statement
            .query_map([name], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, u32>(2)?,
                    r.get::<_, u32>(3)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        ensure!(
            columns.iter().filter(|(_, _, pk, _)| *pk != 0).count()
                == expected.iter().filter(|(_, _, pk)| *pk != 0).count()
                && expected.iter().all(|(column, ty, pk)| columns.iter().any(
                    |(actual, actual_ty, actual_pk, hidden)| actual == column
                        && actual_ty.eq_ignore_ascii_case(ty)
                        && actual_pk == pk
                        && *hidden == 0
                )),
            "Unsupported Python backup schema: unexpected columns or primary key in {name}"
        );
        Ok(true)
    }

    fn bounded<'a>(row: &'a Row<'_>, column: usize, max: usize) -> Result<&'a str> {
        let value = row
            .get_ref(column)?
            .as_str()
            .context("Expected text in Python backup")?;
        ensure!(
            value.len() <= max,
            "Text field in Python backup exceeds the import limit"
        );
        Ok(value)
    }

    fn optional_text<'a>(row: &'a Row<'_>, column: usize) -> Result<Option<&'a str>> {
        row.get_ref(column)?
            .as_str_or_null()
            .context("Expected text or NULL in Python backup")
    }

    fn timestamps(row: &Row<'_>, raw: usize, epoch: usize) -> Result<u64> {
        if optional_text(row, raw)?.is_none() {
            return Ok(0);
        }
        let seconds: u64 = row.get(epoch).context("Invalid Python timestamp")?;
        seconds
            .checked_mul(1000)
            .context("Python timestamp exceeds the import limit")
    }

    fn limit(count: usize) -> Result<()> {
        ensure!(
            count <= MAX_ROWS,
            "Python backup exceeds the 50,000-row scan limit"
        );
        Ok(())
    }

    fn exclude_ids(db: &Connection, query: &str, ids: &mut HashSet<String>) -> Result<()> {
        let mut statement = db.prepare(query)?;
        let mut rows = statement.query([])?;
        let mut count = 0;
        while let Some(row) = rows.next()? {
            count += 1;
            limit(count)?;
            ids.insert(bounded(row, 0, 256)?.to_owned());
        }
        Ok(())
    }

    fn read_archive(snapshot: &Path, timeout: Duration) -> Result<(Vec<Archive>, usize, usize)> {
        let db = Connection::open_with_flags(snapshot, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        db.execute_batch(
            "PRAGMA trusted_schema=OFF; PRAGMA query_only=ON; PRAGMA temp_store=MEMORY;",
        )?;
        let deadline = Instant::now() + timeout;
        db.progress_handler(10_000, Some(move || Instant::now() >= deadline));
        table(
            &db,
            "harness_sessions",
            &[("id", "TEXT", 1), ("title", "TEXT", 0)],
            false,
        )?;
        table(
            &db,
            "v2_sessions",
            &[("id", "TEXT", 1), ("updated_at", "TIMESTAMP", 0)],
            false,
        )?;
        table(
            &db,
            "v2_messages",
            &[
                ("id", "INTEGER", 1),
                ("session_id", "TEXT", 0),
                ("role", "TEXT", 0),
                ("content", "TEXT", 0),
                ("tool_calls", "TEXT", 0),
                ("tool_call_id", "TEXT", 0),
                ("name", "TEXT", 0),
                ("created_at", "TIMESTAMP", 0),
            ],
            false,
        )?;
        let check: String = db
            .query_row("PRAGMA integrity_check(1)", [], |row| row.get(0))
            .context("Cannot complete backup integrity check within import limits")?;
        ensure!(check == "ok", "Python backup failed SQLite integrity check");

        let mut excluded = HashSet::new();
        if table(&db, "ngn_web_session_trash", &[("id", "TEXT", 1)], true)? {
            exclude_ids(
                &db,
                "SELECT id FROM ngn_web_session_trash NOT INDEXED",
                &mut excluded,
            )?;
        }
        if table(
            &db,
            "nagents_agent_sessions",
            &[("sequence", "INTEGER", 1), ("storage_id", "TEXT", 0)],
            true,
        )? {
            exclude_ids(
                &db,
                "SELECT storage_id FROM nagents_agent_sessions NOT INDEXED",
                &mut excluded,
            )?;
        }
        let mut titles = HashMap::new();
        let mut statement = db.prepare("SELECT id,title FROM harness_sessions NOT INDEXED")?;
        let mut rows = statement.query([])?;
        let mut count = 0;
        while let Some(row) = rows.next()? {
            count += 1;
            limit(count)?;
            let id = bounded(row, 0, 256)?;
            ensure!(
                !id.is_empty() && !id.chars().any(char::is_control),
                "Invalid Python session ID"
            );
            if !excluded.contains(id) {
                let title: String = bounded(row, 1, 4096)?
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")
                    .chars()
                    .filter(|character| !character.is_control())
                    .take(80)
                    .collect();
                titles.insert(id.to_owned(), title);
            }
        }
        let mut archives = Vec::new();
        let mut session_indices = HashMap::new();
        let mut statement =
            db.prepare("SELECT id,updated_at,unixepoch(updated_at) FROM v2_sessions NOT INDEXED")?;
        let mut rows = statement.query([])?;
        let mut count = 0;
        while let Some(row) = rows.next()? {
            count += 1;
            limit(count)?;
            let id = bounded(row, 0, 256)?;
            let Some(title) = titles.remove(id) else {
                continue;
            };
            ensure!(
                archives.len() < MAX_SESSIONS,
                "Python backup exceeds the 200-archive picker limit"
            );
            let updated = timestamps(row, 1, 2)?;
            session_indices.insert(id.to_owned(), archives.len());
            archives.push(Archive {
                python_id: id.to_owned(),
                title,
                updated,
                messages: Vec::new(),
            });
        }
        ensure!(
            !archives.is_empty(),
            "Backup contains no supported Python harness sessions"
        );
        let omit_copies = table(
            &db,
            "ngn_context_message_copies",
            &[("history_id", "INTEGER", 1)],
            true,
        )?;
        let mut omitted = HashSet::new();
        if omit_copies {
            let mut statement =
                db.prepare("SELECT history_id FROM ngn_context_message_copies NOT INDEXED")?;
            let mut rows = statement.query([])?;
            while let Some(row) = rows.next()? {
                omitted.insert(row.get::<_, i64>(0)?);
                limit(omitted.len())?;
            }
        }
        let mut statement = db.prepare(
            "SELECT id,session_id,role,content,tool_calls,tool_call_id,name,
            created_at,unixepoch(created_at) FROM v2_messages NOT INDEXED ORDER BY id",
        )?;
        let mut rows = statement.query([])?;
        let (mut total, mut bytes, mut count) = (0, 0, 0);
        while let Some(row) = rows.next()? {
            count += 1;
            limit(count)?;
            let id: i64 = row.get(0)?;
            ensure!(id > 0, "Invalid Python message ID");
            let Some(&index) = session_indices.get(bounded(row, 1, 256)?) else {
                continue;
            };
            if omitted.contains(&id) {
                continue;
            }
            let role = match bounded(row, 2, 32)? {
                "user" => "YOU",
                "assistant" => "ASSISTANT",
                _ => continue,
            };
            if !matches!(optional_text(row, 4)?, None | Some("" | "[]"))
                || optional_text(row, 5)?.is_some_and(|s| !s.is_empty())
                || optional_text(row, 6)?.is_some_and(|s| !s.is_empty())
            {
                continue;
            }
            let Some(text) = optional_text(row, 3)? else {
                continue;
            };
            // Python stores rich content as a JSON array. Conservatively omit
            // all array-looking payloads rather than expose embedded assets.
            if text.trim().is_empty() || text.trim_start().starts_with('[') {
                continue;
            }
            ensure!(
                text.len() <= MAX_TEXT && bytes + text.len() <= MAX_TOTAL_TEXT,
                "Python transcript exceeds 32,000 bytes per message or 16 MiB total"
            );
            let archive = &mut archives[index];
            ensure!(
                total < MAX_MESSAGES && archive.messages.len() < MAX_SESSION_MESSAGES,
                "Python transcript exceeds 10,000 text messages total or 999 per session"
            );
            archive.messages.push(Message {
                role,
                text: text.to_owned(),
                timestamp: timestamps(row, 7, 8)?,
            });
            total += 1;
            bytes += text.len();
        }
        Ok((archives, total, count - total))
    }

    fn write_archive(dest: &Transaction<'_>, archives: &[Archive]) -> Result<usize> {
        let mut event = dest.prepare(
            "INSERT INTO events(run_id, session_id, timestamp_ms, payload)
            VALUES (NULL, ?, ?, ?)",
        )?;
        let mut existing = dest.prepare("SELECT 1 FROM archived_sessions WHERE python_id = ?")?;
        let mut added = 0;
        for archive in archives {
            if existing.exists([&archive.python_id])? {
                continue;
            }
            let digest = Sha256::digest(archive.python_id.as_bytes());
            let id = format!("python-archive-{digest:x}");
            let title = if archive.title.is_empty() {
                &archive.python_id
            } else {
                &archive.title
            };
            dest.execute(
                "INSERT INTO sessions(id,title,updated_at_ms) VALUES (?,?,?)",
                params![id, format!("{ARCHIVE_LABEL}{title}"), archive.updated],
            )?;
            dest.execute(
                "INSERT INTO archived_sessions(session_id,python_id) VALUES (?,?)",
                params![id, archive.python_id],
            )?;
            let label = Event::Notice {
                text: format!(
                    "Read-only Python archive · original session: {}\n\
                Plain text only. Runs, tools, rich content, and model context were not imported. Start a new session to send prompts.",
                    archive.python_id
                ),
            };
            event.execute(params![id, archive.updated, serde_json::to_string(&label)?])?;
            for message in &archive.messages {
                let notice = Event::Notice {
                    text: format!("{}\n{}", message.role, message.text),
                };
                event.execute(params![
                    id,
                    message.timestamp,
                    serde_json::to_string(&notice)?
                ])?;
            }
            added += 1;
        }
        Ok(added)
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use crate::queues::QueueAction;

        fn private_fixture_root() -> tempfile::TempDir {
            // The harness may put TMPDIR under a group-writable directory.
            tempfile::Builder::new()
                .permissions(std::os::unix::fs::PermissionsExt::from_mode(0o700))
                .tempdir_in("/tmp")
                .unwrap()
        }

        fn fixture(path: &Path) -> Connection {
            let db = Connection::open(path).unwrap();
            db.execute_batch(
                "CREATE TABLE harness_sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL);
                 CREATE TABLE v2_sessions(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                     updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                 CREATE TABLE v2_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
                     session_id TEXT NOT NULL REFERENCES v2_sessions(id), role TEXT NOT NULL,
                     content TEXT, tool_calls TEXT, tool_call_id TEXT, name TEXT,
                     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                 CREATE TABLE ngn_web_session_trash(id TEXT PRIMARY KEY);
                 CREATE TABLE nagents_agent_sessions(sequence INTEGER PRIMARY KEY, storage_id TEXT);
                 CREATE TABLE ngn_context_message_copies(history_id INTEGER PRIMARY KEY);
                 CREATE TABLE unrelated_inflight(prompt TEXT);
                 INSERT INTO unrelated_inflight VALUES ('never replay');
                 INSERT INTO harness_sessions VALUES
                     ('ngn-aa', 'A fixture session'), ('ngn-bb', 'A fixture session'),
                     ('trash', 'Trash'), ('child', 'Child'), ('empty', '');
                 INSERT INTO v2_sessions(id,user_id,updated_at) VALUES
                     ('ngn-aa','u','2025-08-01 11:00:00'), ('ngn-bb','u','2025-08-03 12:00:00'),
                     ('trash','u','2025-08-02 12:00:00'), ('child','u','2025-08-02 12:00:00'),
                     ('empty','u','2025-08-02 12:00:00'), ('orphan','u','2025-08-02 12:00:00');
                 INSERT INTO ngn_web_session_trash VALUES ('trash');
                 INSERT INTO nagents_agent_sessions VALUES (1, 'child');
                 INSERT INTO v2_messages(id,session_id,role,content,tool_calls,tool_call_id,name) VALUES
                     (10,'ngn-aa','user','earliest text',NULL,NULL,NULL),
                     (30,'ngn-aa','assistant','latest text',NULL,NULL,NULL),
                     (20,'ngn-aa','assistant','call payload','[{\"name\":\"shell\"}]',NULL,NULL),
                     (40,'ngn-aa','tool','tool output',NULL,'call',NULL),
                     (50,'ngn-aa','user','[{\"type\":\"image\"}]',NULL,NULL,NULL),
                     (60,'ngn-aa','user','synthetic shell notice',NULL,NULL,'ngn_shell_notice'),
                     (70,'ngn-aa','user','a context copy',NULL,NULL,NULL),
                     (80,'ngn-aa','system','system text',NULL,NULL,NULL),
                     (90,'trash','user','not visible',NULL,NULL,NULL),
                     (100,'child','user','not visible',NULL,NULL,NULL),
                     (110,'orphan','user','not visible',NULL,NULL,NULL);
                 INSERT INTO ngn_context_message_copies VALUES (70);"
            ).unwrap();
            db
        }

        #[tokio::test]
        async fn imports_only_display_text_and_never_admits_work() {
            let root = private_fixture_root();
            let source = root.path().join("fixture.db");
            let backup = root.path().join("fixture.backup.db");
            let db = fixture(&source);
            db.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
            db.execute("VACUUM INTO ?", [backup.to_str().unwrap()])
                .unwrap();
            drop(db);
            let data = root.path().join("rust");
            let before = fs::read(&backup).unwrap();
            let report = import_python_backup(&backup, Some(root.path()), Some(&data))
                .await
                .unwrap();
            assert_eq!(
                (report.sessions, report.messages, report.skipped_messages),
                (3, 2, 9)
            );
            assert_eq!(fs::read(&backup).unwrap(), before);
            assert!(!report.destination.join("nagents.sqlite3").exists());
            assert!(!report.destination.join("host.sqlite3-wal").exists());

            let store = Store::open(&report.destination).unwrap();
            let sessions = store.sessions().await.unwrap();
            assert_eq!(sessions[0].title, "[Python archive] A fixture session");
            assert_eq!(sessions[1].title, "[Python archive] empty");
            assert_eq!(sessions[2].title, sessions[0].title);
            assert_ne!(sessions[2].id, sessions[0].id);
            let id = &sessions[2].id;
            let events = store.events(id, 0).await.unwrap();
            assert_eq!(events.len(), 3);
            assert!(events.iter().all(|item| item.run_id.is_empty()));
            assert!(
                matches!(&events[0].event, Event::Notice { text } if text.contains("original session: ngn-aa"))
            );
            assert!(
                matches!(&events[1].event, Event::Notice { text } if text == "YOU\nearliest text")
            );
            assert!(
                matches!(&events[2].event, Event::Notice { text } if text == "ASSISTANT\nlatest text")
            );
            let error = store.accept(id, "test", "must not run").await.unwrap_err();
            assert!(error.to_string().contains("Python archives are read-only"));
            assert!(
                store
                    .enqueue(id, "not queued", "test", false)
                    .await
                    .is_err()
            );
            assert!(store.queue_control(id, QueueAction::Resume).await.is_err());
            assert!(store.queue(id).await.unwrap().entries.is_empty());
            assert_eq!(store.events(id, 0).await.unwrap().len(), 3);
            let native = store.create_session().await.unwrap();
            assert!(store.accept(&native.id, "test", "native").await.is_ok());

            let again = import_python_backup(&backup, Some(root.path()), Some(&data))
                .await
                .unwrap_err();
            assert!(again.to_string().contains("already exists"));
            assert_eq!(store.sessions().await.unwrap().len(), 4);
        }

        #[tokio::test]
        async fn rejects_live_name_sidecars_wal_symlinks_and_nonfresh_destination() {
            let root = private_fixture_root();
            let data = root.path().join("rust");
            let live = root.path().join(LIVE_FILENAME);
            let backup = root.path().join("fixture.backup.db");
            let alias = root.path().join("backup-link.db");
            let db = fixture(&live);
            drop(db);
            let run = |path| import_python_backup(path, Some(root.path()), Some(&data));
            assert!(
                run(&live)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("not sessions.db")
            );
            fs::copy(&live, &backup).unwrap();
            std::os::unix::fs::symlink(&backup, &alias).unwrap();
            assert!(
                run(&alias)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("non-symlink")
            );
            let sidecar = root.path().join("fixture.backup.db-wal");
            fs::write(&sidecar, b"not touched").unwrap();
            assert!(
                run(&backup)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("sidecars")
            );
            assert_eq!(fs::read(&sidecar).unwrap(), b"not touched");
            fs::remove_file(sidecar).unwrap();

            let db = Connection::open(&backup).unwrap();
            db.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
            drop(db);
            assert!(
                run(&backup)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("not WAL")
            );

            let destination = Config {
                workspace: root.path().canonicalize().unwrap(),
                data_dir: data.clone(),
                ..Config::default()
            }
            .state_dir();
            fs::create_dir(&destination).unwrap();
            assert!(
                run(&backup)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("already exists")
            );
            assert_eq!(fs::read_dir(&destination).unwrap().count(), 0);
        }

        #[tokio::test]
        async fn rejects_schema_and_read_limit_without_publishing() {
            let root = private_fixture_root();
            let data = root.path().join("rust");
            let backup = root.path().join("fixture.backup.db");
            let run = || import_python_backup(&backup, Some(root.path()), Some(&data));
            let db = fixture(&backup);
            db.execute(
                "UPDATE v2_messages SET content = ? WHERE id = 30",
                ["x".repeat(MAX_TEXT + 1)],
            )
            .unwrap();
            drop(db);
            assert!(
                run()
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("32,000 bytes")
            );
            assert_eq!(fs::read_dir(data.join("workspaces")).unwrap().count(), 0);

            let db = Connection::open(&backup).unwrap();
            db.execute_batch(
                "ALTER TABLE harness_sessions RENAME TO titles;
                CREATE VIEW harness_sessions AS SELECT id,title FROM titles;",
            )
            .unwrap();
            drop(db);
            assert!(
                run()
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("expected table harness_sessions")
            );
            assert_eq!(fs::read_dir(data.join("workspaces")).unwrap().count(), 0);
        }

        #[tokio::test]
        async fn refuses_python_and_symlink_destination_ancestors() {
            let root = private_fixture_root();
            let workspace = root.path().canonicalize().unwrap();
            let backup = workspace.join("fixture.backup.db");
            drop(fixture(&backup));
            let settings = workspace.join(".ngn");
            fs::create_dir(&settings).unwrap();
            let error = import_python_backup(&backup, Some(&workspace), Some(&settings))
                .await
                .unwrap_err();
            assert!(error.to_string().contains("separate Rust data directory"));
            let alias = workspace.join("alias");
            std::os::unix::fs::symlink(&settings, &alias).unwrap();
            let error = import_python_backup(&backup, Some(&workspace), Some(&alias))
                .await
                .unwrap_err();
            assert!(error.to_string().contains("non-symlink ancestors"));
            assert_eq!(fs::read_dir(settings).unwrap().count(), 0);
        }

        #[tokio::test]
        async fn concurrent_import_can_publish_only_once() {
            let root = private_fixture_root();
            let workspace = root.path().canonicalize().unwrap();
            let backup = workspace.join("fixture.backup.db");
            drop(fixture(&backup));
            let data = workspace.join("rust");
            let run = || import_python_backup(&backup, Some(&workspace), Some(&data));
            let (first, second) = tokio::join!(run(), run());
            let (success, failure) = match (first, second) {
                (Ok(success), Err(failure)) | (Err(failure), Ok(success)) => (success, failure),
                other => panic!("Expected one successful import, got {other:?}"),
            };
            let reason = failure.to_string();
            assert!(
                reason.contains("already exists") || reason.contains("nothing was overwritten")
            );
            assert_eq!(fs::read_dir(data.join("workspaces")).unwrap().count(), 1);
            let store = Store::open(&success.destination).unwrap();
            assert_eq!(store.sessions().await.unwrap().len(), 3);
        }
    }
}
