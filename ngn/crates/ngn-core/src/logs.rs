//! Local diagnostics. The runtime logger has no interface for arbitrary text,
//! protocol messages, identifiers, tool output or error chains.

use std::{
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, ensure};
use serde::Serialize;
use uuid::Uuid;

pub(crate) const WIRE_LOG_ENV: &str = "NGN_HOST_WIRE_LOG_PATH";
pub(crate) const WIRE_LOG_ID_ENV: &str = "NGN_HOST_WIRE_LOG_ID";
pub(crate) const WIRE_LOG_CAPABILITY: &str = "provider_wire_log";

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RuntimeEvent {
    HostStarted,
    HostStopped,
    RunStarted,
    RunCompleted,
    RunFailed,
    RunCancelled,
    RunnerStarting,
    RunnerReady,
    RunnerFailed,
    ToolStarted,
    ToolCompleted,
    ToolFailed,
    PersistenceFailed,
    DispatchFailed,
    ShutdownFailed,
}

#[derive(Serialize)]
struct Entry {
    timestamp_ms: u64,
    elapsed_ms: u64,
    event: RuntimeEvent,
}

pub(crate) struct Logs {
    runtime_path: Option<PathBuf>,
    wire: Option<(PathBuf, File)>,
    runtime: Mutex<Option<File>>,
    started: Instant,
}

impl Logs {
    pub(crate) fn open(data_dir: &Path, wire_log: bool) -> Result<Self> {
        ensure!(
            !wire_log || cfg!(unix),
            "Provider wire logging requires POSIX file protections"
        );
        // Use the effective data directory, including when it is shared with
        // Python ngn. Only new files may be opened here.
        let (wire, runtime) = match log_directory(data_dir) {
            Ok((directory, dir)) => {
                let wire = if wire_log {
                    Some(new_file(&dir, &directory, "wire")?)
                } else {
                    None
                };
                // Normal diagnostics must not block a session, even when a
                // separately requested wire log was successfully opened.
                (wire, new_file(&dir, &directory, "tui").ok())
            }
            Err(error) if wire_log => return Err(error),
            Err(_) => (None, None),
        };
        let (runtime_path, runtime) = runtime.unzip();
        let log = Self {
            runtime_path,
            wire,
            runtime: Mutex::new(runtime),
            started: Instant::now(),
        };
        log.record(RuntimeEvent::HostStarted);
        Ok(log)
    }

    pub(crate) fn runtime_path(&self) -> Option<&Path> {
        self.runtime.lock().ok()?.as_ref()?;
        self.runtime_path.as_deref()
    }

    pub(crate) fn wire_path(&self) -> Option<&Path> {
        self.wire.as_ref().map(|(path, _)| path.as_path())
    }

    pub(crate) fn wire_identity(&self) -> Result<Option<String>> {
        #[cfg(unix)]
        if let Some((_, file)) = &self.wire {
            use std::os::unix::fs::MetadataExt;
            let info = file.metadata().context("Cannot inspect ngn wire log")?;
            return Ok(Some(format!("{}:{}", info.dev(), info.ino())));
        }
        Ok(None)
    }

    pub(crate) fn record(&self, event: RuntimeEvent) {
        let Ok(mut state) = self.runtime.lock() else {
            return;
        };
        let Some(file) = state.as_mut() else {
            return;
        };
        let entry = Entry {
            timestamp_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64,
            elapsed_ms: self.started.elapsed().as_millis() as u64,
            event,
        };
        if serde_json::to_writer(&mut *file, &entry).is_err() || file.write_all(b"\n").is_err() {
            // Never pass log data or errors through to the TUI or runner.
            *state = None;
        }
    }
}

fn log_directory(data_dir: &Path) -> Result<(PathBuf, cap_std::fs::Dir)> {
    let path = data_dir.join("logs");
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    match builder.create(&path) {
        Ok(()) => (),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error).context("Cannot create ngn log directory"),
    }
    let info = path
        .symlink_metadata()
        .context("Cannot inspect ngn log directory")?;
    ensure!(
        info.is_dir(),
        "ngn log directory must be a real directory, not a symlink"
    );
    let dir = cap_std::fs::Dir::open_ambient_dir(&path, cap_std::ambient_authority())
        .context("Cannot open ngn log directory")?;
    #[cfg(unix)]
    {
        use cap_std::fs::MetadataExt as _;
        use std::os::unix::fs::MetadataExt as _;
        let opened = dir.dir_metadata()?;
        ensure!(
            (info.dev(), info.ino()) == (opened.dev(), opened.ino())
                && opened.uid() == nix::unistd::getuid().as_raw()
                && opened.mode() & 0o022 == 0,
            "ngn log directory must be owner-controlled"
        );
    }
    Ok((path, dir))
}

fn new_file(dir: &cap_std::fs::Dir, directory: &Path, prefix: &str) -> Result<(PathBuf, File)> {
    let name = format!("{prefix}-{}.jsonl", Uuid::new_v4().simple());
    let mut options = cap_std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
    }
    let file = dir
        .open_with(&name, &options)
        .context("Cannot create private ngn log")?;
    Ok((directory.join(name), file.into_std()))
}
