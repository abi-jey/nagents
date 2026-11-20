# ngn

A standalone **Rust harness, TUI and local web server**, with a versioned agent
runtime protocol. The optional **nagents** adapter runs the Python agent/model
loop; **all workspace tools and approvals execute in Rust**.

The Rust application lives under `ngn/` in the **same repository** as the Python
`nagents` library (`../src/nagents/`). The first official monorepo release,
`v0.100.0`, includes the tested local prototype and its subagent/tool-result fixes.
The earlier standalone local tags/artifacts were never published and remain
untouched in their original checkout. This is **not yet full legacy feature
parity**. See [migration status](docs/migration.md) and [release notes](docs/releases/).

`0.100.1` adds durable Rust-owned FIFO input queues with matching TUI/web controls.
See the [patch notes](docs/releases/0.100.1.md) before upgrading important state.

## Install and update

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh
ngn update --check && ngn update
```

Self-contained bundle for Linux x86_64 (glibc 2.39+) and macOS 11+ (Apple silicon or Intel); nothing else needs installing.
See [installation](docs/installation.md).

## Build and inspect from source

Requires Rust 1.93+ and a C compiler for bundled SQLite. No Node build is needed.

```sh
# From the repository root:
cd ngn
cargo build -p ngn
./target/debug/ngn --version
./target/debug/ngn -C /path/to/workspace doctor
```

`doctor` reports configuration and runtime paths without launching Python or
making model requests. Configure a provider and runtime before submitting prompts.

The web client is at `http://127.0.0.1:8765`. Web serving is **loopback-only**;
use the exact printed address. It is a trusted local operator application, not a
multi-user service or sandbox.

## Source/small-native integration only

The managed bundle already contains its runtime: **skip this Python setup** for
that installation. For a source build or an older small native archive, install
the integration into a **new environment**, never over an existing Python ngn:

```sh
# From the repository's ngn/ directory; choose another fresh name if it exists.
if test -e .venv-ngn-runtime || test -L .venv-ngn-runtime; then
  echo 'Choose a new environment path; do not overwrite an existing venv.' >&2
  exit 1
fi
python3 -m venv .venv-ngn-runtime
. .venv-ngn-runtime/bin/activate
python -m pip install -e .. -e integrations/nagents

cp config.example.json config.local.json
# Edit its provider/model and environment-variable reference for your own account.
# Export the referenced API key securely in this process's environment.
cargo run -p ngn -- --config config.local.json
cargo run -p ngn -- --config config.local.json serve
```

Without `--config`, ngn reads the existing Python ngn provider settings:
`$XDG_CONFIG_HOME/ngn/` (normally `~/.config/ngn/`), including `providers.json`,
its per-workspace overlay, `assistant.md` model bindings, and `config.json`'s
model and `read_only` settings. These files are only read; no new config file is
needed. `NGN_READ_ONLY=true` (or `1`) and `--read-only` can also tighten native
permissions; other Python approval or tool settings are not imported.
Pass `--config FILE` to use an explicit Rust configuration instead.
Custom Python provider-registry paths are not used implicitly; select the
intended account with an explicit native configuration.
For OpenAI `auth: "auto"`, a saved Python ChatGPT login takes precedence over
local Codex configuration, then the named API-key environment variable. Rust
stops instead of copying the Python login or substituting a different account:
select an explicit native ChatGPT config and sign in to the intended account.
`auth: "codex"` requires local Codex files and uses their configured OAuth or
API-key policy. `ngn doctor` shows the selected connection or why it cannot run.

The managed bundle automatically uses its embedded runtime when `runner` is
omitted, as in the examples. For a source/small-native installation, `runner` is
an argv array. Use an absolute environment interpreter path if the
host will run without activation, e.g. `['/path/to/.venv-ngn-runtime/bin/python', '-I', '-B', '-m',
'ngn_nagents']` (use JSON double quotes in configuration). No key value belongs in
the file, browser, URL or command line. `.env` is not loaded automatically.

The adapter uses public `Agent.run()`, `ToolRegistry.register()` and
`ToolExecutor.execute()` APIs. **No `nagents.harness`, Textual, FastAPI or Uvicorn
is imported.** The core agents library remains independently usable; public library
changes preserve the existing harness import paths rather than embedding that harness.

[ChatGPT device login](docs/chatgpt-auth.md) is an explicit
`kind: "openai", auth: "chatgpt"` connection with native `auth login|status|logout`
commands. It requires the matching `nagents` core version; official bundles require that
matching published core. The API is **not available in nagents
0.28.5**. Existing API-key connection semantics are unchanged.

## Controls

