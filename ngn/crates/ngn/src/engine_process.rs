//! Discover or start the Python engine. Only an owned child is stopped.

use std::path::{Path, PathBuf};

use anyhow::Result;
use ngn_api::Client;
use tokio::process::Command;

use crate::bundle::BundledRuntime;

/// The engine's Python: `NGN_ENGINE_PYTHON`, the verified bundled runtime next
/// to this binary, the active virtualenv, or `python3`, in that order.
pub(crate) fn python_command() -> Result<Command> {
    let override_python = std::env::var_os("NGN_ENGINE_PYTHON").filter(|value| !value.is_empty());
    let bundled = if override_python.is_none() {
        BundledRuntime::detect(&std::env::current_exe()?)?
    } else {
        None
    };
    let python = override_python
        .map(PathBuf::from)
        .or_else(|| bundled.as_ref().map(BundledRuntime::python))
        .or_else(|| {
            std::env::var_os("VIRTUAL_ENV").map(|value| PathBuf::from(value).join("bin/python"))
        })
        .unwrap_or_else(|| PathBuf::from("python3"));
    let mut command = Command::new(python);
    if let Some(runtime) = bundled {
        runtime.environment(&mut command);
    }
    Ok(command)
}

pub(crate) struct EngineProcess {
    pub(crate) client: Client,
    #[cfg(unix)]
    owner: Option<local::Owner>,
    #[cfg(unix)]
    watch: Option<local::Watch>,
}

impl EngineProcess {
    pub(crate) async fn local(options: Launch<'_>) -> Result<Self> {
        #[cfg(unix)]
        {
            local::start(options).await
        }
        #[cfg(not(unix))]
        {
            let _ = options;
            anyhow::bail!("Starting the Python engine currently needs a POSIX host");
        }
    }

    pub(crate) async fn shutdown(self) -> Result<()> {
        #[cfg(unix)]
        {
            drop(self.watch);
            if let Some(owner) = self.owner {
                owner.stop().await?;
            }
        }
        Ok(())
    }
}

#[derive(Default)]
pub(crate) struct Launch<'a> {
    pub(crate) workspace: Option<&'a Path>,
    pub(crate) data_dir: Option<&'a Path>,
    pub(crate) config: Option<&'a Path>,
    pub(crate) provider_id: Option<&'a str>,
    pub(crate) model: Option<&'a str>,
    pub(crate) wire_log: Option<bool>,
    pub(crate) read_only: bool,
    pub(crate) trust_project: bool,
    pub(crate) demo: bool,
    pub(crate) standalone: bool,
}

impl Launch<'_> {
    pub(crate) fn has_launch_overrides(&self) -> bool {
        self.config.is_some()
            || self.provider_id.is_some()
            || self.model.is_some()
            || self.wire_log.is_some()
            || self.read_only
            || self.trust_project
            || self.demo
    }
}

#[cfg(unix)]
mod local {
    use std::{
        fs::File,
        io::Read,
        os::unix::{
            ffi::{OsStrExt, OsStringExt},
            fs::MetadataExt,
        },
        path::{Component, Path, PathBuf},
        process::Stdio,
        time::Duration,
    };

    use anyhow::{Context, Result, bail, ensure};
    use ngn_api::{Client, Error, PROTOCOL_VERSION};
    use rustix::fs::{Mode, OFlags, open, openat};
    use serde::Deserialize;
    use sha2::{Digest, Sha256};
    use tokio::{
        io::{AsyncBufReadExt, AsyncReadExt, BufReader},
        process::{Child, Command},
        time::{Instant, sleep, timeout},
    };

    use super::{EngineProcess, Launch};

    const MAX_REGISTRATION: usize = 4096;
    const STARTUP: Duration = Duration::from_secs(25);
    const LOG_ROTATE_BYTES: u64 = 8 * 1024 * 1024;

    /// The engine's own log. Its stderr must never reach the terminal: the TUI
    /// draws there, and every request would paint server logs over the screen.
    /// A file (not a pipe) lets a shared engine outlive the client that started it.
    struct EngineLog {
        path: PathBuf,
    }

