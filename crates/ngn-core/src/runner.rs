use std::{collections::HashSet, process::Stdio, sync::Arc, time::Duration};

use anyhow::{Context, Result, bail, ensure};
use ngn_protocol::{
    Event, HostFrame, HostMessage, MAX_FRAME_BYTES, RunStatus, RuntimeFrame, RuntimeMessage,
    StartRun, ToolCall, VERSION,
};
use serde_json::json;
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::Command,
};
use tokio_util::sync::CancellationToken;

use crate::{
    host::{ApprovalMode, Host},
    store::Run,
    tools::ProcessGroup,
};

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

pub async fn stdio(
    host: &Arc<Host>,
    run: &Run,
    prompt: &str,
    approval: ApprovalMode,
    cancel: CancellationToken,
) -> Result<(RunStatus, String)> {
    let mut command = Command::new(&host.config.runner[0]);
    command
        .args(&host.config.runner[1..])
        .current_dir(&host.config.workspace)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(unix)]
    command.process_group(0);
    let mut child = command.spawn().context("Cannot start agent runner; install integrations/nagents into the runner's Python environment")?;
    let _group = ProcessGroup(child.id());
    let mut writer = child.stdin.take().context("No runner stdin")?;
    let mut reader = BufReader::new(child.stdout.take().context("No runner stdout")?);
    let ready = tokio::select! {
        ready = tokio::time::timeout(Duration::from_secs(10), read_frame(&mut reader)) => ready.context("Agent runner handshake timed out")??.context("Agent runner exited before ready; verify `python -m ngn_nagents` is installed in the configured runner environment")?,
        _ = cancel.cancelled() => return Ok((RunStatus::Cancelled, String::new())),
    };
    ensure!(
        ready.run_id.is_empty(),
        "Ready frame must not bind an unaccepted run"
    );
    match ready.message {
        RuntimeMessage::Ready { capabilities, .. } => ensure!(
            ["host_tools", "cancel"]
                .iter()
                .all(|required| capabilities.iter().any(|capability| capability == required)),
            "Runtime lacks required host_tools/cancel capabilities"
        ),
        _ => bail!("Expected runtime ready handshake"),
    }
    let mut system_prompt = host.config.system_prompt.clone();
    // Root instructions are plain data, read with the same native path policy.
    if host.config.workspace.join("AGENTS.md").exists() {
        let call = ToolCall {
            call_id: "host-instructions".into(),
            name: "read_file".into(),
            arguments: json!({"path":"AGENTS.md", "limit":1000}),
        };
        let tools = host.tools.clone();
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
        provider: host.config.selected_provider()?.clone(),
        tools: host.tools.definitions(),
        max_tool_rounds: host.config.max_tool_rounds,
    };
    send(&mut writer, run, HostMessage::Start { run: start }).await?;
    let mut calls = HashSet::new();
    let mut frames = 0;
    let mut total_text = 0;
    let terminal = loop {
        let frame = tokio::select! {
            frame = read_frame(&mut reader) => frame?.context("Agent runner exited without a terminal result; actions are not retried")?,
            _ = cancel.cancelled() => {
                let _ = send(&mut writer, run, HostMessage::Cancel).await;
                if tokio::time::timeout(Duration::from_secs(3), child.wait()).await.is_err() { child.kill().await.ok(); child.wait().await.ok(); }
                return Ok((RunStatus::Cancelled, String::new()));
            }
        };
        ensure!(frame.run_id == run.id, "Runtime frame has the wrong run_id");
        frames += 1;
        ensure!(frames <= 100_000, "Runtime event budget exceeded");
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
                let result = host.tool(run, call, approval, cancel.clone()).await?;
                send(&mut writer, run, HostMessage::ToolResult { result }).await?;
            }
            RuntimeMessage::Notice { text } => {
                ensure!(text.len() <= 32_000, "Runtime notice exceeds limit");
                host.emit(run, Event::Notice { text }).await?;
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
    Ok(terminal)
}

pub async fn demo(
    host: &Arc<Host>,
    run: &Run,
    prompt: &str,
    approval: ApprovalMode,
    cancel: CancellationToken,
) -> Result<(RunStatus, String)> {
    let result = host
        .tool(
            run,
            ToolCall {
                call_id: "demo-list".into(),
                name: "list_files".into(),
                arguments: json!({"limit":12}),
            },
            approval,
            cancel.clone(),
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
        "OFFLINE DEMO · ngn 0.100.0\n\nThe harness, tools, TUI and web server are running in Rust. No Python runtime, network or model request was used.\n\nWorkspace files:\n{listing}\n\nTry `demo approval` to inspect a no-write approval preview. For a live agent, install the nagents runtime adapter and select a provider in an explicit Rust configuration.\n"
    );
    if prompt.to_lowercase().contains("demo approval") {
        let result = host
            .tool(
                run,
                ToolCall {
                    call_id: "demo-preview".into(),
                    name: "demo_preview".into(),
                    arguments: json!({}),
                },
                approval,
                cancel.clone(),
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
            return Ok((RunStatus::Cancelled, String::new()));
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
    host.emit(run, Event::TextCompleted { text }).await?;
    Ok((RunStatus::Completed, String::new()))
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
}