- In the terminal, **Enter** sends when idle and steers the active turn if supported.
  **Ctrl+Enter** explicitly submits to the next-turn FIFO (it can start immediately
  when idle and unpaused). A paused queue remains paused. **Shift+Enter** inserts a
  newline. See [terminal keyboard compatibility](docs/steering.md#native-tui).
- Desktop browser **Enter** still sends or queues; **Shift+Enter** inserts a newline.
- On touch/coarse-pointer browsers, **Return inserts a newline**; tap Send/Queue
  (or use Ctrl/Meta+Enter) to submit. Mobile conversation controls remain visible;
  Send and Stop preserve composer focus and Stop retains an unsent draft.
- In the terminal, type **`/`** to discover native commands. **↑/↓** selects;
  **Tab** or **Enter** fills the command without running it. Press **Enter**
  again to run it, or type any arguments first. **Esc** dismisses suggestions
  without changing the draft. Plugins and skills aren't listed.
- Outside suggestions and menus, **Esc** cancels active work and pauses pending
  inputs. Completed actions are not rolled back.
- **F1** or **`/help`** shows shortcuts and commands. **F2**, **`/sessions`**,
  or bare **`/resume`** opens the session picker. Type to filter by title or ID,
  use **↑/↓** and **Enter** to open a session or choose **+ New session**,
  or press **Esc** to close without changing the draft or scroll position.
  **`/resume ID`** opens an exact ID directly. **F3** creates a session and
  **F4** toggles tool details.
- Long assistant code fences clip rather than wrap. Unlike Python Textual's
  scrollable fence widgets, the native conversation uses **F8** to focus an
  overflowing block without changing Tab or composer controls. Use **←/→** to
  pan, **Home/End** to jump to an edge, **↑/↓** to select another overflowing
  fence, and **Esc** or **F8** to return to the draft. The read-only agent
  inspector clips at the left edge and does not offer code focus.
- **F5** opens the agent catalog; **Enter** inspects a child conversation and
  **Ctrl+X** cancels that child and its descendants, not its parent or siblings.
- Above 110 columns, the Agents sidebar stays beside the conversation.
  **Ctrl+T** focuses it (or opens the catalog at narrower widths). Use **↑/↓**
  or **PageUp/PageDown** to move, **Space** or **←/→** to fold/unfold a branch,
  and **Enter** to inspect the selected run. **Esc** returns to the draft from
  the sidebar or an inspection opened from it. **Tab/Shift+Tab** moves between
  the tree and **Workspace & context**, collapsed by default. With the disclosure focused,
  **Enter/Space** toggles it and **↑/↓**, **PageUp/PageDown**, or **Home/End**
  scroll its details.
- **F6** inspects pending inputs; **Ctrl+P** pauses/resumes, **Ctrl+X** removes the
  selected input, **Ctrl+K** clears pending inputs and pauses. These do not cancel active work.
- If an input was accepted but dispatch failed, the composer clears and shows
  an acceptance warning. Inspect the queue instead of resending. A matching
  durable receipt confirms admission even if its saved run status is unreadable.
  The native composer protects up to 16 unconfirmed requests without blocking
  unrelated drafts or replacing an unresolved request. In the original
  conversation, **`/queue verify [REQUEST_ID]`** checks the exact receipt without
  sending anything, and releases the binding only on confirmed admission or
  absence. **`/queue retry [REQUEST_ID]`** restores its text without sending it;
  press **Ctrl+Enter** (or **Enter** while idle) to retry the original UUID. With no ID, these commands
  target the newest unconfirmed request in the conversation. This retry state
  lasts only while the TUI is open; after exit, inspect the conversation and
  queue before resending.
- Active-turn **Enter** and **`/steer TEXT`** target the capable active root at its next persisted safe point;
  **F7** or **`/steering`** inspects exact-run receipts, including while approval waits.
- **PageUp/PageDown** scroll the conversation or approval preview.
- The mouse wheel scrolls the main conversation when the pointer is over it,
  without moving focus. Hover a tool header to highlight it; click to toggle
  its details. Drag over transcript text to select visible rows, then press
  **Ctrl+C** to copy in terminals that accept clipboard writes (OSC 52).
  Dialogs and focused Agents controls take precedence. For selection outside the
  transcript, or if clipboard writes are disabled, use your terminal's own
  selection override (often **Shift** while dragging).
- **Ctrl+Y** allows the displayed exact call once; **Ctrl+D** denies it.
- **Ctrl+Q** quits and joins runners before restoring the terminal.
- `/new`, `/sessions`, `/resume ID`, `/tasks`, `/queue [pause|resume|clear|retry|verify]`, `/steer TEXT`, `/steering`, `/help`, `/quit` are supported.
- `--resume ID`, `--continue`, `sessions`, and `doctor` work from the CLI.
- `run --json PROMPT` emits ordered, versioned NDJSON; mutations **fail closed**.

The terminal footer shows the current activity and selected provider/model and
uses the legacy default terminal palette. When run without `--config`, the
native TUI honors `"animations": false` in the personal Python ngn
`config.json`: footer motion stops and the composer caret stays steady.
Use `--no-animations` to disable both for one invocation without changing the
saved setting. `TEXTUAL_ANIMATIONS=none` also disables both regardless of the
saved setting. Elapsed timers and input still work.

Without `--config`, the native Message editor, caret, and user transcript bubbles
honor saved painted Python themes: `graphite`, `ocean`, and `ember` with
`theme_background` set to `auto` (the default) or `theme`, and `terminal` with
`theme_background: "theme"`.
The default, a `terminal` background override, unsupported values, and
nonempty `NO_COLOR` keep user transcript rows unshaded and the editor on
terminal-default colors with a reverse caret. The native client does not query
terminal colors, so it cannot match the Python bubble shade in terminal-native mode.
The rest of the UI is not themed by this setting.

The Agents sidebar shows native run status, the displayed child approval, and
read-only permissions, not Python model activity phases. Its context panel shows
workspace, session, provider/model and configured authentication mode, not Python
profiles, plugins, project trust, credentials or credential health.

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

ngn also has a separate, explicit
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
state directory or that backup, never by changing `user_version`. Runtime-owned
conversation data is not migrated. Native schema 5 adds read-only Python archives.

### Python session history

On Linux and macOS, normal native startup (`ngn`, `run`, `serve`, or `sessions`)
tries to copy supported Python transcripts for the selected workspace once into
its Rust store. It uses the existing Python data path: `$XDG_DATA_HOME/ngn` or
`~/.local/share/ngn`, overridden by `NGN_DATA_DIR` and then by `data_dir` in the
personal Python `config.json`. Relative paths use the invocation directory for
the environment variable and the personal config directory for the file.
Project or explicitly selected Python config files are not used for discovery.
Native `--data-dir` selects the destination, which must be separate from the
Python data root and its workspace database directory.

Archives are labeled in the picker and contain plain user/assistant text only.
They cannot run prompts, resume model context, replay actions, or grant approvals.
Existing native sessions and archives are not replaced; the picker lists native
sessions first, within its existing 200-item limit. Later Python edits are not
synced. `--continue` selects a native session, never an archive.

The live Python database is read only at the filesystem level, with a nonblocking
lock; SQLite only opens the private copy. The source must be a user-owned regular
file of at most 128 MiB, with no sidecars, and its resolved path must not be
writable by other users. A missing database is ignored. A busy, unsafe,
unsupported, or WAL database is skipped without stopping native startup.
Close Python ngn and retry. If needed, make a standalone rollback-journal backup
(for example with SQLite `VACUUM INTO`) and run
`ngn -C WORKSPACE upgrade --from BACKUP --data-dir FRESH_DIR` to inspect it in
separate fresh native state. Diagnostics, help, auth, install and update do not
attempt the automatic copy.

## Boundaries

- Rust owns admission, idempotency, run/session lifecycle, event persistence,
  filesystem/shell tools, approval decisions, TUI, HTTP and WebSocket transport.
- An agent runtime owns its model integration and opaque conversation storage.
- Private stdin/stdout carry [Agent Runtime Protocol v1](docs/agent-runtime-protocol.md).
  This interface can also be implemented by a native Rust or another-language runtime.
- Rust state defaults to `$XDG_DATA_HOME/ngn-rust/workspaces/<workspace-hash>/`,
  or `~/.local/share/ngn-rust/...`. It never opens Python ngn's database as a live store.
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

## Local diagnostics

The TUI prints its runtime log path on exit. Rust hosts write lifecycle metadata
under the effective data directory (`--data-dir`, if passed), in
`logs/tui-<unique>.jsonl`. This log does not contain prompts, tool payloads,
provider credentials, or runner stderr. Logs stay local; nothing is uploaded.
If the runtime log cannot be written safely, the host continues normally and the
TUI reports it as unavailable. Explicit wire capture fails if its private log
cannot be set up.

Provider HTTP payload capture is **off by default**. Enable it for one invocation
with `--wire-log` or use `NGN_WIRE_LOG=true|false|1|0` as a default. Use
`--no-wire-log` to override the environment. Flags work before or after the
subcommand; the last flag wins. This requires POSIX
file protections and a compatible runtime. The separate `logs/wire-<unique>.jsonl`
file has owner-only permissions, and the TUI prints its path when enabled.
Transport authentication and unknown headers are redacted, and diagnostic URLs
omit user credentials, queries, and fragments. Payloads can still contain private
prompts, model output, tool data and other sensitive content. Inspect the file
before sharing it and delete it when no longer needed; there is no automatic
rotation or retention cleanup.

## Development

### Build and run exactly the installed application

From the repository root, on Linux x86_64 with Python 3.13, Rust 1.93+ and a C
compiler:

```sh
./ngn/dev                                  # Launch with existing provider settings
./ngn/dev --config /path/to/rust-config.json  # Or use an explicit Rust config
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
`--test` uses candidate-owned workspace and state rather than the operator's
configuration, and does not contact a live provider.

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