    impl EngineLog {
        fn open(state: &Path) -> Result<(Self, File)> {
            let directory = state.join("logs");
            let dir =
                checked_directory(&directory, true)?.context("Missing engine log directory")?;
            let path = directory.join("engine.log");
            let open_file = || -> Result<File> {
                let file = File::from(openat(
                    &dir,
                    "engine.log",
                    OFlags::CREATE
                        | OFlags::APPEND
                        | OFlags::WRONLY
                        | OFlags::NOFOLLOW
                        | OFlags::NONBLOCK
                        | OFlags::CLOEXEC,
                    Mode::from_bits_truncate(0o600),
                )?);
                let meta = file.metadata()?;
                ensure!(
                    meta.is_file()
                        && meta.uid() == rustix::process::getuid().as_raw()
                        && meta.mode() & 0o7777 == 0o600
                        && meta.nlink() == 1,
                    "Expected a private engine log file"
                );
                Ok(file)
            };
            let mut file = open_file()?;
            if file.metadata()?.len() > LOG_ROTATE_BYTES {
                rustix::fs::renameat(&dir, "engine.log", &dir, "engine.log.1")?;
                file = open_file()?;
            }
            Ok((Self { path }, file))
        }
    }

    #[derive(Deserialize, PartialEq, Eq)]
    struct Registration {
        url: String,
        token: String,
        pid: u32,
        protocol: u64,
    }

