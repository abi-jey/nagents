# 0001. Python core, Rust TUI client

- Status: Accepted; implemented in ngn 0.100.47
- Date: 2026-11-22

## Context

### What runs where today

```text
ngn (one Rust process)                                     python -m ngn_nagents (one per run)
├── TUI (ratatui)  ──in-process calls──┐                   ├── nagents Agent: the model loop
├── web.rs (axum)  ──in-process calls──┤                   ├── providers: OpenAI, Anthropic, Gemini (HTTP)
└── Host (ngn-core) ◀──────────────────┘                   └── HostExecutor: sends every tool call back
    ├── store.rs, queues.rs: host.sqlite3
    ├── runner.rs: starts the runner per run  ──stdio JSON──▶
    ├── tools.rs, tools/: executes every tool call  ◀── tool_request / tool_result ──
    ├── steering.rs, subagents.rs, auth.rs
    └── upgrade.rs, legacy.rs, bundle.rs

nagents serve (a separate Python product: FastAPI)
├── src/nagents/web: ~22k lines, the React web UI's API
└── src/nagents/harness: ~16k lines, its own runtime, tools, subagents, providers and sessions
```

The engine is split across two languages. Rust admits runs, stores them, queues and steers them, executes tools
and delegates to subagents. Python runs the model loop and calls the providers. Every tool call crosses the
process boundary twice. Meanwhile the Python harness and web server are a second, more complete engine that does
the same things on its own.

### The cost

| Part | Lines | Problem |
| --- | --- | --- |
| `ngn/crates` (`ngn`, `ngn-core`, `ngn-protocol`) | ~30k | The TUI is welded to half an engine through ~25 in-process `Host` calls |
| `ngn/integrations/nagents` | ~1.2k | Hand-written bridge, one Python process started per run |
| `src/nagents/harness` | ~16k | A second runtime: tools, subagents, sessions, providers |
| `src/nagents/web` | ~22k | A second server; FastAPI, ~70 routes |
| `ngn/scripts` release and bundle code | ~8k | Ships CPython inside the Rust bundle and checks its integrity |

- **Every feature lands twice or drifts.** Subagents, tool execution, sessions, queues, steering, approvals and
  the web server each have a Rust and a Python implementation.
- **Two stores.** `host.sqlite3` (Rust: sessions, runs, events) and `nagents.sqlite3` (Python: conversation
  history) describe the same sessions.
- **The protocol is written twice by hand:** `ngn-protocol` in Rust and the dictionaries in `ngn_nagents`.
- **A Python process per run** costs startup time and is where packaging failures surface (the Python 3.14
  crash, the bundle-integrity failures behind `ngn update`).

### What we want

Python for the core, because its interfaces are clean and `nagents` already speaks every provider. Rust for the
TUI, because terminal rendering must be fast. Rust also for the few tools that do many operations, where Python
would be the bottleneck.

### Prior art

**opencode v2** (`anomalyco/opencode`, `dev` branch) is making the same separation, with a TypeScript core and a
Rust TUI client (`packages/cli-rs`, binary `opencode-native`, not shipped yet):

- Packages and their allowed dependencies: `schema` → `core` and `protocol` → `server`. Clients depend on
  `schema` and `protocol` only, never on `core` or `server`, and boundary tests enforce the import graph.
- The client is **generated** from the server's API definition and committed; `check:generated` fails on drift.
  The Rust client's generated types tolerate a newer server: unknown fields are ignored, enums carry an
  `Unknown(String)` variant, and absent and `null` stay distinct.
- The client **finds or starts** the server: `--server <url>`; `--standalone` (a private server whose stdin is its
  lease); a registered background service; or it starts one. It never stops a server it did not start.
- One **event stream** feeds a client-side store. Deltas are batched and merged, the queue is bounded, and when
  it overflows the client reconnects and resynchronises. The store records what each flush changed, so a view
  redraws only that.
