# ngn · 0.100.2 development

A standalone **Rust harness, TUI and local web server**, with a versioned agent
runtime protocol. The optional **nagents** adapter runs the Python agent/model
loop; **all workspace tools and approvals execute in Rust**.

The Rust application lives under `ngn/` in the **same repository** as the Python
`nagents` library (`../src/nagents/`). The first official monorepo release,
`v0.100.0`, includes the tested local prototype and its subagent/tool-result fixes.
The earlier standalone local tags/artifacts were never published and remain
untouched in their original checkout. This is **not yet full legacy feature
parity**. See [migration status](docs/migration.md) and [release notes](docs/releases/0.100.0.md).

`0.100.1` adds durable Rust-owned FIFO input queues with matching TUI/web controls.
See the [patch notes](docs/releases/0.100.1.md) before upgrading important state.

## Install and update (0.100.2 development)

The new [managed installer](docs/installation.md) bundles pinned CPython, the
matching core/adapter, dependency wheels and CA certificates. It needs no existing
Python, pip, Rust or Node installation. `ngn update --check` checks for a newer
stable harness; `ngn update` verifies/stages it and atomically switches versions
without changing credentials or conversations. Existing unrelated `ngn` commands
are never overwritten. Initial installer support is Linux x86_64/glibc 2.39+.
These bundled assets are not yet published; 0.100.1 is unchanged.

## Run without Python

Requires Rust 1.93+ and a C compiler for bundled SQLite. No Node build is needed.

```sh
# From the repository root:
cd ngn
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
# To use the Python core from this same repository:
# python -m pip install -e .. -e integrations/nagents

cp config.example.json config.local.json
# Edit its provider/model and environment-variable reference for your own account.
# Export the referenced API key securely in this process's environment.
cargo run -p ngn -- --config config.local.json
cargo run -p ngn -- --config config.local.json serve
```

The managed bundle automatically uses its embedded runtime when `runner` is
omitted, as in the examples. For a source/small-native installation, `runner` is
an argv array. Use an absolute environment interpreter path if the
host will run without activation, e.g. `['/path/to/.venv/bin/python', '-m',
'ngn_nagents']` (use JSON double quotes in configuration). No key value belongs in
the file, browser, URL or command line. `.env` is not loaded automatically.

The adapter uses public `Agent.run()`, `ToolRegistry.register()` and
`ToolExecutor.execute()` APIs. **No `nagents.harness`, Textual, FastAPI or Uvicorn
is imported.** The core agents library remains independently usable; public library
changes preserve the existing harness import paths rather than embedding that harness.

Development-only [ChatGPT device login](docs/chatgpt-auth.md) is an explicit
`kind: "openai", auth: "chatgpt"` connection with native `auth login|status|logout`
commands. It requires `nagents>=0.28.6,<0.29`; the official release is blocked until
that matching public core is published. The API is **not available in nagents
0.28.5**. Existing API-key connection semantics are unchanged.

## Controls

- **Enter** sends, or queues while busy/paused; **Shift+Enter** inserts a newline.
- On touch/coarse-pointer browsers, **Return inserts a newline**; tap Send/Queue
  (or use Ctrl/Meta+Enter) to submit. Mobile conversation controls remain visible;
  Send and Stop preserve composer focus and Stop retains an unsent draft.
- **Esc** cancels active work and pauses pending inputs. Completed actions are not rolled back.
- **F2** lists sessions, **F3** creates one, **F4** toggles tool details.
- **F5** opens the agent catalog; **Enter** inspects a child conversation and
  **Ctrl+X** cancels that child and its descendants, not its parent or siblings.
- **F6** inspects pending inputs; **Ctrl+P** pauses/resumes, **Ctrl+X** removes the
  selected input, **Ctrl+K** clears pending inputs and pauses. These do not cancel active work.
- **`/steer TEXT`** targets the capable active root at its next persisted safe point;
  **F7** or **`/steering`** inspects exact-run receipts, including while approval waits.