    impl Registration {
        fn decode(bytes: &[u8]) -> Result<Self> {
            ensure!(
                !bytes.is_empty() && bytes.len() <= MAX_REGISTRATION,
                "Invalid engine registration size"
            );
            // Never include credential-bearing registration bytes in errors.
            let value: Self = serde_json::from_slice(bytes)
                .map_err(|_| anyhow::anyhow!("Invalid engine registration"))?;
            let port = value
                .url
                .strip_prefix("http://127.0.0.1:")
                .and_then(|text| text.parse::<u16>().ok());
            ensure!(
                port.is_some_and(|port| port > 0 && value.url == format!("http://127.0.0.1:{port}"))
                    && value.pid > 0
                    && value.token.len() >= 32
                    && value.token.len() <= 512
                    && value
                        .token
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte)),
                "Invalid local engine registration"
            );
            if value.protocol != PROTOCOL_VERSION {
                return Err(Error::Incompatible {
                    client: PROTOCOL_VERSION,
                    engine: value.protocol,
                }
                .into());
            }
            Ok(value)
        }

        async fn connect(&self, workspace: &Path) -> Result<Client> {
            let client = Client::connect(&self.url, &self.token).await?;
            let actual = client.config().await?.workspace;
            ensure!(
                workspace.as_os_str() == actual.as_str(),
                "Engine registration belongs to a different workspace"
            );
            Ok(client)
        }
    }

    fn private_directory(directory: &Path) -> Result<Option<File>> {
        checked_directory(directory, false)
    }

    fn checked_directory(directory: &Path, create: bool) -> Result<Option<File>> {
        ensure!(
            directory.is_absolute(),
            "Engine state directory must be absolute"
        );
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let uid = rustix::process::getuid().as_raw();
        let mut dir = File::from(open("/", flags, Mode::empty())?);
        for component in directory.components() {
            let name = match component {
                Component::RootDir => continue,
                Component::Normal(name) => name,
                _ => bail!("Engine state directory must be canonical"),
            };
            dir = match openat(&dir, name, flags, Mode::empty()) {
                Ok(fd) => File::from(fd),
                Err(rustix::io::Errno::NOENT) if create => {
                    match rustix::fs::mkdirat(&dir, name, Mode::from_bits_truncate(0o700)) {
                        Ok(()) | Err(rustix::io::Errno::EXIST) => (),
                        Err(err) => return Err(err.into()),
                    }
                    File::from(openat(&dir, name, flags, Mode::empty())?)
                }
                Err(rustix::io::Errno::NOENT) => return Ok(None),
                Err(error) => {
                    return Err(error)
                        .context("Cannot safely open the private engine state directory");
                }
            };
            let info = dir.metadata()?;
            let (owner, mode) = (info.uid(), info.mode());
            let writable_allowed = if mode & 0o002 != 0 {
                owner == 0 && mode & 0o1000 != 0
            } else {
                mode & 0o020 == 0 || owner == uid
            };
            ensure!(
                (owner == 0 || owner == uid) && writable_allowed,
                "Engine state has an unsafe ancestor directory"
            );
        }
        let info = dir.metadata()?;
        ensure!(
            info.uid() == uid && info.mode() & 0o7777 == 0o700,
            "Engine state directory must be private and user-owned"
        );
        Ok(Some(dir))
    }

    fn registration(directory: &Path) -> Result<Option<Registration>> {
        let Some(dir) = private_directory(directory)? else {
            return Ok(None);
        };
        let file = match openat(
            &dir,
            "engine.json",
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC,
            Mode::empty(),
        ) {
            Ok(fd) => File::from(fd),
            Err(rustix::io::Errno::NOENT) => return Ok(None),
            Err(error) => return Err(error).context("Cannot safely open engine registration"),
        };
        let metadata = file.metadata()?;
        ensure!(
            metadata.is_file()
                && metadata.uid() == rustix::process::getuid().as_raw()
                && metadata.mode() & 0o7777 == 0o600
                && metadata.nlink() == 1,
            "Engine registration must be a private, user-owned file"
        );
        let mut bytes = Vec::new();
        file.take(MAX_REGISTRATION as u64 + 1)
            .read_to_end(&mut bytes)?;
        Registration::decode(&bytes).map(Some)
    }

    pub(super) struct Owner {
        child: Child,
        standalone: bool,
    }

    impl Owner {
        pub(super) async fn stop(mut self) -> Result<()> {
            if self.standalone {
                drop(self.child.stdin.take()); // EOF releases the private lease.
            } else if self.child.try_wait()?.is_none() {
                let pid = self.child.id().context("Missing owned engine process ID")?;
                match nix::sys::signal::kill(
                    nix::unistd::Pid::from_raw(i32::try_from(pid)?),
                    nix::sys::signal::Signal::SIGTERM,
                ) {
                    Ok(()) | Err(nix::errno::Errno::ESRCH) => (),
                    Err(error) => return Err(error).context("Could not signal the owned engine"),
                }
            }
            match timeout(Duration::from_secs(8), self.child.wait()).await {
                Ok(status) => {
                    status.context("Could not join the owned engine")?;
                }
                Err(_) => self
                    .child
                    .kill()
                    .await
                    .context("Could not stop the owned engine")?,
            }
            Ok(())
        }
    }

    pub(super) struct Watch(tokio::task::JoinHandle<()>);

    impl Drop for Watch {
        fn drop(&mut self) {
            self.0.abort();
        }
    }

    fn watch(
        directory: PathBuf,
        workspace: PathBuf,
        client: Client,
        mut current: Registration,
    ) -> Watch {
        Watch(tokio::spawn(async move {
            loop {
                sleep(Duration::from_millis(500)).await;
                let Ok(Some(candidate)) = registration(&directory) else {
                    continue;
                };
                if candidate == current {
                    continue;
                }
                let Ok(Ok(replacement)) =
                    timeout(Duration::from_secs(3), candidate.connect(&workspace)).await
                else {
                    continue;
                };
                client.use_verified_endpoint(&replacement);
                current = candidate;
            }
        }))
    }

    fn command(options: &Launch<'_>, workspace: &Path) -> Result<Command> {
        let mut command = super::python_command()?;
        command
            .args([
                "-I",
                "-B",
                "-m",
                "nagents.engine.cli",
                "engine",
                "--workspace",
            ])
            .arg(workspace)
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .env_remove("NGN_ENGINE_TOKEN")
            .kill_on_drop(true);
        if let Some(config) = options.config {
            command.arg("--config").arg(config);
        }
        if let Some(provider) = options.provider_id {
            command.arg("--provider-id").arg(provider);
        }
        if let Some(model) = options.model {
            command.arg("--model").arg(model);
        }
        if let Some(enabled) = options.wire_log {
            command.arg(if enabled {
                "--wire-log"
            } else {
                "--no-wire-log"
            });
        }
        if options.read_only {
            command.arg("--read-only");
        }
        if options.trust_project {
            command.arg("--trust-project");
        }
        if options.demo {
            command.arg("--demo");
        }
        Ok(command)
    }

    fn workspace_directory(base: &Path, workspace: &Path) -> Result<PathBuf> {
        let hash = Sha256::digest(workspace.as_os_str().as_bytes());
        Ok(std::path::absolute(base)?
            .join("engine")
            .join("workspaces")
            .join(format!("{hash:x}")))
    }

    async fn state_directory(options: &Launch<'_>, workspace: &Path) -> Result<PathBuf> {
        let mut probe = command(options, workspace)?;
        if let Some(base) = options.data_dir {
            probe
                .arg("--state-dir")
                .arg(workspace_directory(base, workspace)?);
        }
        let mut child = probe
            .arg("--print-state-dir")
            .stdout(Stdio::piped())
            .spawn()
            .context("Could not run Python. Install nagents[engine] or set NGN_ENGINE_PYTHON.")?;
        let mut output = Vec::new();
        timeout(STARTUP, async {
            child.stdout.take().context("Missing engine state discovery pipe")?
                .take(MAX_REGISTRATION as u64 + 1).read_to_end(&mut output).await?;
            ensure!(output.len() <= MAX_REGISTRATION, "Engine state directory response is too large");
            ensure!(child.wait().await?.success(), "Could not resolve the Python engine state directory. Check workspace, config and NGN_ENGINE_PYTHON.");
            Ok::<_, anyhow::Error>(())
        }).await.context("Python engine state discovery timed out")??;
        let value = output
            .strip_suffix(b"\n")
            .context("Invalid engine state path response")?;
        ensure!(
            !value.iter().any(|byte| b"\0\r\n".contains(byte)),
            "Invalid engine state path response"
        );
        let path = PathBuf::from(std::ffi::OsString::from_vec(value.to_vec()));
        ensure!(
            path.is_absolute(),
            "Engine did not return an absolute state directory"
        );
        Ok(path)
    }

    async fn standalone(
        mut command: Command,
        workspace: &Path,
        log: EngineLog,
    ) -> Result<EngineProcess> {
        let mut child = command
            .arg("--stdio")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let pipe = child
            .stdout
            .take()
            .context("Missing private engine readiness pipe")?;
        let mut reader = BufReader::new(pipe.take(MAX_REGISTRATION as u64 + 1));
        let mut bytes = Vec::new();
        timeout(STARTUP, reader.read_until(b'\n', &mut bytes))
            .await
            .context("Private engine startup timed out")??;
        ensure!(
            bytes.last() == Some(&b'\n'),
            "Private engine did not report readiness. See {}",
            log.path.display()
        );
        let record = Registration::decode(&bytes)?;
        ensure!(
            child.id() == Some(record.pid),
            "Private engine readiness names another process"
        );
        let client = record.connect(workspace).await?;
        Ok(EngineProcess {
            client,
            owner: Some(Owner {
                child,
                standalone: true,
            }),
            watch: None,
        })
    }

    fn allow_existing(options: &Launch<'_>) -> Result<()> {
        ensure!(
            !options.has_launch_overrides(),
            "A Python engine already owns this workspace state. Remove launch overrides, stop that engine, or select separate --data-dir state."
        );
        Ok(())
    }

    pub(super) async fn start(options: Launch<'_>) -> Result<EngineProcess> {
        let workspace = options
            .workspace
            .unwrap_or_else(|| Path::new("."))
            .canonicalize()
            .context("Workspace does not exist")?;
        ensure!(workspace.is_dir(), "Workspace must be a directory");
        ensure!(
            workspace.to_str().is_some(),
            "The engine API requires a UTF-8 workspace path"
        );
        let directory = state_directory(&options, &workspace).await?;
        let mut command = command(&options, &workspace)?;
        command.arg("--state-dir").arg(&directory);
        if options.standalone {
            let (log, file) = EngineLog::open(&directory)?;
            command.stderr(Stdio::from(file));
            return standalone(command, &workspace, log).await;
        }

        let stale = registration(&directory)?;
        if let Some(record) = &stale {
            match record.connect(&workspace).await {
                Ok(client) => {
                    allow_existing(&options)?;
                    let watch = watch(
                        directory,
                        workspace,
                        client.clone(),
                        stale.expect("still present"),
                    );
                    return Ok(EngineProcess {
                        client,
                        owner: None,
                        watch: Some(watch),
                    });
                }
                // An unclean shutdown can leave a valid but unreachable file.
                // Only the Python state lock owner may replace it.
                Err(error) if matches!(error.downcast_ref::<Error>(), Some(Error::Transport(inner)) if inner.is_connect()) =>
                    {}
                Err(error) => return Err(error),
            }
        }

        let (log, file) = EngineLog::open(&directory)?;
        let mut child = command
            .stderr(Stdio::from(file))
            .stdout(Stdio::null())
            .spawn()
            .context("Could not start the Python engine")?;
        let start_pid = child.id();
        let candidate = timeout(STARTUP, async {
            let mut exited = None;
            loop {
                if let Some(record) = registration(&directory)?
                    && stale.as_ref() != Some(&record)
                {
                    return Ok::<_, anyhow::Error>(record);
                }
                if child.try_wait()?.is_some()
                    && exited.get_or_insert_with(Instant::now).elapsed() > Duration::from_secs(3)
                {
                    bail!(
                        "Python engine exited before publishing readiness. See {}",
                        log.path.display()
                    );
                }
                sleep(Duration::from_millis(80)).await;
            }
        })
        .await
        .context("Registered engine startup timed out")??;
        let client = candidate.connect(&workspace).await?;
        let owner = if start_pid == Some(candidate.pid) {
            Some(Owner {
                child,
                standalone: false,
            })
        } else {
            allow_existing(&options)?;
            None // A different launcher won the state lock; never stop it.
        };
        let watch = watch(directory, workspace, client.clone(), candidate);
        Ok(EngineProcess {
            client,
            owner,
            watch: Some(watch),
        })
    }

    #[cfg(test)]
    mod tests {
        use std::os::unix::fs::{PermissionsExt, symlink};

        use super::*;

        const RECORD: &[u8] = br#"{"url":"http://127.0.0.1:1234","token":"0123456789012345678901234567890123456789","pid":12,"protocol":1}"#;

        #[test]
        fn readiness_is_loopback_scoped_and_protocol_checked() {
            assert!(Registration::decode(RECORD).is_ok());
            let valid = std::str::from_utf8(RECORD).unwrap();
            for change in [
                valid.replace("127.0.0.1", "example.com"),
                valid.replace(":1234", ":1234/path"),
                valid.replace("\"protocol\":1", "\"protocol\":2"),
                valid.replace("\"pid\":12", "\"pid\":0"),
            ] {
                assert!(Registration::decode(change.as_bytes()).is_err());
            }
        }

        #[test]
        fn discovery_rejects_unprotected_and_linked_registrations() {
            let tmp = crate::test_support::private_tempdir().unwrap();
            // The engine reports a resolved state directory; macOS temp paths
            // go through the /var -> /private/var symlink, which discovery refuses.
            let root = &tmp.path().canonicalize().unwrap();
            let record = root.join("engine.json");
            assert!(registration(root).unwrap().is_none());
            std::fs::write(&record, RECORD).unwrap();
            std::fs::set_permissions(&record, std::fs::Permissions::from_mode(0o644)).unwrap();
            assert!(registration(root).is_err());
            std::fs::set_permissions(&record, std::fs::Permissions::from_mode(0o600)).unwrap();
            assert!(registration(root).unwrap().is_some());
            let other = root.join("other");
            std::fs::hard_link(&record, &other).unwrap();
            assert!(registration(root).is_err());
            std::fs::remove_file(&record).unwrap();
            symlink(other, &record).unwrap();
            assert!(registration(root).is_err());
        }
    }
}
