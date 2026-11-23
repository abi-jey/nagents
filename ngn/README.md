# ngn

`ngn` (pronounced *"engine"*) is the native **terminal and browser client** for
the **`nagents`** (pronounced *"N-agents"*) Python engine. There is one engine:
`nagents engine`, a local loopback server that owns models, tools, approvals,
sessions, queues and steering behind the versioned `/api/v1` contract. The Rust
binary is only a client: the Ratatui TUI, `run --json`, and `ngn serve`, which
serves the React workspace and forwards it to the same engine.

The Rust client lives under `ngn/` in the **same repository** as the Python
`nagents` library (`../src/nagents/`). See [ADR 0001](../docs/adr/0001-python-core-rust-tui.md)
and the [release notes](docs/releases/).

## Install and update

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh
ngn update --check && ngn update
```

Self-contained bundle for Linux x86_64 (glibc 2.39+) and macOS 11+ (Apple silicon or Intel); nothing else needs installing.
See [installation](docs/installation.md).

## Build and inspect from source

Requires Rust 1.93+, a C compiler for bundled SQLite, and Node.js (24 in CI) to
build the embedded web assets. Installed binaries don't need Node.js.

```sh
# From the repository root:
npm ci --prefix src/nagents/web-ui
cd ngn
cargo build -p ngn
./target/debug/ngn --version
./target/debug/ngn -C /path/to/workspace doctor
```

`doctor` prints the Python interpreter that runs the engine, then the engine's
own diagnostics. It makes no model requests.

The web client is at `http://127.0.0.1:8765`. Web serving is **loopback-only**;
use the exact printed address. It is a trusted local operator application, not a
multi-user service or sandbox. `ngn serve` checks the browser's host, origin and
token, then forwards requests and events to the engine.

## Where the engine runs

The managed bundle contains a pinned CPython with `nagents[engine]`; nothing
else is needed. From a source build, ngn uses, in order: `NGN_ENGINE_PYTHON`,
the bundled runtime, `$VIRTUAL_ENV/bin/python`, then `python3`. That Python
needs the engine extra:

```sh
python3 -m venv .venv-ngn
. .venv-ngn/bin/activate
python -m pip install -e '..[engine]'   # from the repository's ngn/ directory
cargo run -p ngn                         # TUI
cargo run -p ngn -- serve                # browser client
```

ngn starts one engine per workspace and shares it between terminals and
browsers; `--standalone` starts a private one that stops with the client. The
engine reads the normal ngn settings in `$XDG_CONFIG_HOME/ngn/` (normally
`~/.config/ngn/`): `providers.json`, per-workspace overlays, model bindings and
`config.json`. `--config FILE` selects an explicit trusted engine configuration;
`--provider-id`, `--model` and `--read-only` apply to the engine it starts.

`ngn auth login|status|logout` runs the engine's own sign-in in that Python;
see [ChatGPT device login](docs/chatgpt-auth.md).

## Controls

See the [native TUI UX review and roadmap](docs/ux-review.md) for the design
principles behind these controls and what is planned next.

The [UX review](docs/ux-review.md) explains the design principles and roadmap.

