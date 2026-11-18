//! Private, bounded authentication subprocess protocol; credentials stay in Python.

use std::{
    fs::{self, File, OpenOptions},
    path::{Component, Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use anyhow::{Context, Result, anyhow, bail, ensure};
use fs2::FileExt;
use ngn_protocol::ProviderAuth;
use serde::Deserialize;
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, BufReader},
    process::{Child, Command},
};
use tokio_util::sync::CancellationToken;

use crate::{Config, tools::ProcessGroup};

const MAX_AUTH_FRAME: usize = 2048;
pub const VERIFICATION_URL: &str = "https://auth.openai.com/codex/device";
const INVALID: &str = "Invalid private authentication response (contents redacted)";
const BUSY: &str = "Authentication identity is in use. Stop all ngn hosts sharing this data directory and finish other auth commands before login/logout.";
const MISSING_CORE: &str = "ChatGPT requires the public nagents.provider.device_auth API, absent from published nagents 0.28.5. Install nagents>=0.28.6 and the matching adapter, or source-built equivalents containing that API.";

/// Stable cooperative metadata lock, not a credential store. Never unlink it.
/// Hosts hold shared ownership for their entire lifetime; identity writers hold
/// exclusive ownership in Python, including if their Rust parent dies.
pub struct IdentityGuard(File);

impl IdentityGuard {
    pub fn host(data_dir: &Path) -> Result<Self> {
        Self::acquire(data_dir, false)
    }

    pub fn mutation(data_dir: &Path) -> Result<Self> {
        Self::acquire(data_dir, true)
    }

    fn acquire(data_dir: &Path, exclusive: bool) -> Result<Self> {
        let directory = data_dir.join(".identity");
        ensure!(
            directory.is_absolute(),
            "Identity ownership requires an absolute data directory"
        );
        let mut current = PathBuf::new();
        let last = directory.components().count() - 1;
        // Reject aliases and writable ancestors before touching a stable flock inode.
        for (index, component) in directory.components().enumerate() {
            ensure!(
                !matches!(component, Component::ParentDir),
                "Identity data directory cannot contain parent traversal"
            );
            current.push(component);
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;
                builder.mode(0o700);
            }
            match builder.create(&current) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(_) => bail!("Cannot open private identity ownership metadata"),
            }
            let mut metadata = current
                .symlink_metadata()
                .context("Cannot inspect identity ownership directory")?;
            if index < last
                && let Some(target) = crate::paths::trusted_system_alias(&current, &metadata)
            {
                // e.g. macOS /var -> /private/var; only root can create or replace it.
                current = target;
                metadata = current
                    .symlink_metadata()
                    .context("Cannot inspect identity ownership directory")?;
            }
            ensure!(
                metadata.is_dir() && !metadata.file_type().is_symlink(),
                "Identity ownership directories cannot be symlinks"
            );
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                let trusted_sticky = metadata.uid() == 0 && metadata.mode() & 0o1000 != 0;
                // Group-writable parents (umask 0002) are accepted; world-writable are not.
                ensure!(
                    metadata.mode() & 0o002 == 0 || trusted_sticky,
                    "Identity ownership requires non-world-writable parent directories"
                );
                if index == last {
                    ensure!(
                        metadata.mode() & 0o777 == 0o700,
                        "Identity ownership directory must be private (700)"
                    );
                }
            }
        }
        let path = directory.join("host.lock");
        let mut options = OpenOptions::new();
        options.create(true).read(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options
                .mode(0o600)
                .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
        }
        let file = options
            .open(&path)
            .context("Cannot open private identity ownership lock")?;
        check_lock(&file, &path)?;
        if exclusive {
            FileExt::try_lock_exclusive(&file)
        } else {
            FileExt::try_lock_shared(&file)
        }
        .map_err(|_| anyhow!(BUSY))?;
        let guard = Self(file);
        check_lock(&guard.0, &path)?;
        Ok(guard)
    }
}