- **Durable sessions:** one step is one LLM call, continued from reloaded history, with no in-memory tool loop.
  Prompts enter a durable inbox before they run, which makes steering and restart recovery explicit.
- **Native work is a helper, not a rewrite:** the core runs a pinned, checksummed ripgrep, and terminals are
  owned by a Rust daemon (`process-daemon`) over an authenticated local socket.
- New endpoints start under `/api/experimental/` and graduate deliberately.

**Codex** (OpenAI) moved its agent loop out of the TUI into "Codex core", hosted by a long-lived app server that
speaks JSON-RPC over stdio. The TUI and IDE extensions are clients of it.

## Decision

### 1. `nagents` is the engine

The Python core owns everything that is not drawing: the model loop, providers and auth, sessions and their one
store, the run queue and steering, tool registry and execution, the approval policy, subagents, compaction, MCP,
skills, A2A and live audio. It runs as **one long-lived process** that serves any number of clients.

It is built from what already exists: the runtime in `src/nagents/harness` and the FastAPI app in
`src/nagents/web`, run headless as `nagents engine`. The engine serves the API and event stream only; serving the
web UI to browsers is `ngn serve`'s job (below).

### 2. `ngn` is a client

The Rust binary owns the terminal: rendering, input, mouse, selection and copy, themes, the keymap and
client-local preferences. It holds a copy of the engine's state, fed by the event stream, and **no agent logic**.
It finds or starts the engine, and it can attach to a remote one with `--engine <url>`.

`ngn serve` stays as the one command for the browser. It starts the main engine (or attaches to the registered
one), then serves the rest: the bundled web UI's static files, and the same-origin `/api` and event WebSocket,
which it forwards to the engine with the engine's token so the browser never holds it. It keeps today's browser
security (same-origin checks, `no-store`, `X-Frame-Options: DENY`). It holds no agent logic, and stopping it
stops the engine only if `ngn serve` started that engine.

### 3. One contract, defined in Python, generated for Rust

- `nagents.protocol` holds pydantic models for every request, response and event. It depends on pydantic only.
- The engine's API is versioned under `/api/v1`. FastAPI's OpenAPI document plus the event models' JSON Schema
  are the published contract, exported by a script and committed.
- A generator writes the `ngn-api` Rust crate from that contract: types, one method per endpoint, and the event
  enum. Generated code is committed, and pre-commit and CI fail when it is stale.
- Generated types tolerate a newer engine, as opencode's do: unknown fields ignored, `Unknown` variants on enums
  and unions, `Option<Option<T>>` where absent and `null` differ.
- The client checks the engine's protocol version at connect and refuses an incompatible major version with a
  message that names both versions.
- New endpoints start under `/api/v1/experimental/`.

### 4. Transport

- Requests are JSON over HTTP on `127.0.0.1`, with a random bearer token per engine.
- Events are one WebSocket per client, keeping the semantics `ngn.events.v1` already has in `web.rs`: subscribe
  before replay, a monotonically increasing sequence number per event, and resume with `?after=<sequence>`.
  Durable events replay from the store; transient deltas are only live.
- The engine registers itself (URL, token, pid, protocol version) in a private file in the state directory.
  `ngn --standalone` instead starts a private engine over stdio whose stdin is the lease: it exits when `ngn`
  does.

### 5. Tools run in the engine; heavy tools are native

- Tool execution moves into the engine. Approval stays with the user: the engine emits `approval.requested`,
  every attached client may show it, and the first decision posted to `/api/v1/approvals/{id}` wins. The engine
  times a call only while it executes, never while it waits for a human (as `timing.rs` does today).
- A tool is written in Rust when it does **many operations**: it walks or searches many files, diffs or patches
  large inputs, or holds long-lived OS resources such as terminals. Everything else stays in Python.
- Rust tools ship in a `nagents-native` crate built with PyO3 and maturin as an `abi3` wheel, called in process
  without IPC. Terminals, when added, get a Rust helper process owned by the engine.
