# Migration to one Python engine and a Rust client

[ADR 0001: Python core, Rust TUI client](https://github.com/abi-jey/nagents/blob/main/docs/adr/0001-python-core-rust-tui.md)
is in effect from **0.100.47**. The Python engine is the only engine; the Rust
host, the per-run adapter, `ngn-core`, `ngn-protocol` and the `--engine`
choice are gone. Phase 5 skipped its one-release `--engine=legacy` escape
hatch at the owner's direction. This page keeps the rollout record.

## Before and after

| Concern | Split host (up to 0.100.46) | Engine (0.100.47) |
| --- | --- | --- |
| Agent runtime | Rust `ngn-core` owns admission, queues, steering, subagents, tools and approvals. `ngn_nagents` runs the Python model loop and provider integration in a subprocess per run. | One long-lived `nagents engine`, built on `src/nagents/harness` and the headless FastAPI API in `src/nagents/web`, owns the whole run. |
| Sessions and events | `host.sqlite3` holds the Rust host's sessions, runs and journal; `nagents.sqlite3` holds runtime conversation history. The Python web application has its own state. | One engine-owned SQLite database. Clients cache projections and persist only client preferences. |
| Terminal | Ratatui calls `Host` in-process. | Rust still owns terminal rendering, input, selection, copy, navigation and themes, but calls a generated API client and renders event-fed state. |
| Browser | `ngn serve` embeds the shared React workspace and its native transport calls the Rust host's API. | `ngn serve` starts or attaches to the engine, serves the embedded UI, and proxies its same-origin API and event WebSocket. It doesn't own sessions or runs. |
| Tools and approvals | Rust validates and executes workspace tools and waits for decisions. | Python owns policy, execution and approval state. Heavy primitives such as discovery and diff previews run in-process through `nagents-native` (PyO3, maturin, `abi3`). |

The restored browser UI uses the shared Sessions, Conversation and Team
components, including Markdown, tool cards, approval review and resizable
panels. Its [native transport](https://github.com/abi-jey/nagents/tree/main/src/nagents/web-ui/src/native)
is a compatibility layer, not a new engine or a promise of Python feature
parity. Settings/model editors, unsupported session actions, voice and channels
do not become native features just because the components are shared.

## The interface to migrate to

- **One contract.** `nagents.protocol` depends only on Pydantic and defines
  requests, responses and events. Commit the OpenAPI and event JSON Schema
  exports and the Rust `ngn-api` generated from them. CI and pre-commit reject
  stale generated code. Version the API under `/api/v1` and try new endpoints
  under `/api/v1/experimental/` first. Clients reject an incompatible major
  version, ignore unknown fields, preserve unknown variants and distinguish
  absent values from explicit `null` where they differ.
- **One connection per client for events.** Use authenticated HTTP requests and
  a WebSocket that subscribes before replay, sequences events monotonically and
  resumes with `?after=<sequence>`. Durable events can replay; transient deltas
  cannot. Keep client queues bounded and reconnect and resynchronize on overflow.
- **Engine lifecycle.** Bind locally to `127.0.0.1`, use a per-engine bearer token
  and a private registration file, and allow explicit attach with `--engine
  <url>`. A private `--standalone` engine has a stdin lifetime lease. A client
  never shuts down an engine it did not start. `ngn serve` keeps the engine token
  on its side of the proxy and preserves same-origin, `no-store` and frame-deny
  protections.
- **Exact approval decisions.** The engine emits `approval.requested`; any
  attached client can decide the exact pending approval, and the first valid
  decision wins. Time actual tool execution, not the wait for a human. Replaying
  a journal must never execute a tool or revive a past approval.

## Release sequence and exit gates

Ship each phase separately. Keep both interfaces working during the
transition; switch defaults only after conformance, acceptance and recovery
checks pass.

| Phase | Work | Exit gate |
| --- | --- | --- |
| **0. Clean up and inventory** | Land the shared React build in `ngn serve`, remove the duplicate `ngn/web/` frontend, and keep the Rust-host transport until the engine API is available. Use the [route and TUI inventory](engine-inventory.md) to decide what to keep, merge or drop before fixing the v1 surface. | The restored browser passes desktop and touch acceptance against the current host, and the inventory records a disposition for each existing route and TUI operation. |
| **1. Publish the contract** | Define sessions and history, run start/cancel/steer, queue, approvals, subagent tasks, provider/model and config reads, and the event union. Export schemas and generate `ngn-api`. | The crate builds, golden fixtures round-trip in Python and Rust, and stale generation fails CI. |
| **2. Decouple the TUI** | Wrap today's `Host` behind an `Engine` trait and an `InProcess` implementation; add `Remote` over `ngn-api`. Feed a bounded client store from events instead of handing host objects to views. | No direct `Host` calls remain in `tui.rs`, `tui/` or `view.rs`. The existing TUI tests and terminal smoke pass with `InProcess`. |
| **3. Offer the Python engine** | Serve `/api/v1` from the existing Python runtime, with token, discovery and private lease mode. Implement durable event replay, queue/steering and approval semantics. Opt in via `ngn --engine=python` and `ngn serve --engine=python`; switch the browser transport to the engine through the proxy. | Public-API conformance, terminal and browser acceptance pass on the Python engine. Restart and reconnect recover the session and its history without repeating an interrupted tool or replaying an approval. |
| **4. Move tools** | Keep light tools in Python. Move heavy workspace discovery and diff previews first to `nagents-native`; follow with search and large reads where benchmarks justify it. Retain workspace path restrictions and approval behavior. | Parity fixtures agree, discovery/search on a large workspace are within 10% of the Rust host, and tool timing and approvals pass end-to-end. |
| **5. Switch and retire** (done in 0.100.47) | Remove the Rust host, per-run adapter, handwritten `ngn-protocol` and direct `Host` web handlers, with no legacy escape hatch. The engine imports each workspace's Rust host history once on first start. | A release ships without the legacy engine path. The `ngn` crate no longer depends on any tool or provider code. |
| **6. Package and lay out** (bundle done in 0.100.47) | Ship the client, CPython, engine wheel and platform-specific native tool wheel. The 0.100.47 bundle carries CPython and `nagents[engine]`; the native tool wheel is not bundled yet. Move the Rust workspace to root `crates/` when release and development tooling are ready. | Clean Linux and macOS installs and `ngn update` verify and run the packaged engine and clients. |

Use a fixture provider for repeatable terminal and browser flows: session
history, copy and scrolling, Markdown and tool inspection, one-time approvals,
queue controls, steering, and child inspection and cancellation. Test engine
restart mid-run, disconnect/reconnect, queue overflow, and an approval resolved
from another client. Fixture tests do not establish paid-provider eligibility or
feature parity for features left out of the first contract.

## Running the engine from source

From the repository root, install `.[engine]` in a Python 3.13 environment,
run `npm ci --prefix src/nagents/web-ui`, and build the Rust binary with
`cargo build --locked --manifest-path ngn/Cargo.toml -p ngn`. With that
environment active (or `NGN_ENGINE_PYTHON` set to its Python), run
`ngn/target/debug/ngn` for the TUI, `serve` for the browser, or `--standalone`
for a private engine. `--engine-demo` lets an owned engine run without provider
credentials.

The [`ngn-api`](../crates/ngn-api/) crate comes from the
[v1 contract](../../contracts/engine/v1/README.md). Acceptance is in
`ngn/scripts/python_engine_smoke.py`; it uses no personal state or credentials.
The optional `nagents-native` ABI3 wheel provides discovery and diff helpers.
Build and install it in the same environment and opt in with
`NGN_NATIVE_DISCOVERY=1` before starting an engine:

```bash
python -m maturin build --release --locked \
  --manifest-path ngn/crates/nagents-native/Cargo.toml --out ngn/target/native-wheel
python -m pip install --no-deps ngn/target/native-wheel/nagents_native-*.whl
NGN_NATIVE_DISCOVERY=1 ngn/target/debug/ngn
```

## Data

The first time the engine starts in a workspace, it imports that workspace's
Rust host history (schema 5, written by 0.100.46) from the default
`ngn-rust` data directory. It reads a private SQLite backup taken beside the
old file, never changes the original, and records the import so it runs once.
Root display history becomes read-only sessions, with an interrupted marker
where the host recorded no final outcome. Provider context, child history,
credentials, approvals, receipts and queued work are not restored.

If the old state lived elsewhere (a custom `data_dir`), or the automatic copy
failed, import a backup by hand:

```bash
(umask 077; sqlite3 -readonly /absolute/legacy/state/host.sqlite3 \
  ".backup '/absolute/private/host-snapshot.sqlite3'")
nagents import-host -C /absolute/workspace \
  --host-snapshot /absolute/private/host-snapshot.sqlite3
```

An engine imports one snapshot; the same backup is a no-op on retry and a
different one is rejected. Older Rust host files are left in place for you to
delete when you no longer need them.
