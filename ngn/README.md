# ngn · 0.100.1

A standalone **Rust harness, TUI and local web server**, with a versioned agent
runtime protocol. The optional **nagents** adapter runs the Python agent/model
loop; **all workspace tools and approvals execute in Rust**.

The original Rust baseline is tagged `v0.100.0`; `0.100.1` adds Rust-supervised
subagents and fixes successful tool-result delivery to the model. This is **not
yet full legacy feature parity**. See [migration status](docs/migration.md) and
[patch notes](docs/releases/0.100.1.md). No remote release is published automatically.

## Run without Python

Requires Rust 1.93+ and a C compiler for bundled SQLite. No Node build is needed.

```sh
cargo run -p ngn -- --demo -C /path/to/workspace
cargo run -p ngn -- --demo -C /path/to/workspace serve
cargo run -p ngn -- --demo run --json "hello"
cargo build --release -p ngn
./target/release/ngn --version
```

The offline demo uses native Rust tools and scripted text. It makes no model or
network requests, never writes workspace files, and does not launch Python. It
does persist conversations in its own data directory. Try **demo approval**,
**demo subagents**, or **demo subagents wait** (then inspect/cancel the child with F5).

The web client is at `http://127.0.0.1:8765`. Web serving is **loopback-only**;
use the exact printed address. It is a trusted local operator application, not a
multi-user service or sandbox.

## Use nagents as the agent runtime

Install the integration into a **separate environment**, not over an existing
Python ngn installation:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e integrations/nagents
# To use a local nagents checkout instead of the published library:
# python -m pip install -e ../nagents -e integrations/nagents

cp config.example.json config.local.json
# Edit its provider/model and environment-variable reference for your own account.
# Export the referenced API key securely in this process's environment.
cargo run -p ngn -- --config config.local.json
cargo run -p ngn -- --config config.local.json serve
```

`runner` is an argv array. Use an absolute environment interpreter path if the
host will run without activation, e.g. `['/path/to/.venv/bin/python', '-m',
'ngn_nagents']` (use JSON double quotes in configuration). No key value belongs in
the file, browser, URL or command line. `.env` is not loaded automatically.

The adapter uses public `Agent.run()`, `ToolRegistry.register()` and
`ToolExecutor.execute()` APIs. **No `nagents.harness`, Textual, FastAPI or Uvicorn
is imported.** The core agents library remains independently usable and unchanged.

## Controls

- **Enter** sends; **Shift+Enter** inserts a newline where the terminal supports it.
- **Esc** cancels active work. Completed tool actions are not rolled back.
- **F2** lists sessions, **F3** creates one, **F4** toggles tool details.
- **F5** opens the agent catalog; **Enter** inspects a child conversation and
  **Ctrl+X** cancels that child and its descendants, not its parent or siblings.
- **PageUp/PageDown** scroll the conversation or approval preview.
- **Ctrl+Y** allows the displayed exact call once; **Ctrl+D** denies it.
- **Ctrl+Q** quits and joins runners before restoring the terminal.
- `/new`, `/sessions`, `/resume ID`, `/tasks`, `/help`, `/quit` are supported.
- `--resume ID`, `--continue`, `sessions`, and `doctor` work from the CLI.
- `run --json PROMPT` emits ordered, versioned NDJSON; mutations **fail closed**.

The browser also shows agent status, independent child transcripts, child-only
cancellation, and correctly attributed child approvals in the root workspace.
Prompts submitted to a busy session are rejected, not silently
queued or replayed. Different sessions can run independently (eight root runs max).

## Rust-supervised subagents

`to_agent` starts a separate protocol-compatible runner with an isolated session.
The default is foreground delegation: its final public result returns through the
same tool call. With `background: true`, it returns a child `run_id` immediately;
the parent can do other work and later use `agent_status`, `agent_wait`, or
`agent_cancel`. Only the immediate owning run can receive/control that result.

Limits are shared by the whole root activation, including nested children:

```json
"subagents": {
  "max_depth": 2,
  "max_concurrent": 3,
  "max_executions": 8,
  "timeout_seconds": 300
}
```

This is a field inside the explicit JSON configuration. `max_depth: 0` disables
delegation. Saturated slots are rejected, not secretly queued. Completed/failed
executions do not refund the execution budget. Read-only permissions and
non-interactive approval denials cannot be relaxed by a child. Children inherit
the selected provider/runtime; profiles and model overrides are not implemented yet.

Cancelling a parent stops its subtree and joins it before publishing parent
completion. A model that finishes while background children are still active is
marked failed and those children are cancelled; use `agent_wait` before finishing.
Background results are **not** automatically injected into an unrelated model
generation. Child text/reasoning is not relayed as root assistant output.

The host database upgrades from schema 1 to 2 for immutable task topology and
recovery. Stop the host and back up its workspace state before upgrading important
data. `0.100.0` deliberately refuses schema 2; downgrading requires restoring that
backup. No legacy Python ngn data or runtime-owned database is migrated.

## Boundaries

- Rust owns admission, idempotency, run/session lifecycle, event persistence,
  filesystem/shell tools, approval decisions, TUI, HTTP and WebSocket transport.
- An agent runtime owns its model integration and opaque conversation storage.
- Private stdin/stdout carry [Agent Runtime Protocol v1](docs/agent-runtime-protocol.md).
  This interface can also be implemented by a native Rust or another-language runtime.
- Rust state defaults to `$XDG_DATA_HOME/ngn-rust/workspaces/<workspace-hash>/`,
  or `~/.local/share/ngn-rust/...`. It never implicitly opens legacy Python ngn data.
- Host and runtime databases are separate; no shared SQLite schema or Python objects.
- A per-workspace lock prevents a second host from recovering another live host's runs.

Native tools: `read_file`, `list_files`, literal `search`, `write`, literal `edit`,
and `shell`. Existing file changes require the exact SHA-256 from an inspected
file and are revalidated after approval. File tools are capability-relative,
bounded, and reject traversal, symlinks, hard links and common credential paths.
Approved shell commands and a trusted runtime have full access as your user:
**this is not an OS sandbox**. See [security](docs/security.md).

## Development

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
# In the adapter's activated environment, with ruff/mypy/pre-commit installed:
python -m unittest discover -s integrations/nagents/tests -v
python -m ruff check integrations/nagents
python -m mypy --strict integrations/nagents/src integrations/nagents/tests
pre-commit run --all-files
```

`scripts/smoke.py` performs real local CLI/HTTP/WebSocket/TUI and nagents-protocol
checks against a fixture model server, with no paid-provider requests. See its
`--help`. The integration must be importable in the selected Python environment.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `crates/ngn-protocol` | Agent wire types and client event schema; no UI dependency |
| `crates/ngn-core` | Rust harness lifecycle, durable journal, native tools, runtime supervision |
| `crates/ngn` | Clap CLI, Ratatui TUI, Axum HTTP/WebSocket host |
| `web` | Embedded, dependency-free initial browser client |
| `integrations/nagents` | Optional Python adapter using only the agents library |
| `docs` | Protocol, trust model, architecture and migration inventory |

MIT licensed. This repository was started independently from the Python nagents
working tree; existing Python work and history are not modified or removed.
