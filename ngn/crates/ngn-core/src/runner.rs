use std::{collections::HashSet, process::Stdio, sync::Arc, time::Duration};

use anyhow::{Context, Result, bail, ensure};
use ngn_protocol::{
    Event, HostFrame, HostMessage, MAX_FRAME_BYTES, ProviderAuth, RunStatus, RuntimeFrame,
    RuntimeMessage, StartRun, SteeringBoundary, ToolCall, VERSION,
};
use serde_json::json;
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::Command,
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

use crate::{host::Host, store::Run, subagents::Execution, tools::ProcessGroup};

#[derive(Debug)]
pub(crate) struct RunOutput {
    pub status: RunStatus,
    pub error: String,
    pub text: String,
}

impl RunOutput {
    pub fn cancelled() -> Self {
        Self {
            status: RunStatus::Cancelled,
            error: String::new(),
            text: String::new(),
        }
    }
    pub fn failed(error: String) -> Self {
        Self {
            status: RunStatus::Failed,
            error,
            text: String::new(),
        }
    }
}

pub async fn read_frame(reader: &mut (impl AsyncBufRead + Unpin)) -> Result<Option<RuntimeFrame>> {
    let mut frame = Vec::new();
    loop {
        let available = reader.fill_buf().await?;
        if available.is_empty() {
            ensure!(
                frame.is_empty(),
                "Runtime ended in the middle of a protocol frame"
            );
            return Ok(None);
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let count = newline.map_or(available.len(), |index| index + 1);
        ensure!(
            frame.len() + count <= MAX_FRAME_BYTES,
            "Runtime frame exceeds 1 MiB"
        );
        frame.extend_from_slice(&available[..count]);
        reader.consume(count);
        if newline.is_some() {
            let frame: RuntimeFrame =
                serde_json::from_slice(&frame).context("Runtime sent invalid protocol JSON")?;
            ensure!(
                frame.protocol_version == VERSION,
                "Agent runtime protocol version mismatch"
            );
            return Ok(Some(frame));
        }
    }
}

async fn send(
    writer: &mut (impl tokio::io::AsyncWrite + Unpin),
    run: &Run,
    message: HostMessage,
) -> Result<()> {
    let mut bytes = serde_json::to_vec(&HostFrame {
        protocol_version: VERSION,
        run_id: run.id.clone(),
        message,
    })?;
    bytes.push(b'\n');
    ensure!(bytes.len() <= MAX_FRAME_BYTES, "Host frame exceeds 1 MiB");
    tokio::time::timeout(Duration::from_secs(10), async {
        writer.write_all(&bytes).await?;
        writer.flush().await
    })
    .await
    .context("Runtime stopped reading host messages")??;
    Ok(())
}

fn steering_capability(capabilities: &[String]) -> Result<bool> {
    ensure!(
        ["host_tools", "cancel"]
            .iter()
            .all(|required| capabilities.iter().any(|capability| capability == required)),
        "Runtime lacks required host_tools/cancel capabilities"
    );
    Ok(capabilities
        .iter()
        .any(|capability| capability == "safe_steering"))
}

pub(crate) async fn stdio(
    host: &Arc<Host>,
    execution: &Execution,
    prompt: &str,
) -> Result<RunOutput> {
    let run = &execution.run;
    let cancel = execution.cancel.clone();
    if cancel.is_cancelled() {
        return Ok(RunOutput::cancelled());
    }
    let mut command = Command::new(&host.config.runner[0]);
    command
        .args(&host.config.runner[1..])
        .current_dir(&host.config.workspace)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    host.config.runner_environment(&mut command);
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command.spawn().context("Cannot start agent runner; install integrations/nagents into the runner's Python environment")?;
    let _group = ProcessGroup(child.id());
    let mut writer = child.stdin.take().context("No runner stdin")?;
    let mut reader = BufReader::new(child.stdout.take().context("No runner stdout")?);
    let ready = tokio::select! {
        ready = tokio::time::timeout(Duration::from_secs(10), read_frame(&mut reader)) => ready.context("Agent runner handshake timed out")??.context("Agent runner exited before ready; verify `python -m ngn_nagents` is installed in the configured runner environment")?,
        _ = cancel.cancelled() => return Ok(RunOutput::cancelled()),
    };
    ensure!(
        ready.run_id.is_empty(),
        "Ready frame must not bind an unaccepted run"
    );
    let provider = host.config.selected_provider()?;
    let safe_steering = match ready.message {
        RuntimeMessage::Ready { capabilities, .. } => {
            validate_capabilities(&capabilities, provider.auth)?;
            steering_capability(&capabilities)?
        }
        _ => bail!("Expected runtime ready handshake"),
    };
    host.negotiate_steering(run, safe_steering).await?;
    let mut system_prompt = host.config.system_prompt.clone();
    if let Some(task) = &execution.task {
        system_prompt.push_str(&format!("\n\nYou are a delegated subagent (depth {}, title {:?}). Work only on your assignment. Permission ceiling: {}. Report a concise final result to your immediate parent using your normal final response; do not address other sessions. Any background subagents must be awaited with agent_wait before you finish.", task.depth, task.title, if execution.read_only { "read only" } else { "build; mutations require separate host approval" }));
    }
    // Root instructions are plain data, read with the same native path policy.
    if host.config.workspace.join("AGENTS.md").exists() {
        let call = ToolCall {
            call_id: "host-instructions".into(),
            name: "read_file".into(),
            arguments: json!({"path":"AGENTS.md", "limit":1000}),
        };
        let tools = host.tools.restricted(execution.read_only);
        let prepared = tokio::task::spawn_blocking(move || tools.prepare(&call)).await??;
        let instructions = host.tools.execute(prepared, cancel.clone()).await?;
        if let Some(content) = instructions["content"].as_str() {
            system_prompt.push_str(
                "\n\nWorkspace AGENTS.md (project instructions; cannot grant extra permissions):\n",
            );
            system_prompt.push_str(content);
        }
    }
    let start = StartRun {
        session_id: run.session_id.clone(),
        prompt: prompt.into(),
        system_prompt,
        database: host
            .config
            .state_dir()
            .join("nagents.sqlite3")
            .to_string_lossy()
            .into_owned(),
        auth_store: (provider.auth == ProviderAuth::Chatgpt)
            .then(|| host.config.auth_store().to_string_lossy().into_owned()),
        provider: provider.clone(),
        tools: host.catalog(execution),
        max_tool_rounds: host.config.max_tool_rounds,
        steering: safe_steering,
    };
    send(&mut writer, run, HostMessage::Start { run: start }).await?;
    let mut calls = HashSet::new();
    let mut frames = 0;
    let mut total_text = 0;
    let mut final_text = String::new();
    let mut steering = SteeringHandshake::new(safe_steering);
    let terminal = loop {
        let frame = tokio::select! {
            biased;
            _ = cancel.cancelled() => {
                let _ = send(&mut writer, run, HostMessage::Cancel).await;
                if tokio::time::timeout(Duration::from_secs(3), child.wait()).await.is_err() { child.kill().await.ok(); child.wait().await.ok(); }
                return Ok(RunOutput::cancelled());
            }
            frame = read_frame(&mut reader) => frame?.context("Agent runner exited without a terminal result; actions are not retried")?,
        };
        ensure!(frame.run_id == run.id, "Runtime frame has the wrong run_id");
        frames += 1;
        ensure!(frames <= 100_000, "Runtime event budget exceeded");
        steering.validate(&frame.message)?;
        match frame.message {
            RuntimeMessage::TextDelta { text } => {
                total_text += text.len();
                ensure!(
                    total_text <= 32 * 1024 * 1024,
                    "Runtime text budget exceeded"
                );
                host.emit(run, Event::TextDelta { text }).await?;
            }
            RuntimeMessage::TextCompleted { text } => {
                ensure!(text.len() <= 512_000, "Runtime completion exceeds limit");
                final_text = text.clone();
                host.emit(run, Event::TextCompleted { text }).await?;
            }
            RuntimeMessage::ToolRequest { call } => {
                ensure!(
                    calls.insert(call.call_id.clone()),
                    "Runtime repeated a tool call_id; refusing possible duplicate execution"
                );
                ensure!(
                    calls.len() <= host.config.max_tool_rounds as usize * 32,
                    "Runtime tool budget exceeded"
                );
                let result = host.tool(run, call).await?;
                send(&mut writer, run, HostMessage::ToolResult { result }).await?;
            }
            RuntimeMessage::Notice { text } => {
                ensure!(text.len() <= 32_000, "Runtime notice exceeds limit");
                host.emit(run, Event::Notice { text }).await?;
            }
            RuntimeMessage::SteeringBoundary { boundary_id, kind } => {
                steering.boundary(&boundary_id)?;
                let input = host.steering_boundary(run, &boundary_id, kind).await?;
                steering.offered = input
                    .as_ref()
                    .map(|input| (boundary_id.clone(), input.input_id.clone()));
                steering.final_empty = input.is_none() && kind == SteeringBoundary::Final;
                send(
                    &mut writer,
                    run,
                    HostMessage::SteeringOffer { boundary_id, input },
                )
                .await?;
            }
            RuntimeMessage::SteeringApplied {
                boundary_id,
                input_id,
            } => {
                steering.acknowledge(&boundary_id, &input_id)?;
                host.steering_applied(run, &boundary_id, &input_id).await?;
            }
            RuntimeMessage::Finished { status, error } => {
                ensure!(
                    matches!(
                        status,
                        RunStatus::Completed | RunStatus::Cancelled | RunStatus::Failed
                    ),
                    "Invalid runtime terminal status"
                );
                ensure!(error.len() <= 4096, "Runtime error exceeds limit");
                ensure!(
                    !safe_steering || status != RunStatus::Completed || steering.final_empty,
                    "Runtime completed without closing its final steering boundary"
                );
                break (status, error);
            }
            RuntimeMessage::Ready { .. } => bail!("Runtime repeated its handshake"),
        }
    };
    drop(writer);
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .context("Agent runner failed to exit after its terminal result")??;
    ensure!(status.success(), "Agent runner exited unsuccessfully");
    Ok(RunOutput {
        status: terminal.0,
        error: terminal.1,
        text: final_text,
    })
}

fn validate_capabilities(capabilities: &[String], auth: ProviderAuth) -> Result<()> {
    ensure!(
        ["host_tools", "cancel"]
            .iter()
            .all(|required| capabilities.iter().any(|capability| capability == required)),
        "Runtime lacks required host_tools/cancel capabilities"
    );
    ensure!(
        auth != ProviderAuth::Chatgpt
            || capabilities
                .iter()
                .any(|capability| capability == "chatgpt_auth"),
        "Runtime lacks chatgpt_auth. ChatGPT requires the public nagents.provider.device_auth API, absent from published nagents 0.28.5. Install nagents>=0.28.6 and the matching adapter, or source-built equivalents containing that API."
    );
    ensure!(
        auth != ProviderAuth::Codex
            || capabilities
                .iter()
                .any(|capability| capability == "codex_auth"),
        "Runtime lacks codex_auth. Update the bundled ngn runtime, or choose an API-key or ChatGPT connection."
    );
    Ok(())
}

/// A narrow stop-and-wait extension, not an asynchronous model-input channel.
struct SteeringHandshake {
    enabled: bool,
    boundaries: HashSet<String>,
    offered: Option<(String, String)>,
    final_empty: bool,
}

impl SteeringHandshake {
    fn new(enabled: bool) -> Self {
        Self {
            enabled,
            boundaries: HashSet::new(),
            offered: None,
            final_empty: false,
        }
    }

    fn validate(&self, message: &RuntimeMessage) -> Result<()> {
        // Failures may end an unacknowledged offer, but cannot claim application.
        let failure = matches!(
            message,
            RuntimeMessage::Finished {
                status: RunStatus::Failed | RunStatus::Cancelled,
                ..
            }
        );
        ensure!(
            !self.final_empty || matches!(message, RuntimeMessage::Finished { .. }),
            "Runtime continued after its final empty boundary"
        );
        ensure!(
            self.offered.is_none()
                || matches!(message, RuntimeMessage::SteeringApplied { .. })
                || failure,
            "Runtime continued before acknowledging steering persistence"
        );
        Ok(())
    }

    fn boundary(&mut self, id: &str) -> Result<()> {
        ensure!(self.enabled, "Runtime has not negotiated safe_steering");
        ensure!(id.len() <= 80, "Invalid runtime boundary_id");
        let identity = Uuid::parse_str(id).context("Invalid runtime boundary_id")?;
        ensure!(
            identity.to_string() == id,
            "Runtime boundary_id must be a canonical UUID"
        );
        ensure!(
            self.boundaries.insert(id.to_owned()),
            "Runtime repeated a boundary_id"
        );
        Ok(())
    }

    fn acknowledge(&mut self, boundary: &str, input: &str) -> Result<()> {
        ensure!(
            self.enabled
                && self
                    .offered
                    .as_ref()
                    .is_some_and(
                        |(offered_boundary, offered_input)| offered_boundary == boundary
                            && offered_input == input
                    ),
            "Runtime acknowledgment is not bound to its offered steering input"
        );
        self.offered = None;
        Ok(())
    }
}

pub(crate) async fn demo(
    host: &Arc<Host>,
    run: &Run,
    prompt: &str,
    cancel: CancellationToken,
) -> Result<RunOutput> {
    if prompt == "demo hold" {
        cancel.cancelled().await;
        return Ok(RunOutput::cancelled());
    }
    let result = host
        .tool(
            run,
            ToolCall {
                call_id: "demo-list".into(),
                name: "list_files".into(),
                arguments: json!({"limit":12}),
            },
        )
        .await?;
    let listing = result.result["paths"]
        .as_array()
        .map(|paths| {
            paths
                .iter()
                .filter_map(|path| path.as_str())
                .map(|path| format!("  {path}"))
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default();
    let mut text = format!(
        "OFFLINE DEMO · ngn {}\n\nThe harness, tools, TUI and web server are running in Rust. No Python runtime, network or model request was used.\n\nWorkspace files:\n{listing}\n\nTry `demo approval` for a no-write approval preview or `demo subagents` for Rust-supervised background agents.\n",
        env!("CARGO_PKG_VERSION")
    );
    if prompt.starts_with("demo subagents") {
        let waiting = prompt.contains("wait");
        let mut tasks = Vec::new();
        for index in 0..if waiting { 1 } else { 2 } {
            let delegated = host.tool(run, ToolCall { call_id:format!("demo-delegate-{index}"), name:"to_agent".into(), arguments:json!({"prompt":if waiting { "demo hold" } else { "demo child" }, "title":format!("Workspace reviewer {}", index + 1), "read_only":true, "background":true}) }).await?;
            if let Some(id) = delegated.result["task"]["run_id"].as_str() {
                tasks.push(id.to_owned());
            } else {
                text.push_str(&format!("\nDelegation denied: {}\n", delegated.error));
            }
        }
        for (index, id) in tasks.iter().enumerate() {
            loop {
                let outcome = host
                    .tool(
                        run,
                        ToolCall {
                            call_id: format!("demo-wait-{index}"),
                            name: "agent_wait".into(),
                            arguments: json!({"run_id":id, "timeout_seconds":60}),
                        },
                    )
                    .await?;
                if outcome.result["task"]["status"] == "running" {
                    continue;
                }
                text.push_str(&format!(
                    "\nRust-supervised subagent {}: {}\n",
                    id,
                    outcome.result["task"]["status"]
                        .as_str()
                        .unwrap_or("failed")
                ));
                break;
            }
        }
    }
    if prompt == "demo nested" {
        let outcome = host.tool(run, ToolCall { call_id:"demo-nested".into(), name:"to_agent".into(), arguments:json!({"prompt":"demo subagents", "title":"Nested coordinator", "read_only":true}) }).await?;
        text.push_str(&format!(
            "\nNested supervisor result: {}\n",
            outcome.result["task"]["status"]
                .as_str()
                .unwrap_or("denied")
        ));
    }
    if prompt == "demo orphan" {
        host.tool(run, ToolCall { call_id:"demo-unjoined".into(), name:"to_agent".into(), arguments:json!({"prompt":"demo hold", "title":"Unjoined demo child", "background":true}) }).await?;
    }
    if prompt.to_lowercase().contains("demo approval") {
        let result = host
            .tool(
                run,
                ToolCall {
                    call_id: "demo-preview".into(),
                    name: "demo_preview".into(),
                    arguments: json!({}),
                },
            )
            .await?;
        text.push_str(if result.error.is_empty() {
            "\nPreview allowed. No file was written.\n"
        } else {
            "\nPreview denied. No file was written.\n"
        });
    }
    let chars: Vec<char> = text.chars().collect();
    for chunk in chars.chunks(48) {
        if cancel.is_cancelled() {
            return Ok(RunOutput::cancelled());
        }
        host.emit(
            run,
            Event::TextDelta {
                text: chunk.iter().collect(),
            },
        )
        .await?;
        tokio::time::sleep(Duration::from_millis(12)).await;
    }
    host.emit(run, Event::TextCompleted { text: text.clone() })
        .await?;
    Ok(RunOutput {
        status: RunStatus::Completed,
        error: String::new(),
        text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn rejects_version_and_unbounded_lines() {
        let bytes = b"{\"protocol_version\":2,\"run_id\":\"\",\"message\":{\"type\":\"ready\",\"runtime\":\"fake\",\"capabilities\":[]}}\n";
        assert!(read_frame(&mut BufReader::new(&bytes[..])).await.is_err());
        let bytes = vec![b'x'; MAX_FRAME_BYTES + 1];
        assert!(read_frame(&mut BufReader::new(&bytes[..])).await.is_err());
    }

    #[test]
    fn subscription_requires_explicit_capability_before_start() {
        let old = vec!["host_tools".into(), "cancel".into()];
        assert!(validate_capabilities(&old, ProviderAuth::ApiKey).is_ok());
        let error = validate_capabilities(&old, ProviderAuth::Chatgpt)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("chatgpt_auth")
                && error.contains("0.28.5")
                && error.contains("source-built")
        );
        let mut modern = old;
        modern.push("chatgpt_auth".into());
        assert!(validate_capabilities(&modern, ProviderAuth::Chatgpt).is_ok());
        assert!(!steering_capability(&modern).unwrap());
        modern.push("safe_steering".into());
        assert!(validate_capabilities(&modern, ProviderAuth::Chatgpt).is_ok());
        assert!(steering_capability(&modern).unwrap());
        modern.retain(|capability| capability != "chatgpt_auth");
        assert!(validate_capabilities(&modern, ProviderAuth::Chatgpt).is_err());
        assert!(validate_capabilities(&modern, ProviderAuth::Codex).is_err());
        modern.push("codex_auth".into());
        assert!(validate_capabilities(&modern, ProviderAuth::Codex).is_ok());
        assert!(validate_capabilities(&[], ProviderAuth::Chatgpt).is_err());
    }

    #[test]
    fn host_acknowledges_only_advertised_steering_and_preserves_legacy_start_shape() {
        let baseline = json!({"session_id":"s", "prompt":"user", "system_prompt":"policy", "database":"/private/runtime.sqlite3",
            "provider":{"kind":"fixture","model":"fixture","api":"","base_url":"","api_key_env":"FIXTURE_KEY"}, "tools":[],"max_tool_rounds":10});
        for (optional, expected) in [
            (None, false),
            (Some("unknown_optional_capability"), false),
            (Some("safe_steering"), true),
        ] {
            let mut capabilities = vec!["host_tools".into(), "cancel".into()];
            capabilities.extend(optional.map(str::to_owned));
            let mut start: StartRun = serde_json::from_value(baseline.clone()).unwrap();
            start.steering = steering_capability(&capabilities).unwrap();
            assert_eq!(start.steering, expected);
            let mut wire = baseline.clone();
            if expected {
                wire["steering"] = json!(true);
            }
            assert_eq!(serde_json::to_value(start).unwrap(), wire);
        }
        assert!(steering_capability(&["safe_steering".into()]).is_err());
    }

    #[test]
    fn steering_handshake_rejects_forged_ack_and_out_of_boundary_work() {
        let boundary = Uuid::new_v4().to_string();
        assert!(SteeringHandshake::new(false).boundary(&boundary).is_err());
        let mut state = SteeringHandshake::new(true);
        assert!(state.boundary("not-a-uuid").is_err());
        state.boundary(&boundary).unwrap();
        assert!(state.boundary(&boundary).is_err());
        assert!(state.acknowledge(&boundary, "forged").is_err());
        state.offered = Some((boundary.clone(), "host-id".into()));
        assert!(
            state
                .validate(&RuntimeMessage::TextDelta {
                    text: "too early".into()
                })
                .is_err()
        );
        assert!(state.acknowledge(&boundary, "wrong-id").is_err());
        assert!(state.acknowledge("wrong-boundary", "host-id").is_err());
        state.acknowledge(&boundary, "host-id").unwrap();
        assert!(state.acknowledge(&boundary, "host-id").is_err());
        state.final_empty = true;
        assert!(
            state
                .validate(&RuntimeMessage::SteeringBoundary {
                    boundary_id: Uuid::new_v4().to_string(),
                    kind: SteeringBoundary::Final
                })
                .is_err()
        );
    }
}