- The first native tools are the ones Rust already has: workspace discovery with gitignore matching
  (`tools/discovery.rs`) and diff previews (`tools/diff.rs`), followed by search and large-file reads.

### 6. One store

The engine owns one SQLite database. Existing `host.sqlite3` data is imported once, the way `upgrade.rs` already
imports Python history. Clients store only their own preferences.

### 7. Dependency rules

- `nagents.protocol` imports nothing from the engine.
- The engine imports `nagents.protocol`; nothing in the engine imports a client.
- `ngn` depends on `ngn-api` (generated) and its own UI crates. It may not depend on any crate that executes
  tools or talks to providers.
- A test in each language enforces its rule.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| **Keep the split engine** | Every feature twice, two stores, a hand-written protocol and a process per run. This is what we are leaving. |
| **All Rust**: providers, loop and tools in Rust | Rewrites `nagents`, the reusable library and the part with the cleanest interfaces, plus every provider quirk, auth flow and A2A. Gives up Python where we want it. |
| **All Python**: Textual TUI | Tried and removed. Not fast enough for the transcript, selection and mouse behaviour we now have in Rust. |
| **Rust engine, Python only for model calls** (finish today's direction) | Removes the Python harness instead, but keeps a cross-process call for every model step and moves the clean Python interfaces into Rust. |
| **JSON-RPC over stdio only** (as Codex does) | Simpler for one client, but the web UI and remote attach need a network transport. `--standalone` keeps the stdio lease for the private case. |
| **SSE instead of WebSocket for events** (as opencode does) | Equally good. WebSocket is kept because both servers already implement it and its resume semantics are tested. |

## Consequences

**Better**

- Each concern has one implementation. The Rust side shrinks to the TUI and a generated client.
- No Python process per run: the engine starts once.
- The TUI, the web UI and scripts are all clients of one API, so a feature added to the engine reaches all of
  them.
- The contract is checked mechanically instead of kept in sync by hand.

**Worse, and what we do about it**

- **Approvals cross a process boundary.** The token, the `127.0.0.1` bind and the private registration file
  protect it; an approval names the exact call it decides.
- **The TUI needs reconnect and resynchronisation logic.** Sequence cursors make this a replay, not a guess.
- **A crashed engine takes every client's session down.** Runs are durable and resume from the store; the
  client shows "engine restarting" and reattaches.
- **Packaging still ships CPython**, now with a native wheel per platform. It changes only when Python or a
  dependency changes, and `ngn update` keeps verifying it.
- **Delta latency** gains one local socket hop, which is negligible next to model latency.

## Module map

### Rust

| Module | Today | Destination |
| --- | --- | --- |
| `ngn/src/tui.rs`, `tui/*` | TUI | **Stays** in `ngn`, reading the client store instead of `Host` |
| `ngn/src/view.rs` | Turns events into display units | **Stays**, fed by `ngn-api` events |
| `ngn/src/main.rs` | CLI, config, starts `Host` | **Stays**; finds or starts the engine instead |
| `ngn/src/install.rs` | Installer, `ngn update` | **Stays**; installs the engine bundle |
| `ngn/src/web.rs` | axum server for the web UI, over `Host` | **Stays as `ngn serve`**: starts the engine, serves the web UI's files and forwards `/api` and events to the engine; its `Host` handlers are deleted |
| `ngn-core/host.rs` | Admission, runs, events | **Deleted**; the engine's session runner |
| `ngn-core/store.rs`, `queues.rs` | `host.sqlite3`, durable queue | **Deleted** after a one-way import into the engine's store |
| `ngn-core/steering.rs` | Run-scoped steering | **Deleted**; engine (steering already exists in `nagents`) |
| `ngn-core/subagents.rs` | Delegation policy | **Deleted**; engine (`harness/agents/subagents.py`) |
| `ngn-core/runner.rs` | Starts `python -m ngn_nagents` per run | **Deleted** |
| `ngn-core/tools.rs` | Native file and shell tools | **Moves** to `nagents-native`, or to Python where it is not heavy |
| `ngn-core/tools/discovery.rs`, `tools/diff.rs` | Discovery, diff previews | **Moves** to `nagents-native` |
| `ngn-core/timing.rs` | Active-time accounting | **Moves** to the engine |
| `ngn-core/auth.rs` | Auth subprocess protocol | **Deleted**; auth is the engine's (credentials already stay in Python) |
| `ngn-core/config.rs`, `paths.rs`, `logs.rs` | Configuration, private paths, diagnostics | **Split**: client settings stay; runtime settings move to the engine |
| `ngn-core/bundle.rs` | Bundle metadata | **Stays**, used by the installer |
| `ngn-core/upgrade.rs`, `legacy.rs` | Imports of Python history and files | **Deleted** after the store import ships; kept only while supported versions need them |
| `ngn-protocol` | Hand-written runner and event types | **Replaced** by generated `ngn-api` |
| `ngn/integrations/nagents` | Runner adapter | **Deleted** |

### Python

| Package | Destination |
| --- | --- |
| `nagents` agent, `provider`, `session`, `tools`, `mcp`, `skills`, `events` | The library. Unchanged in purpose; the engine uses it. |
| `harness/runtime`, `harness/agents`, `harness/tools`, `harness/providers` | The engine's runtime. The surviving implementation of runs, subagents, tools and provider configuration. |
| `web` | The engine's server, moved under `/api/v1` with the contract made explicit. |
| `web-ui` | A client of the engine, like `ngn`. |
| `a2a`, `live`, `channels`, `batch` | Engine features exposed through the same API. |
| New `protocol` | The contract. |

## Migration plan

Each phase ships on its own, keeps `ngn` working for users, and ends at an exit check.

### Phase 0: cleanup

- Removed the root and `ngn/` `AGENTS.md` files.
- Inventory every route of `src/nagents/web` and `ngn/src/web.rs` and every `Host` call in the TUI (about 25
  distinct operations), and mark each as kept, merged or dropped.
- Delete code with no users: dead routes, compatibility paths for versions no longer supported, and checked-in
  generated or vendored files.
- Coordinate with in-flight work on the web UI (the native web-UI build and the removal of `ngn/web`) so it lands
  as a client of the engine rather than of `web.rs`.

**Exit:** a written inventory, with each route and call marked.

### Phase 1: the contract

- Create `nagents.protocol` with pydantic models for the v1 surface: sessions (list, create, get, rename, fork),
  history, runs (start, cancel, steer), queue, approvals, tasks (subagents), models and providers, config read,
  and the event union.
- Seed the event union from `ngn-protocol::Event` (`run.queued`, `queue.updated`, `run.started`,
  `steering.updated`, `text.delta`, `text.completed`, `tool.started`, `tool.completed`, `tool.approval_timing`,
  `approval.requested`, `approval.resolved`, `notice`, `task.started`, `task.updated`, `task.finished`,
  `run.finished`) plus `session.updated` and `engine.connected`.
- Add `scripts/export_contract.py` (OpenAPI plus event JSON Schema) and a Rust generator producing `ngn-api`.
- Add the drift check to pre-commit and CI.

**Exit:** `ngn-api` compiles; golden JSON fixtures round-trip in both languages; changing a model without
regenerating fails CI.

### Phase 2: decouple the TUI from `Host`

- Introduce an `Engine` trait in `ngn` covering the ~25 operations the TUI uses (submit, queue control, cancel,
  steer, decide, sessions, history, tasks, subscribe, shutdown).
- Implement it twice: `InProcess` over today's `Arc<Host>`, and `Remote` over `ngn-api`.
- Move the TUI to a client store (sessions, runs, transcript, approvals, tasks) updated from events, with change
  tracking so views redraw only what changed.
- Make every queue between the store and the UI bounded.

**Exit:** no `host.` call left in `tui.rs`, `tui/` or `view.rs`; all TUI tests and tmux smoke tests pass on
`InProcess`.

### Phase 3: the engine

- `nagents engine`: headless FastAPI on `127.0.0.1` with a token, the registration file, and `--stdio` lease
  mode.
- Implement `/api/v1` from the contract on the existing runtime, including durable event sequences and resume
  by cursor, the run queue and steering, and approvals as `approval.requested` / decision requests.
- Add a conformance suite: a Python test client that drives every endpoint and event against the engine.
- `ngn --engine=python` uses `Remote` against it, behind a flag, and `ngn serve --engine=python` starts the engine
  and forwards the web UI's `/api` and events to it.

**Exit:** the conformance suite passes; the tmux smoke scenarios (copy, tool cards, approvals, queue, steering,
subagents) pass with `--engine=python`; killing and restarting the engine mid-run resumes the session.

### Phase 4: tools move into the engine

- Port `tools.rs`, `tools/discovery.rs` and `tools/diff.rs` into `nagents-native` (PyO3, maturin, `abi3`); keep
  their existing tests and fixtures as parity tests.
- Move the light tools to Python; keep path policy (workspace-relative capabilities, symlink checks) identical
  and tested on both sides until the Rust host is gone.
- Benchmark discovery and search on a large repository against today's Rust host.

**Exit:** parity tests produce identical results; benchmarks are within 10% of the Rust host; approvals behave
the same in the smoke tests.

### Phase 5: switch and delete

- Make `--engine=python` the default for one release, with `--engine=legacy` as the escape hatch.
- Import `host.sqlite3` into the engine store once, keeping the original file.
- Then delete `runner.rs`, `host.rs`, `store.rs`, `queues.rs`, `steering.rs`, `subagents.rs`, `auth.rs`, the
  moved tools, the `Host` handlers in `web.rs`, `ngn-protocol` and `ngn/integrations/nagents`.

**Exit:** `ngn` depends only on `ngn-api` and UI crates; the dependency-rule tests pass; one release has shipped
with the engine as default and no legacy path.

### Phase 6: packaging and layout

- The bundle ships the `ngn` client, CPython, the engine wheel and the `nagents-native` wheel per platform;
  `ngn update` verifies them as it does today.
- Move the Rust workspace from `ngn/crates` to `crates/` at the repository root, beside `src/nagents`, so the
  layout reads "Python core, Rust clients".

**Exit:** installers and `ngn update` work on Linux and macOS from a clean machine.

## Testing strategy

- **Contract:** golden fixtures for every message, decoded and re-encoded in Python and Rust.
- **Conformance:** one suite drives the engine through the public API only.
- **Client:** the TUI runs against a recorded transport (replayed responses and events), as opencode's
  `memory.rs` does, so most TUI tests need no engine.
- **End to end:** the existing tmux smoke scripts, against a real engine and a fixture provider.
- **Failure:** engine restart mid-run, client reconnect from a cursor, a full event queue forcing
  resynchronisation, and an approval answered by a second client.
- **Resources:** every queue is bounded and tested at its bound; no test or tool may grow processes or memory
  without a limit.

## Open questions

1. Pydantic is the default for the contract because FastAPI already uses it. Is the engine allowed to require
   the web extra's dependencies, or should the headless engine have a slimmer dependency set?
2. Which generator writes `ngn-api`: an existing one (`typify`, `progenitor`) or a small one of our own, which
   opencode found worth it for `Unknown` variants and `Option<Option<T>>`?
3. Should `ngn` start a shared background engine by default, or a private `--standalone` one, with the shared
   engine opt-in?
4. Which tools beyond discovery, diff, search and large-file reads count as heavy?
5. When to move `ngn/crates` to `crates/`: early, so the layout matches the plan, or last, so release scripts stay
   stable during the migration?