- **PageUp/PageDown** scroll the conversation or approval preview.
- **Ctrl+Y** allows the displayed exact call once; **Ctrl+D** denies it.
- **Ctrl+Q** quits and joins runners before restoring the terminal.
- `/new`, `/sessions`, `/resume ID`, `/tasks`, `/queue [pause|resume|clear]`, `/steer TEXT`, `/steering`, `/help`, `/quit` are supported.
- `--resume ID`, `--continue`, `sessions`, and `doctor` work from the CLI.
- `run --json PROMPT` emits ordered, versioned NDJSON; mutations **fail closed**.

The browser also shows agent status, independent child transcripts, child-only
cancellation, and correctly attributed child approvals in the root workspace.
The browser also exposes the pending FIFO, pause/resume, per-input removal and
clear-and-pause. Queues allow 32 pending inputs per session and 256 per host;
different sessions can run independently (eight roots and 64 total active runs).

Queued request IDs retain their identities after dispatch/cancellation; retrying
the same UUID and text cannot execute twice or upgrade its stored approval mode.
A completed turn dispatches the next input only after its children are cleaned up.
Stop/failure/shutdown/restart pause pending inputs. **Resume is explicit**; started
or interrupted work is never requeued/retried. `run --json` remains immediate and
fails rather than jumping a paused/pending queue. This is FIFO, not model steering.

Development 0.100.2 also has a separate, explicit
[active-run steering boundary](docs/steering.md): browser **Steer active turn**,
TUI **`/steer TEXT`**, and durable receipt inspection. It requires both the runtime's
`safe_steering` capability and the host's explicit acknowledgment. Inputs apply
only after a complete persisted tool batch or final response, never between tools
or by interrupting model generation. Existing Enter/Send/FIFO controls are unchanged.
Pending/offered/applied/uncertain/undelivered receipts report persistence state,
not proof of model consumption. Stop/restart never replays steering or tool work.

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

Host schema 3 added durable queues and session-scoped events; the development
steering boundary now upgrades host state to schema **4** for durable receipts.
Stop the host and back up its workspace state before upgrading important data.
Older schema-3 binaries cannot open schema 4; downgrade only using a separate
state directory or that backup, never by changing `user_version`. No legacy
Python ngn data or runtime-owned database is migrated.

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
List/search honor only workspace-root/nested `.gitignore` files, not global/user
Git configuration or `.git/info/exclude`. Ignored ancestors are checked even for
explicit search paths; negation cannot override protected/credential/build exclusions.
Rules use bounded capability-relative reads and a mature Git-ignore matcher. An
unsafe/unreadable/invalid ignore file fails discovery closed, without leaking its
contents or returning a misleading partial result. Discovery does not load project
instructions; nested instruction/skill execution is still a separate migration gap.
Approved shell commands and a trusted runtime have full access as your user:
**this is not an OS sandbox**. See [security](docs/security.md).

## Development

### Build and run exactly the installed application

From the repository root, on Linux x86_64 with Python 3.13, Rust 1.93+ and a C
compiler:

```sh
./ngn/dev                         # Rebuild, install wheels, launch offline TUI
./ngn/dev serve --port 18768      # Same installed binary, embedded browser UI
./ngn/dev --config /path/to/rust-config.json
./ngn/dev --config /path/to/rust-config.json serve --port 18768
./ngn/dev --test                  # Build + actual CLI/HTTP/WebSocket/PTY acceptance
./ngn/dev --published-core --test # Compatibility with the pinned published core
```

This works with **uncommitted changes**. It builds the optimized, locked Cargo
binary, builds non-editable Python core/adapter wheels, installs a fresh virtual
environment, packs and extracts the binary, then launches its explicit path.
The runtime never falls back to your installed Python `ngn` or an editable import.
The configured runner is rebound to that fresh environment; provider and permission
settings are retained. Export the referenced API key securely; `.env` is not loaded.
Without an explicit Rust `--config`, the command selects the offline demo.