fn check_lock(file: &File, path: &Path) -> Result<()> {
    let info = file
        .metadata()
        .context("Cannot inspect identity ownership lock")?;
    let named = path
        .symlink_metadata()
        .context("Cannot inspect named identity ownership lock")?;
    ensure!(
        info.is_file() && named.is_file() && !named.file_type().is_symlink() && info.len() == 0,
        "Identity ownership lock must be an empty regular file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let parent = path
            .parent()
            .context("No identity ownership directory")?
            .symlink_metadata()?;
        ensure!(
            info.nlink() == 1
                && info.mode() & 0o777 == 0o600
                && info.uid() == parent.uid()
                && (info.dev(), info.ino()) == (named.dev(), named.ino()),
            "Identity ownership lock must be private (600), singly linked and stable"
        );
    }
    Ok(())
}

impl Drop for IdentityGuard {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operation {
    Login,
    Status,
    Logout,
}

impl Operation {
    fn argument(self) -> &'static str {
        match self {
            Self::Login => "login",
            Self::Status => "status",
            Self::Logout => "logout",
        }
    }
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AuthStatus {
    SignedIn,
    SignedOut,
    Unusable,
}

impl AuthStatus {
    pub fn description(self) -> &'static str {
        match self {
            Self::SignedIn => {
                "ChatGPT device login saved (local presence only; not an entitlement or refresh check)."
            }
            Self::SignedOut => "Not signed in to ChatGPT.",
            Self::Unusable => {
                "Saved ChatGPT login is unusable. Check private storage permissions, stop all using hosts, then run ngn auth login."
            }
        }
    }
}

