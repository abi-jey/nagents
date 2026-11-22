//! Native, bounded tools. Capability-relative files are not an OS sandbox.

mod discovery;

use std::{
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result, bail, ensure};
use cap_std::{ambient_authority, fs::Dir};
use nagents_native::diff;
use ngn_protocol::{ToolCall, ToolDefinition};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::{io::AsyncReadExt, process::Command};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const MAX_FILE: usize = 262_144;
const MAX_OUTPUT: usize = 32_768;

#[derive(Clone)]
pub struct Tools {
    root: Arc<Dir>,
    workspace: PathBuf,
    protected: PathBuf,
    read_only: bool,
    #[cfg(any(test, feature = "test-support"))]
    demo_preview: bool,
    shell_timeout: Duration,
}

pub struct Prepared {
    pub preview: String,
    pub needs_approval: bool,
    action: Action,
}

enum Action {
    Result(Value),
    Write {
        path: String,
        original: Option<Vec<u8>>,
        content: Vec<u8>,
    },
    Shell(String),
}

fn one() -> usize {
    1
}
fn lines() -> usize {
    200
}
fn paths() -> usize {
    200
}
fn dot() -> String {
    ".".into()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadFile {
    path: String,
    #[serde(default = "one")]
    start_line: usize,
    #[serde(default = "lines")]
    limit: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Listing {
    #[serde(default = "dot")]
    path: String,
    #[serde(default = "paths")]
    limit: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Search {
    query: String,
    #[serde(default = "dot")]
    path: String,
    #[serde(default = "paths")]
    limit: usize,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteFile {
    path: String,
    content: String,
    /// Empty means create-only. Existing files require the exact inspected digest.
    #[serde(default)]
    expected_sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Edit {
    path: String,
    old: String,
    new: String,
    expected_sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Shell {
    command: String,
}

pub fn bounded(text: &str, limit: usize) -> String {
    if text.len() <= limit {
        return text.to_owned();
    }
    let mut end = limit;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}\n[truncated]", &text[..end])
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn denied_component(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    name == ".env"
        || name.starts_with(".env.")
        || name.ends_with(".pem")
        || name.ends_with(".key")
        || [
            ".git",
            ".ngn",
            ".ssh",
            ".aws",
            ".azure",
            ".gnupg",
            ".kube",
            ".docker",
            ".gcloud",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            "credentials",
            "credentials.json",
            "credentials.toml",
            "secrets",
            "secrets.json",
            "secrets.yaml",
            "secrets.yml",
            "auth.json",
            "id_rsa",
            "id_ed25519",
            "kubeconfig",
            "service-account.json",
            "application_default_credentials.json",
        ]
        .contains(&name.as_str())
}

impl Tools {
    pub fn open(workspace: &Path, state: &Path, read_only: bool, timeout: u64) -> Result<Self> {
        Ok(Self {
            root: Arc::new(Dir::open_ambient_dir(workspace, ambient_authority())?),
            workspace: workspace.to_owned(),
            protected: state.to_owned(),
            read_only,
            #[cfg(any(test, feature = "test-support"))]
            demo_preview: false,
            shell_timeout: Duration::from_secs(timeout),
        })
    }

    #[cfg(any(test, feature = "test-support"))]
    pub(crate) fn with_demo_preview(mut self) -> Self {
        self.read_only = true;
        self.demo_preview = true;
        self
    }

    pub(crate) fn restricted(&self, read_only: bool) -> Self {
        let mut tools = self.clone();
        tools.read_only |= read_only;
        tools
    }

    pub fn definitions(&self) -> Vec<ToolDefinition> {
        let string = json!({"type":"string"});
        let integer = json!({"type":"integer", "minimum":1, "maximum":1000});
        let entries = [
            (
                "read_file",
                "Read bounded workspace text. Returns sha256 for safe editing.",
                json!({"path":string, "start_line":integer, "limit":integer}),
                vec!["path"],
            ),
            (
                "list_files",
                "List workspace files respecting nested .gitignore rules; skips links, credential paths and common build directories.",
                json!({"path":string, "limit":integer}),
                vec![],
            ),
            (
                "search",
                "Search for a literal string in bounded workspace text files, respecting nested .gitignore rules even for explicit file paths.",
                json!({"query":string, "path":string, "limit":integer}),
                vec!["query"],
            ),
            (
                "write",
                "Create a text file or replace an inspected file. Existing files require expected_sha256 from read_file. Requires approval.",
                json!({"path":string, "content":string, "expected_sha256":string}),
                vec!["path", "content"],
            ),
            (
                "edit",
                "Replace one unique literal occurrence in an inspected file. Requires its sha256 and approval.",
                json!({"path":string, "old":string, "new":string, "expected_sha256":string}),
                vec!["path", "old", "new", "expected_sha256"],
            ),
            (
                "shell",
                "Run a shell command in the workspace after explicit approval. NOT sandboxed; full user access.",
                json!({"command":string}),
                vec!["command"],
            ),
            #[cfg(any(test, feature = "test-support"))]
            (
                "demo_preview",
                "Offline approval preview. Never writes a file.",
                json!({}),
                vec![],
            ),
        ];
        entries.into_iter()
            .filter(|(name, ..)| match *name {
                #[cfg(any(test, feature = "test-support"))]
                "demo_preview" => self.demo_preview,
                "write" | "edit" | "shell" => !self.read_only,
                _ => true,
            })
            .map(|(name, description, properties, required)| ToolDefinition { name: name.into(), description: description.into(), parameters: json!({"type":"object", "properties":properties, "required":required, "additionalProperties":false}) }).collect()
    }

    fn relative(&self, path: &str, directory: bool) -> Result<PathBuf> {
        ensure!(
            !path.is_empty()
                && path.len() <= 4096
                && !path.contains('\\')
                && !path.chars().any(char::is_control),
            "Invalid workspace path"
        );
        let relative = Path::new(path);
        let mut clean = PathBuf::new();
        for part in relative.components() {
            match part {
                Component::Normal(name) => {
                    ensure!(
                        !denied_component(&name.to_string_lossy()),
                        "Credential and internal paths are blocked"
                    );
                    clean.push(name);
                    if let Ok(metadata) = self.root.symlink_metadata(&clean) {
                        ensure!(!metadata.file_type().is_symlink(), "Symlinks are blocked");
                    }
                }
                Component::CurDir => (),
                _ => bail!("Path must stay inside the workspace; traversal is blocked"),
            }
        }
        ensure!(
            directory || !clean.as_os_str().is_empty(),
            "File path is required"
        );
        ensure!(
            !self.workspace.join(&clean).starts_with(&self.protected),
            "Runtime state is not accessible to tools"
        );
        Ok(if clean.as_os_str().is_empty() {
            PathBuf::from(".")
        } else {
            clean
        })
    }

    fn read(&self, path: &str) -> Result<Vec<u8>> {
        let path = self.relative(path, false)?;
        let parent = path
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let directory = self.directory(parent)?;
        let name = path.file_name().context("File name required")?;
        let mut options = cap_std::fs::OpenOptions::new();
        options.read(true);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt;
            options.custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
        }
        let mut file = directory.open_with(name, &options)?.into_std();
        let metadata = file.metadata()?;
        ensure!(
            metadata.is_file() && metadata.len() <= MAX_FILE as u64,
            "Only regular files up to 256 KiB are supported"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            ensure!(metadata.nlink() == 1, "Hard-linked files are blocked");
        }
        let mut bytes = Vec::new();
        (&mut file)
            .take(MAX_FILE as u64 + 1)
            .read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= MAX_FILE, "File exceeds 256 KiB");
        Ok(bytes)
    }

    fn directory(&self, path: &Path) -> Result<Dir> {
        let mut directory = self.root.try_clone()?;
        for component in path.components() {
            match component {
                Component::CurDir => (),
                Component::Normal(name) => {
                    let mut options = cap_std::fs::OpenOptions::new();
                    options.read(true);
                    #[cfg(unix)]
                    {
                        use cap_std::fs::OpenOptionsExt;
                        options.custom_flags(
                            nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK | nix::libc::O_DIRECTORY,
                        );
                    }
                    let opened = directory.open_with(name, &options)?;
                    ensure!(
                        opened.metadata()?.is_dir(),
                        "Expected a regular workspace directory"
                    );
                    directory = Dir::from_std_file(opened.into_std());
                }
                _ => bail!("Invalid workspace directory"),
            }
        }
        Ok(directory)
    }

    fn walk(&self, path: &str) -> Result<(Vec<String>, bool)> {
        discovery::walk(self, path, false)
    }

    pub fn prepare(&self, call: &ToolCall) -> Result<Prepared> {
        ensure!(
            call.call_id.len() <= 200 && !call.call_id.is_empty(),
            "Invalid call_id"
        );
        ensure!(
            call.arguments.is_object(),
            "Tool arguments must be an object"
        );
        let mut prepared = Prepared {
            preview: String::new(),
            needs_approval: false,
            action: Action::Result(Value::Null),
        };
        prepared.action = match call.name.as_str() {
            "read_file" => {
                let args: ReadFile = serde_json::from_value(call.arguments.clone())?;
                ensure!(
                    (1..=1000).contains(&args.limit) && args.start_line > 0,
                    "Invalid line bounds"
                );
                let bytes = self.read(&args.path)?;
                let text = std::str::from_utf8(&bytes).context("File is not UTF-8 text")?;
                let selected = text
                    .lines()
                    .enumerate()
                    .skip(args.start_line - 1)
                    .take(args.limit)
                    .map(|(n, line)| format!("{}: {line}", n + 1))
                    .collect::<Vec<_>>()
                    .join("\n");
                Action::Result(
                    json!({"path":args.path, "sha256":digest(&bytes), "content":bounded(&selected, MAX_OUTPUT), "total_lines":text.lines().count(), "truncated":selected.len() > MAX_OUTPUT}),
                )
            }
            "list_files" => {
                let args: Listing = serde_json::from_value(call.arguments.clone())?;
                ensure!((1..=1000).contains(&args.limit), "limit must be 1..1000");
                let (files, capped) = self.walk(&args.path)?;
                let mut selected = Vec::new();
                let mut truncated = capped;
                let mut output_bytes = 64; // Reserve the result envelope and separators.
                for path in files {
                    let size = serde_json::to_vec(&path)?.len() + 1;
                    if selected.len() == args.limit || output_bytes + size > MAX_OUTPUT {
                        truncated = true;
                        break;
                    }
                    output_bytes += size;
                    selected.push(path);
                }
                Action::Result(json!({"paths":selected, "truncated":truncated}))
            }
            "search" => {
                let args: Search = serde_json::from_value(call.arguments.clone())?;
                ensure!(
                    !args.query.is_empty()
                        && args.query.len() <= 4096
                        && (1..=1000).contains(&args.limit),
                    "Invalid search query or limit"
                );
                let (files, capped) = discovery::walk(self, &args.path, true)?;
                let mut matches = Vec::new();
                let mut bytes_read = 0;
                let mut truncated = capped;
                let mut output_bytes = 64;
                'files: for path in files {
                    let Ok(bytes) = self.read(&path) else {
                        continue;
                    };
                    bytes_read += bytes.len();
                    if bytes_read > 8 * 1024 * 1024 {
                        truncated = true;
                        break;
                    }
                    let Ok(text) = std::str::from_utf8(&bytes) else {
                        continue;
                    };
                    for (number, line) in text.lines().enumerate() {
                        if line.contains(&args.query) {
                            let found =
                                json!({"path":path, "line":number + 1, "text":bounded(line, 500)});
                            let size = serde_json::to_vec(&found)?.len() + 1;
                            if output_bytes + size > MAX_OUTPUT {
                                truncated = true;
                                break 'files;
                            }
                            output_bytes += size;
                            matches.push(found);
                            if matches.len() >= args.limit {
                                truncated = true;
                                break 'files;
                            }
                        }
                    }
                }
                Action::Result(json!({"matches":matches, "truncated":truncated}))
            }
            "write" | "edit" => {
                ensure!(!self.read_only, "Workspace mutations are disabled");
                let (path, content, expected) = if call.name == "write" {
                    let args: WriteFile = serde_json::from_value(call.arguments.clone())?;
                    (args.path, args.content, args.expected_sha256)
                } else {
                    let args: Edit = serde_json::from_value(call.arguments.clone())?;
                    ensure!(!args.old.is_empty(), "old text must be nonempty");
                    let bytes = self.read(&args.path)?;
                    let text = std::str::from_utf8(&bytes)?;
                    ensure!(
                        text.matches(&args.old).count() == 1,
                        "old text must match exactly once"
                    );
                    let content = text.replacen(&args.old, &args.new, 1);
                    (args.path, content, args.expected_sha256)
                };
                ensure!(content.len() <= MAX_FILE, "New content exceeds 256 KiB");
                let relative = self.relative(&path, false)?;
                let original = match self.root.symlink_metadata(&relative) {
                    Ok(_) => {
                        let bytes = self.read(&path)?;
                        ensure!(
                            !expected.is_empty() && digest(&bytes) == expected,
                            "File has changed or has not been inspected; read_file again"
                        );
                        Some(bytes)
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                        ensure!(expected.is_empty(), "Expected an existing file");
                        None
                    }
                    Err(error) => return Err(error.into()),
                };
                let old = original
                    .clone()
                    .map(String::from_utf8)
                    .transpose()
                    .context("Existing file is not UTF-8")?;
                prepared.preview = bounded(&diff::change(&path, old.as_deref(), &content), 48_000);
                // Never approve hidden portions of a large write.
                ensure!(
                    prepared.preview.len() < 48_000,
                    "Change preview exceeds 48 KiB; split it into smaller edits"
                );
                prepared.needs_approval = true;
                Action::Write {
                    path,
                    original,
                    content: content.into_bytes(),
                }
            }
            "shell" => {
                ensure!(!self.read_only, "Shell execution is disabled");
                let args: Shell = serde_json::from_value(call.arguments.clone())?;
                ensure!(
                    !args.command.trim().is_empty() && args.command.len() <= 16_384,
                    "Invalid shell command"
                );
                prepared.preview = format!(
                    "$ {}\n\nRuns unsandboxed as your user · timeout {}s\nWorkspace: {}",
                    args.command,
                    self.shell_timeout.as_secs(),
                    self.workspace.display()
                );
                prepared.needs_approval = true;
                Action::Shell(args.command)
            }
            #[cfg(any(test, feature = "test-support"))]
            "demo_preview" => {
                ensure!(
                    self.demo_preview && call.arguments == json!({}),
                    "Unavailable preview tool or invalid arguments"
                );
                prepared.preview = "OFFLINE PREVIEW ONLY\n--- demo.txt\n+++ demo.txt\n-Hello\n+Hello from Rust\n\nNo file will be written.".into();
                prepared.needs_approval = true;
                Action::Result(json!({"preview_only":true, "written":false}))
            }
            _ => bail!("Unknown or unavailable host tool"),
        };
        Ok(prepared)
    }

    pub async fn execute(&self, prepared: Prepared, cancel: CancellationToken) -> Result<Value> {
        ensure!(!cancel.is_cancelled(), "Run was cancelled");
        match prepared.action {
            Action::Result(value) => Ok(value),
            Action::Write {
                path,
                original,
                content,
            } => {
                let tools = self.clone();
                tokio::task::spawn_blocking(move || {
                    tools.commit(&path, original.as_deref(), &content, &cancel)
                })
                .await?
            }
            Action::Shell(command) => self.shell(&command, cancel).await,
        }
    }

    fn commit(
        &self,
        path: &str,
        original: Option<&[u8]>,
        content: &[u8],
        cancel: &CancellationToken,
    ) -> Result<Value> {
        let relative = self.relative(path, false)?;
        ensure!(!cancel.is_cancelled(), "Run was cancelled");
        match original {
            Some(original) => ensure!(
                self.read(path)? == original,
                "File changed while approval was pending; no write performed"
            ),
            None => ensure!(
                !self.root.try_exists(&relative)?,
                "File appeared while approval was pending; no write performed"
            ),
        }
        let parent = relative
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let directory = self
            .directory(parent)
            .context("Parent directory must already exist")?;
        let name = relative.file_name().context("File name required")?;
        if original.is_none() {
            let mut options = cap_std::fs::OpenOptions::new();
            options.write(true).create_new(true);
            let mut file = directory.open_with(name, &options)?.into_std();
            file.write_all(content)?;
            file.sync_all()?;
        } else {
            let temporary = format!(".ngn-write-{}", Uuid::new_v4().simple());
            let result = (|| -> Result<()> {
                let mut options = cap_std::fs::OpenOptions::new();
                options.write(true).create_new(true);
                let mut file = directory.open_with(&temporary, &options)?.into_std();
                let mode = directory.open(name)?.into_std().metadata()?.permissions();
                file.set_permissions(mode)?;
                file.write_all(content)?;
                file.sync_all()?;
                ensure!(!cancel.is_cancelled(), "Run was cancelled");
                ensure!(
                    self.read(path)?.as_slice() == original.unwrap(),
                    "File changed before commit"
                );
                directory.rename(&temporary, &directory, name)?;
                Ok(())
            })();
            if result.is_err() {
                let _ = directory.remove_file(&temporary);
            }
            result?;
        }
        Ok(json!({"path":path, "bytes":content.len(), "sha256":digest(content)}))
    }

    async fn shell(&self, command: &str, cancel: CancellationToken) -> Result<Value> {
        #[cfg(unix)]
        let mut process = {
            let mut command_builder = Command::new("/bin/sh");
            command_builder.args(["-c", command]);
            command_builder.process_group(0);
            command_builder
        };
        #[cfg(not(unix))]
        let mut process = {
            let mut command_builder = Command::new("cmd");
            command_builder.args(["/C", command]);
            command_builder
        };
        let mut child = process
            .current_dir(&self.workspace)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()?;
        let group = ProcessGroup(child.id());
        let stdout = tokio::spawn(drain(child.stdout.take().context("No shell stdout")?));
        let stderr = tokio::spawn(drain(child.stderr.take().context("No shell stderr")?));
        let status = tokio::select! {
            result = child.wait() => result.map(Some),
            _ = cancel.cancelled() => Ok(None),
            _ = tokio::time::sleep(self.shell_timeout) => Ok(None),
        };
        drop(group); // Also kill descendants that outlived their shell.
        let status = match status? {
            Some(status) => status,
            None => {
                child.kill().await.ok();
                child.wait().await.ok();
                stdout.abort();
                stderr.abort();
                bail!("Shell cancelled or timed out; completed actions are not rolled back");
            }
        };
        let (stdout, stderr) = tokio::time::timeout(Duration::from_secs(2), async {
            Ok::<_, anyhow::Error>((stdout.await??, stderr.await??))
        })
        .await
        .context("Shell output pipes did not close")??;
        Ok(
            json!({"exit_code":status.code(), "success":status.success(), "stdout":stdout.0, "stderr":stderr.0, "truncated":stdout.1 || stderr.1}),
        )
    }
}

async fn drain(mut stream: impl tokio::io::AsyncRead + Unpin) -> Result<(String, bool)> {
    let mut output = Vec::new();
    let mut buffer = [0; 4096];
    let mut truncated = false;
    loop {
        let count = stream.read(&mut buffer).await?;
        if count == 0 {
            break;
        }
        let available = (MAX_OUTPUT / 2).saturating_sub(output.len());
        output.extend_from_slice(&buffer[..count.min(available)]);
        truncated |= count > available;
    }
    Ok((String::from_utf8_lossy(&output).into_owned(), truncated))
}

pub(crate) struct ProcessGroup(pub Option<u32>);
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        #[cfg(unix)]
        if let Some(id) = self.0 {
            let _ = nix::sys::signal::killpg(
                nix::unistd::Pid::from_raw(id as i32),
                nix::sys::signal::Signal::SIGKILL,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tools(directory: &Path, read_only: bool) -> Tools {
        Tools::open(directory, &directory.join("state"), read_only, 1).unwrap()
    }
    fn call(name: &str, arguments: Value) -> ToolCall {
        ToolCall {
            call_id: "c".into(),
            name: name.into(),
            arguments,
        }
    }

    #[tokio::test]
    async fn writes_revalidate_after_approval() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("hello.txt"), "before").unwrap();
        let tools = tools(directory.path(), false);
        let prepared = tools
            .prepare(&call(
                "write",
                json!({"path":"hello.txt", "content":"after", "expected_sha256":digest(b"before")}),
            ))
            .unwrap();
        assert!(prepared.needs_approval);
        std::fs::write(directory.path().join("hello.txt"), "someone else's change").unwrap();
        assert!(
            tools
                .execute(prepared, CancellationToken::new())
                .await
                .is_err()
        );
        assert_eq!(
            std::fs::read_to_string(directory.path().join("hello.txt")).unwrap(),
            "someone else's change"
        );
    }

    #[test]
    fn paths_policy_and_schema_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let tools = tools(directory.path(), true);
        for path in [
            "../secrets",
            "/etc/passwd",
            ".env",
            ".ssh/id_rsa",
            "state/host.sqlite3",
        ] {
            assert!(
                tools
                    .prepare(&call("read_file", json!({"path":path})))
                    .is_err(),
                "{path}"
            );
        }
        assert!(
            tools
                .prepare(&call("shell", json!({"command":"true"})))
                .is_err()
        );
        assert!(
            tools
                .prepare(&call("list_files", json!({"approved":true})))
                .is_err()
        );
        assert!(
            tools
                .prepare(&call("write", json!({"path":"x", "content":"x"})))
                .is_err()
        );
    }

    #[cfg(unix)]
    #[test]
    fn links_are_blocked() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("file"), "x").unwrap();
        std::os::unix::fs::symlink("file", directory.path().join("link")).unwrap();
        let tools = tools(directory.path(), false);
        assert!(tools.read("link").is_err());
        std::fs::hard_link(
            directory.path().join("file"),
            directory.path().join("hardlink"),
        )
        .unwrap();
        assert!(tools.read("file").is_err());
    }

    #[tokio::test]
    async fn native_read_search_write_and_edit_round_trip() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::create_dir(directory.path().join("nested")).unwrap();
        std::fs::write(directory.path().join("nested/file.txt"), "hello\nworld\n").unwrap();
        std::fs::write(directory.path().join(".env"), "not discoverable").unwrap();
        let tools = tools(directory.path(), false);
        let cancel = CancellationToken::new();
        let read = tools
            .prepare(&call("read_file", json!({"path":"nested/file.txt"})))
            .unwrap();
        let read = tools.execute(read, cancel.clone()).await.unwrap();
        assert_eq!(read["content"], "1: hello\n2: world");
        let search = tools
            .prepare(&call("search", json!({"query":"world"})))
            .unwrap();
        let search = tools.execute(search, cancel.clone()).await.unwrap();
        assert_eq!(search["matches"][0]["path"], "nested/file.txt");
        let edit = tools.prepare(&call("edit", json!({"path":"nested/file.txt", "old":"world", "new":"Rust", "expected_sha256":read["sha256"]}))).unwrap();
        tools.execute(edit, cancel.clone()).await.unwrap();
        assert_eq!(
            std::fs::read_to_string(directory.path().join("nested/file.txt")).unwrap(),
            "hello\nRust\n"
        );
        let write = tools
            .prepare(&call("write", json!({"path":"new.txt", "content":"new"})))
            .unwrap();
        tools.execute(write, cancel).await.unwrap();
        assert_eq!(
            std::fs::read_to_string(directory.path().join("new.txt")).unwrap(),
            "new"
        );
        let (files, _) = tools.walk(".").unwrap();
        assert!(!files.iter().any(|path| path.contains(".env")));
    }

    #[test]
    fn previews_are_unified_diffs_and_oversized_changes_are_refused() {
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("a.txt"), "hello\nworld\n").unwrap();
        let tools = tools(directory.path(), false);
        let sha = digest(b"hello\nworld\n");
        let edit = tools
            .prepare(&call(
                "edit",
                json!({"path":"a.txt", "old":"world", "new":"Rust", "expected_sha256":sha}),
            ))
            .unwrap();
        assert_eq!(
            edit.preview,
            "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n hello\n-world\n+Rust\n"
        );
        let fresh = tools
            .prepare(&call("write", json!({"path":"b.txt", "content":"x\n"})))
            .unwrap();
        assert!(
            fresh
                .preview
                .starts_with("--- /dev/null\n+++ b/b.txt (new file)\n")
        );
        // Hidden portions are never approved: a diff over the cap is rejected.
        let huge: String = (0..20_000).map(|n| format!("line {n}\n")).collect();
        let error = tools
            .prepare(&call("write", json!({"path":"c.txt", "content":huge})))
            .err()
            .unwrap();
        assert!(error.to_string().contains("48 KiB"), "{error}");
        let shell = tools
            .prepare(&call("shell", json!({"command":"ls -la"})))
            .unwrap();
        assert!(
            shell
                .preview
                .starts_with("$ ls -la\n\nRuns unsandboxed as your user · timeout ")
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn shell_exit_status_output_and_cancellation_are_real() {
        let directory = tempfile::tempdir().unwrap();
        let tools = tools(directory.path(), false);
        let output = tools
            .shell("printf hello", CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(output["stdout"], "hello");
        assert_eq!(output["success"], true);
        let failed = tools
            .shell("exit 7", CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(failed["exit_code"], 7);
        assert_eq!(failed["success"], false);
        let cancel = CancellationToken::new();
        let cancellation = cancel.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(30)).await;
            cancellation.cancel();
        });
        let started = std::time::Instant::now();
        assert!(tools.shell("sleep 30", cancel).await.is_err());
        assert!(started.elapsed() < Duration::from_secs(2));
    }
}