Artifacts and state default to `$XDG_CACHE_HOME/ngn-rust-dev/<checkout-hash>/`
(or `~/.cache/...`). Development state is separate from installed/legacy state;
stop one host before opening another on the same development state. Pass
`--data-dir PATH` only when deliberately choosing another Rust state directory.
`NGN_DEV_CACHE` overrides the cache; `NGN_DEV_PYTHON` chooses the bootstrap
interpreter. A resolved `CARGO_TARGET_DIR` inside this checkout is reused directly
(including `<checkout>/ngn/target` in CI). Every external target base, including
the default `<cache>/target`, instead uses a child `ngn-dev-<checkout-hash>` bucket.
The hash identifies the canonical checkout path, so different worktrees sharing
an external override or `NGN_DEV_CACHE` do not share mutable Cargo artifacts.
Symlinks are resolved before selecting the bucket; relative `CARGO_TARGET_DIR`
and `NGN_DEV_CACHE` paths resolve from the launcher's working directory, not the
Cargo manifest directory. This is artifact isolation, not an OS sandbox.
Every missing cache ancestor is created with owner-only access, including on a
first launch under umask 0002. Existing writable ancestors are rejected with an
actionable error; the launcher does not chmod or repair existing directories or
symlinks. Choose a private cache or secure the existing path deliberately.
`--test` also launches the extracted offline/read-only app in its actual cache
ancestry with a separate candidate-owned workspace/state and no Python on PATH;
it never tests against an operator's configured state or contacts a live provider.

Rust and embedded web assets are rebuilt on every invocation; Python wheels and
the runtime environment are freshly built/installed. A source change during the
build aborts the candidate rather than mixing revisions.

“Latest” means the source currently checked out, including local edits. The command
does **not** silently fetch, switch branches, pull, publish, or update an existing
environment. Development artifacts are explicitly labelled as local candidates;
official publication still requires a clean commit, CI, matching tag and release
manifest. See [local-work migration](docs/local-work-migration.md).

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
# In the adapter's activated environment, with ruff/mypy/pre-commit installed:
python -m unittest discover -s integrations/nagents/tests -v
python -m ruff check integrations/nagents
python -m mypy --strict integrations/nagents/src integrations/nagents/tests
# Root pre-commit includes both Python-core and Rust-harness gates:
(cd .. && pre-commit run --all-files)
```

`scripts/smoke.py` performs real local CLI/HTTP/WebSocket/TUI and nagents-protocol
checks against a fixture model server, with no paid-provider requests. See its
`--help`. The integration must be importable in the selected Python environment.

For optional real-browser acceptance, install `playwright` and `aiohttp` into a
separate testing venv and provide Chromium (or run `python -m playwright install
chromium`). Reuse the candidate path printed by `./ngn/dev --test`:

```sh
python ngn/scripts/browser_smoke.py --binary CANDIDATE/app/ngn \
  --runtime-python CANDIDATE/runtime/bin/python --output /path/to/evidence
# Add --browser /path/to/chromium to use an existing browser executable.
```

This runs the actual embedded client in Chromium at desktop and touch viewport
sizes against an owned local fixture. It tests Return/focus, conversation creation,
native approvals, long preview scrolling and child attribution without paid requests
or legacy state. It is separate from the dependency-free DOM regression tests.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `crates/ngn-protocol` | Agent wire types and client event schema; no UI dependency |
| `crates/ngn-core` | Rust harness lifecycle, durable journal, native tools, runtime supervision |
| `crates/ngn` | Clap CLI, Ratatui TUI, Axum HTTP/WebSocket host |
| `web` | Embedded, dependency-free initial browser client |
| `integrations/nagents` | Optional Python adapter using only the agents library |
| `docs` | Protocol, trust model, architecture and migration inventory |

MIT licensed. Rust development started independently, then its history was imported
under `ngn/` in the existing nagents repository. The Python core, legacy clients,
existing history and installed defaults are retained.