pub struct Challenge {
    pub user_code: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthFrame {
    auth_protocol: u16,
    message: AuthMessage,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum AuthMessage {
    Challenge {
        verification_url: String,
        user_code: String,
    },
    Status {
        status: AuthStatus,
    },
    Error {
        code: ErrorCode,
    },
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum ErrorCode {
    InvalidRequest,
    MissingCore,
    AuthFailed,
    Storage,
    Busy,
    Cancelled,
}

impl ErrorCode {
    fn description(self) -> &'static str {
        match self {
            Self::MissingCore => MISSING_CORE,
            Self::Busy => BUSY,
            Self::InvalidRequest => {
                "Auth runner rejected the private request. Verify the configured runner and adapter installation."
            }
            Self::Storage => {
                "Cannot use private authentication storage. Check owned directories (700), files (600), and local POSIX flock support; no credential details are exposed."
            }
            Self::Cancelled => {
                "Authentication cancelled. Check ngn auth status; completed identity changes are not rolled back."
            }
            Self::AuthFailed => {
                "ChatGPT authentication failed. Check network/service availability and enable device authorization in ChatGPT security/workspace settings, then retry ngn auth login. Upstream details are not exposed."
            }
        }
    }
}

async fn read_auth(reader: &mut (impl AsyncBufRead + Unpin)) -> Result<Option<AuthMessage>> {
    let mut bytes = Vec::new();
    loop {
        let available = reader.fill_buf().await.map_err(|_| anyhow!(INVALID))?;
        if available.is_empty() {
            ensure!(bytes.is_empty(), "{INVALID}");
            return Ok(None);
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let count = newline.map_or(available.len(), |index| index + 1);
        ensure!(bytes.len() + count <= MAX_AUTH_FRAME, "{INVALID}");
        bytes.extend_from_slice(&available[..count]);
        reader.consume(count);
        if newline.is_some() {
            let frame: AuthFrame = serde_json::from_slice(&bytes).map_err(|_| anyhow!(INVALID))?;
            ensure!(frame.auth_protocol == 1, "{INVALID}");
            return Ok(Some(frame.message));
        }
    }
}

async fn exchange(
    reader: &mut (impl AsyncBufRead + Unpin),
    operation: Operation,
    mut present: impl FnMut(Challenge),
) -> Result<AuthStatus> {
    let mut challenged = false;
    let status = loop {
        match read_auth(reader).await?.context("Auth runner exited before a result; verify its installation (upstream details redacted)")? {
            AuthMessage::Challenge { verification_url, user_code } => {
                ensure!(operation == Operation::Login && !challenged && verification_url == VERIFICATION_URL && !user_code.is_empty() && user_code.len() <= 64 && user_code.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'), "{INVALID}");
                challenged = true;
                present(Challenge { user_code });
            }
            AuthMessage::Status { status } => {
                ensure!(match operation {
                    Operation::Login => challenged && status == AuthStatus::SignedIn,
                    Operation::Status => !challenged,
                    Operation::Logout => !challenged && status == AuthStatus::SignedOut,
                }, "{INVALID}");
                break status;
            }
            AuthMessage::Error { code } => bail!(code.description()),
        }
    };
    let extra = tokio::time::timeout(Duration::from_secs(5), read_auth(reader))
        .await
        .context("Auth runner did not close its protocol stream")??;
    ensure!(extra.is_none(), "{INVALID}");
    Ok(status)
}

async fn stop(child: &mut Child) -> Result<()> {
    #[cfg(unix)]
    if let Some(id) = child.id() {
        let _ = nix::sys::signal::killpg(
            nix::unistd::Pid::from_raw(id as i32),
            nix::sys::signal::Signal::SIGTERM,
        );
    }
    if matches!(
        tokio::time::timeout(Duration::from_secs(3), child.wait()).await,
        Ok(Ok(_))
    ) {
        return Ok(());
    }
    #[cfg(unix)]
    if let Some(id) = child.id() {
        let _ = nix::sys::signal::killpg(
            nix::unistd::Pid::from_raw(id as i32),
            nix::sys::signal::Signal::SIGKILL,
        );
    }
    let _ = child.start_kill();
    tokio::time::timeout(Duration::from_secs(3), child.wait())
        .await
        .context("Auth runner forced cleanup timed out; stop all using processes before retrying")?
        .context("Cannot join auth runner cleanup")?;
    Ok(())
}

/// Caller owns cancellation and must await this future after cancelling the token.
/// Auth writes are guarded again in Python: a host that starts between preflight
/// and exec wins ownership and the writer fails closed, never changes identity.
pub async fn run(
    config: &Config,
    operation: Operation,
    cancel: CancellationToken,
    present: impl FnMut(Challenge),
) -> Result<AuthStatus> {
    ensure!(
        cfg!(unix),
        "ChatGPT device authentication currently requires POSIX"
    );
    ensure!(
        config.selected_provider()?.auth == ProviderAuth::Chatgpt,
        "Select an explicit kind openai, auth chatgpt connection in --config before using ngn auth"
    );
    if cancel.is_cancelled() {
        bail!(ErrorCode::Cancelled.description());
    }
    if operation != Operation::Status {
        drop(IdentityGuard::mutation(&config.data_dir)?);
    }
    let path = config.auth_store();
    ensure!(
        path.is_absolute(),
        "Authentication storage requires an absolute host data directory"
    );
    let executable = config
        .runner
        .first()
        .context("Configured auth runner executable is required")?;
    let mut command = Command::new(executable);
    command
        .args(&config.runner[1..])
        .arg("auth")
        .arg(operation.argument())
        .arg("--store")
        .arg(path)
        .current_dir(&config.workspace)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    config.runner_environment(&mut command);
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command.spawn().context("Cannot start configured auth runner; install this adapter in its explicit Python environment")?;
    let group = ProcessGroup(child.id());
    let result = async {
        let mut reader = BufReader::new(child.stdout.take().context("No private auth runner stdout")?);
        let budget = if operation == Operation::Login { 960 } else { 15 };
        let status = tokio::select! {
            result = tokio::time::timeout(Duration::from_secs(budget), exchange(&mut reader, operation, present)) => result.context("Authentication timed out; check ngn auth status before retrying")??,
            _ = cancel.cancelled() => bail!(ErrorCode::Cancelled.description()),
        };
        let exit = tokio::select! {
            exit = tokio::time::timeout(Duration::from_secs(5), child.wait()) => exit.context("Auth runner did not exit after its result")??,
            _ = cancel.cancelled() => bail!(ErrorCode::Cancelled.description()),
        };
        ensure!(exit.success(), "Auth runner exited unsuccessfully (details redacted)");
        Ok(status)
    }.await;
    let cleanup = if result.is_err() {
        stop(&mut child).await
    } else {
        Ok(())
    };
    // Kill any descendants, even if the direct child exited on its own.
    drop(group);
    cleanup?;
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn frame(message: serde_json::Value) -> Vec<u8> {
        let mut bytes = serde_json::to_vec(&json!({"auth_protocol":1, "message":message})).unwrap();
        bytes.push(b'\n');
        bytes
    }

    #[tokio::test]
    async fn auth_frames_reject_secret_fields_unknown_codes_and_unbounded_lines() {
        for secret in [
            "device_auth_id",
            "access_token",
            "refresh_token",
            "id_token",
            "body",
        ] {
            let mut message = json!({"type":"status", "status":"signed_in"});
            message[secret] = json!("PRIVATE-SECRET");
            let bytes = frame(message);
            let error = read_auth(&mut BufReader::new(&bytes[..]))
                .await
                .err()
                .unwrap()
                .to_string();
            assert_eq!(error, INVALID);
            assert!(!error.contains("PRIVATE-SECRET"));
        }
        for bytes in [
            frame(json!({"type":"error", "code":"PRIVATE-SECRET"})),
            frame(json!({"type":"status", "status":"PRIVATE-SECRET"})),
            b"{\"auth_protocol\":2,\"message\":{\"type\":\"status\",\"status\":\"signed_out\"}}\n".to_vec(),
            b"{\"auth_protocol\":1,\"message\":{\"type\":\"status\",\"status\":\"signed_out\"},\"access_token\":\"PRIVATE\"}\n".to_vec(),
            vec![b'x'; MAX_AUTH_FRAME + 1],
            b"PRIVATE-INCOMPLETE".to_vec(),
        ] {
            assert_eq!(read_auth(&mut BufReader::new(&bytes[..])).await.err().unwrap().to_string(), INVALID);
        }
    }

    #[tokio::test]
    async fn only_bounded_official_challenges_and_one_terminal_result_are_allowed() {
        let challenge = json!({"type":"challenge", "verification_url":VERIFICATION_URL, "user_code":"TEST-CODE"});
        let status = json!({"type":"status", "status":"signed_in"});
        let mut bytes = frame(challenge.clone());
        bytes.extend(frame(status.clone()));
        let mut seen = Vec::new();
        assert_eq!(
            exchange(
                &mut BufReader::new(&bytes[..]),
                Operation::Login,
                |challenge| seen.push(challenge.user_code)
            )
            .await
            .unwrap(),
            AuthStatus::SignedIn
        );
        assert_eq!(seen, ["TEST-CODE"]);
        for messages in [
            vec![status.clone()],
            vec![challenge.clone(), challenge.clone(), status.clone()],
            vec![challenge.clone(), status.clone(), status.clone()],
            vec![
                json!({"type":"challenge", "verification_url":"https://example.invalid", "user_code":"TEST"}),
                status.clone(),
            ],
            vec![
                json!({"type":"challenge", "verification_url":VERIFICATION_URL, "user_code":"\u{1b}[31m"}),
                status.clone(),
            ],
            vec![
                json!({"type":"challenge", "verification_url":VERIFICATION_URL, "user_code":"x".repeat(65)}),
                status,
            ],
        ] {
            let bytes = messages.into_iter().flat_map(frame).collect::<Vec<_>>();
            assert!(
                exchange(&mut BufReader::new(&bytes[..]), Operation::Login, |_| ())
                    .await
                    .is_err()
            );
        }
        let bytes = frame(challenge);
        assert!(
            exchange(
                &mut BufReader::new(&bytes[..]),
                Operation::Status,
                |_| panic!("Unexpected challenge presentation")
            )
            .await
            .is_err()
        );
        let bytes = frame(json!({"type":"error", "code":"missing_core"}));
        let error = exchange(&mut BufReader::new(&bytes[..]), Operation::Status, |_| ())
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("0.28.5") && error.contains("source-built"));
    }

    #[test]
    fn ownership_excludes_mutators_and_keeps_all_workspace_hosts_shared() {
        let directory = crate::test_support::private_tempdir().unwrap();
        let one = IdentityGuard::host(directory.path()).unwrap();
        let two = IdentityGuard::host(directory.path()).unwrap();
        assert!(IdentityGuard::mutation(directory.path()).is_err());
        drop(one);
        assert!(IdentityGuard::mutation(directory.path()).is_err());
        drop(two);
        let writer = IdentityGuard::mutation(directory.path()).unwrap();
        assert!(IdentityGuard::host(directory.path()).is_err());
        assert!(IdentityGuard::mutation(directory.path()).is_err());
        drop(writer);
        assert!(IdentityGuard::host(directory.path()).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn private_ownership_metadata_rejects_links_and_public_modes() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let directory = crate::test_support::private_tempdir().unwrap();
        drop(IdentityGuard::host(directory.path()).unwrap());
        let lock = directory.path().join(".identity/host.lock");
        fs::set_permissions(&lock, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(IdentityGuard::host(directory.path()).is_err());
        fs::set_permissions(&lock, fs::Permissions::from_mode(0o600)).unwrap();
        fs::hard_link(&lock, directory.path().join("linked")).unwrap();
        assert!(IdentityGuard::host(directory.path()).is_err());
        let other = crate::test_support::private_tempdir().unwrap();
        symlink(directory.path(), other.path().join(".identity")).unwrap();
        assert!(IdentityGuard::host(other.path()).is_err());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn cancelling_auth_joins_the_child_and_waits_for_graceful_cleanup() {
        use std::os::unix::fs::PermissionsExt;
        let directory = crate::test_support::private_tempdir().unwrap();
        let script = directory.path().join("runner");
        let joined = directory.path().join("joined");
        let pid = directory.path().join("pid");
        fs::write(&script, format!("#!/bin/sh\necho $$ > '{}'\ntrap 'echo joined > {}; exit 0' TERM\nprintf '%s\\n' '{{\"auth_protocol\":1,\"message\":{{\"type\":\"challenge\",\"verification_url\":\"{VERIFICATION_URL}\",\"user_code\":\"TEST-CODE\"}}}}'\nwhile :; do /bin/sleep 1; done\n", pid.display(), joined.display())).unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        let config: Config = serde_json::from_value(json!({
            "data_dir":directory.path().join("state"), "runner":[script], "provider":"s",
            "providers":{"s":{"kind":"openai", "auth":"chatgpt", "model":"test"}}
        }))
        .unwrap();
        let config = config.resolve().unwrap();
        let cancel = CancellationToken::new();
        let error = run(&config, Operation::Login, cancel.clone(), |_| {
            cancel.cancel()
        })
        .await
        .unwrap_err();
        assert!(error.to_string().contains("cancelled"));
        assert!(
            joined.exists(),
            "Cancellation returned before graceful child cleanup"
        );
        let pid: i32 = fs::read_to_string(pid).unwrap().trim().parse().unwrap();
        assert_eq!(
            nix::sys::signal::kill(nix::unistd::Pid::from_raw(pid), None),
            Err(nix::errno::Errno::ESRCH)
        );
        assert!(IdentityGuard::mutation(&config.data_dir).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn fresh_private_fixture_has_exact_mode_and_passes_identity_guard() {
        use std::os::unix::fs::PermissionsExt;
        let directory = crate::test_support::private_tempdir().unwrap();
        assert_eq!(
            directory.path().metadata().unwrap().permissions().mode() & 0o777,
            0o700
        );
        drop(IdentityGuard::host(directory.path()).unwrap());
        assert!(IdentityGuard::mutation(directory.path()).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn group_writable_parent_is_accepted_but_world_writable_fails_closed() {
        use std::os::unix::fs::PermissionsExt;
        let directory = crate::test_support::private_tempdir().unwrap();
        let parent = directory.path().join("group-parent");
        fs::create_dir(&parent).unwrap();
        // Like ~/.local created under the Debian/Ubuntu default umask 0002.
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o775)).unwrap();
        assert!(IdentityGuard::host(&parent.join("state")).is_ok());
        let public = directory.path().join("public-parent");
        fs::create_dir(&public).unwrap();
        fs::set_permissions(&public, fs::Permissions::from_mode(0o777)).unwrap();
        let error = IdentityGuard::host(&public.join("state")).err().unwrap();
        assert!(
            error
                .to_string()
                .contains("non-world-writable parent directories")
        );
    }
}