- In the terminal, **Enter** sends when idle and steers the active turn if supported.
  **Ctrl+Enter** explicitly submits to the next-turn FIFO (it can start immediately
  when idle and unpaused). A paused queue remains paused. **Shift+Enter** inserts a
  newline. See [terminal keyboard compatibility](docs/steering.md#native-tui).
- The draft is editable anywhere: **←/→** move by character, **Ctrl+←/→**
  (or Alt) by word, **Home/End** to the line's start or end (with Ctrl, the
  draft's), and **↑/↓** between lines of a multi-line draft. **Backspace**
  and **Delete** work at the cursor; with Ctrl or Alt they remove a word.
- Desktop browser **Enter** still sends or queues; **Shift+Enter** inserts a newline.
- On touch/coarse-pointer browsers, **Return inserts a newline**; tap Send/Queue
  (or use Ctrl/Meta+Enter) to submit. Mobile conversation controls remain visible;
  Send and Stop preserve composer focus and Stop retains an unsent draft.
- A new conversation opens with the version, the workspace and the first keys
  to know; the empty composer shows a hint that is never part of the draft.
- Assistant replies render Markdown: headings, emphasis, lists, quotes, links,
  rules and code fences. Copying a wrapped paragraph gives back one line.
- Tool calls show one header each: **▸**/**▾** for collapsed/expanded. A
  finished call's label turns green; failed, denied and cancelled calls are
  coloured and say so. Nothing is prefixed when a call finishes, so a header
  never shifts while you read it. A call waiting for you reads *waiting for
  approval*. Finished turns
  only get a line when something went wrong: **✗ Failed: reason**,
  **⊘ Stopped** or **⚠ Interrupted before the turn finished**. Provider errors
  say what happened (for example HTTP 429 after its retries) and what to do.
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
   the tree and the context panel, hidden until focused or expanded. With the panel focused,
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
- **PageUp/PageDown** scroll the conversation or approval preview. You can also
  use the mouse wheel in the conversation, or click the scrollbar track or drag
  its thumb when the conversation overflows.
- Click a tool header to expand or collapse its details. Drag within the conversation
  (including visible tool details and code) or the Message draft to select text. A drag
  from a tool header selects text instead of toggling it. Selection does not edit a draft.
  Everything else on screen (borders, the status rows, the approval panel,
  help and pickers) can be dragged too and copies as painted, one line per
  row without trailing padding.
- Release to copy the selection automatically. This sends an OSC 52 request
  that replaces the clipboard contents when the terminal accepts it.
  **Copy requested** does not confirm acceptance. For terminal-native selection,
  use your terminal's mouse override (often **Shift+drag**) and usual Copy shortcut.
- **Esc** clears the highlight without changing the clipboard. Selecting
  conversation text holds the current view; scroll to the bottom to follow
  new output again. If the selected text moves or changes before release,
  select it again.
- When a tool asks permission, press **y** (or **Enter**) to allow it once and **n** to deny it. **Ctrl+Y** and **Ctrl+D** still work. Keys pressed in the first moment after the prompt appears are ignored so typing cannot approve by accident.
- **Ctrl+C** or **Ctrl+Q** quits; **Esc** cancels active work without quitting.
- `/new`, `/sessions`, `/resume ID`, `/tasks`, `/queue [pause|resume|clear|retry|verify]`, `/steer TEXT`, `/steering`, `/help`, `/quit` are supported.
- `--resume ID`, `--continue`, `sessions`, and `doctor` work from the CLI.
- `run --json PROMPT` emits ordered, versioned NDJSON; mutations **fail closed**.

The terminal footer shows the current activity and selected provider/model and
uses the default terminal palette. When run without `--config`, the
native TUI honors `"animations": false` in the personal Python ngn
`config.json`: footer motion stops and the composer caret stays steady.
Use `--no-animations` to disable both for one invocation without changing the
saved setting. `TEXTUAL_ANIMATIONS=none` also disables both regardless of the
saved setting. Elapsed timers and input still work.

With terminal-default colors, on Unix the client asks the terminal for its
background and uses that color to tint the full width of user-message rows.
If the terminal does not report a background, the client leaves those rows
unpainted rather than guessing. Nonempty `NO_COLOR` disables the tint.

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
[active-run steering boundary](docs/steering.md): **Enter** steers a running turn in
the browser and TUI (**Ctrl+Enter** queues instead), TUI **`/steer TEXT`**, and
durable receipt inspection. It requires both the runtime's
`safe_steering` capability and the host's explicit acknowledgment. Inputs apply
only after a complete persisted tool batch or final response, never between tools
or by interrupting model generation. Idle Enter/Send and FIFO ordering are unchanged.
Pending/offered/applied/uncertain/undelivered receipts report persistence state,
not proof of model consumption. Stop/restart never replays steering or tool work.

## Earlier Rust host history

Releases up to 0.100.46 ran their own Rust host. The first time the engine
starts in a workspace, it copies that workspace's old history from
`$XDG_DATA_HOME/ngn-rust/workspaces/<hash>/host.sqlite3` (or
`~/.local/share/ngn-rust/...`) into read-only archived sessions. It reads the old
file through a private SQLite backup and never changes it. Archives can be read
but not continued. If the copy fails, the engine starts anyway and prints how to
retry with `nagents import-host --host-snapshot BACKUP`, which also imports a
backup taken from a non-default location.

## Boundaries

- The Python engine owns models, tools, approvals, session and event storage,
  FIFO queues, steering and subagents. Its state lives in the normal ngn data
  directory, with one private registration per workspace.
- The Rust client owns terminal rendering, keyboard and mouse input, and the
  browser gateway. It stores no sessions and runs no tools.
- Approved shell commands and project code run as your user: **this is not an
  OS sandbox**. See [security](docs/security.md).

## Local diagnostics

An engine started by ngn writes its own log to `logs/engine.log` in its state
directory (beside any wire logs), never to the terminal the TUI draws on. If the
engine exits before reporting readiness, ngn points to that log. Logs stay local;
nothing is uploaded.

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
compiler (install the frontend dependencies once with `npm ci --prefix src/nagents/web-ui`):

```sh
./ngn/dev                         # Launch the mouse-enabled TUI
./ngn/dev --config /path/to/engine-config.json serve --port 18768
./ngn/dev --test                  # Build, install and run the engine offline
./ngn/dev --published-core --test # Same, with the pinned published core
```

This works with **uncommitted changes**. It builds the optimized, locked Cargo
binary, builds a non-editable `nagents[engine]` wheel, installs a fresh virtual
environment, packs and extracts the binary, then launches its explicit path.
The engine never falls back to your installed Python `ngn` or an editable
import. Export any referenced API key securely; `.env` is not loaded.

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
# In an activated environment with ruff/mypy/pre-commit installed:
python -m unittest discover -s integrations/nagents/tests -v
# Root pre-commit includes both Python-core and Rust-client gates:
(cd .. && pre-commit run --all-files)
```

`scripts/python_engine_smoke.py` drives the real TUI (in a PTY) and the browser
workspace (in Chromium, desktop and touch) against a live engine and a loopback
fixture model, with no paid requests. Install `playwright` and `aiohttp` and run
`python -m playwright install chromium` first:

```sh
python ngn/scripts/python_engine_smoke.py --binary ngn/target/release/ngn \
  --output /path/to/evidence
# --check private|browser|restart|pty|tasks|import selects phases.
```

## Repository layout

| Path | Responsibility |
| --- | --- |
| `crates/ngn` | Clap CLI, Ratatui TUI, browser gateway, installer/updater |
| `crates/ngn-api` | Client generated from the engine's `/api/v1` contract |
| `crates/nagents-native` | Optional ABI3 discovery and diff helpers for the engine |
| `../src/nagents/web-ui` | Shared React workspace, built into the Rust binary |
| `integrations/nagents` | Compatibility marker that lets ngn 0.100.46 and earlier update |
| `docs` | Protocol, trust model, architecture and migration inventory |

MIT licensed. Rust development started independently, then its history was imported
under `ngn/` in the existing nagents repository.
