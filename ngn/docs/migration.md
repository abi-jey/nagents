# Migration to one Python engine and a Rust client

[ADR 0001: Python core, Rust TUI client](https://github.com/abi-jey/nagents/blob/main/docs/adr/0001-python-core-rust-tui.md)
is **Proposed**. This is the rollout plan for that architecture, not a claim
that the new engine has shipped. The Rust host stays supported until the
replacement passes the cutover gates below.

## Shipped baseline and target

| Concern | Shipped split host | ADR target |
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
| **5. Switch and retire** | Make the Python engine the default for one release with an explicit `--engine=legacy` escape hatch. Import Rust host state once into the engine store. After the transition, remove the Rust host, per-run adapter, handwritten `ngn-protocol` and direct `Host` web handlers. | A release ships without the legacy engine path. Dependency tests prevent `ngn` from reaching tool/provider code and prevent Python protocol/engine modules from depending on clients. |
| **6. Package and lay out** | Ship the client, CPython, engine wheel and platform-specific native tool wheel. Move the Rust workspace to root `crates/` when release and development tooling are ready. | Clean Linux and macOS installs and `ngn update` verify and run the packaged engine and clients. |

Use a fixture provider for repeatable terminal and browser flows: session
history, copy and scrolling, Markdown and tool inspection, one-time approvals,
queue controls, steering, and child inspection and cancellation. Test engine
restart mid-run, disconnect/reconnect, queue overflow, and an approval resolved
from another client. Fixture tests do not establish paid-provider eligibility or
feature parity for features left out of the first contract.

## Trying the opt-in engine from source

From the repository root, install `.[engine]` in a Python 3.13 environment,
run `npm ci --prefix src/nagents/web-ui`, and build the Rust binary with
`cargo build --locked --manifest-path ngn/Cargo.toml -p ngn`. Set
`NGN_ENGINE_PYTHON` to that environment's Python executable. Then use the
resulting `ngn/target/debug/ngn` with `--engine=python` for the TUI,
`--engine=python serve` for the browser, or `--standalone` for a private
child. `--engine-demo` lets an owned engine run without provider credentials.

The [`ngn-api`](../crates/ngn-api/) crate comes from the
[v1 contract](../../contracts/engine/v1/README.md).
Source acceptance is in `ngn/scripts/python_engine_smoke.py`. It does not use
personal state or credentials and can test a registered engine, a private
lease, or reconnect after a crash without re-running a tool. The optional
`nagents-native` ABI3 wheel shares the legacy host's diff algorithm and
workspace Git matcher. For source trials, build and install it in the same
environment and opt in with `NGN_NATIVE_DISCOVERY=1` before starting an engine:

```bash
python -m maturin build --release --locked \
  --manifest-path ngn/crates/nagents-native/Cargo.toml --out ngn/target/native-wheel
python -m pip install --no-deps ngn/target/native-wheel/nagents_native-*.whl
NGN_NATIVE_DISCOVERY=1 NGN_ENGINE_PYTHON="$(command -v python)" ngn/target/debug/ngn --engine=python
python ngn/scripts/native_discovery_bench.py
```

Discovery and search keep the legacy scan and read budgets. Python supplies
protected paths and identities and still owns approval and all writes. The
benchmark compares output and measures both paths on the same 4,200-file
fixture; it is not a claim of production or cross-platform parity. The release
bundle and default engine are unchanged.

## Data and rollback

**Today**, the native host can copy supported Python display history into a
separate read-only archive. Host schema 5 adds archives alongside schema 4
steering receipts. See [Python session history](../README.md#python-session-history)
and [installation and update safety](installation.md) before opening important
state. Stop the host and back up both stores; downgrade with a compatible state
directory or backup, never by editing SQLite `user_version`.

**Opt-in source preview:** stop both workspace engines and create a private
SQLite backup outside the workspace and both state directories. From the
Python engine environment:

```bash
(umask 077; sqlite3 -readonly /absolute/legacy/state/host.sqlite3 \
  ".backup '/absolute/private/host-snapshot.sqlite3'")
nagents import-host -C /absolute/workspace \
  --host-snapshot /absolute/private/host-snapshot.sqlite3
```

Use the same `--state-dir` or trusted configuration as the engine if it
doesn't use its default path. The importer accepts Rust schema 5 (v0.100.46),
requires an offline backup without WAL or journal sidecars, and refuses an
owned destination. It copies root display history as read-only sessions,
including an interrupted marker where the host recorded no final outcome.
The same backup is a no-op on retry; a different backup is rejected.
Keep both the source and its backup: no changes flow back. Provider context,
child history, credentials, approvals, receipts and queued work are not
restored. This preview is not authorization to switch defaults.

The generator choice, headless engine dependencies, default shared versus
standalone lifecycle, and the final native-tool and workspace scope remain
open in the ADR. The React restoration does not decide them.
