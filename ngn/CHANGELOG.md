# Changelog

## 0.100.3

- Fix: the installer accepts group-writable parents like ~/.local/bin under Debian/Ubuntu user-private groups (umask 0002), and release publishing restores the installer exec bit.

## 0.100.2 — self-contained installation (2026-11-18)

- Self-contained Linux x86_64 installer with pinned relocatable CPython 3.13.18,
  hashed binary dependency wheels, CA certificates and upstream redistribution
  notices. No Python/pip/Rust/Node setup is needed on the target system.
- Native managed `ngn update --check`/`ngn update`, version pinning, verified
  staging and an atomic current-version switch. Keep prior runtimes, refuse
  foreign executables/unsafe prefixes, and never modify application state/auth.
- Bundled default runners use a version-specific isolated Python with bytecode
  writes disabled; explicit trusted custom runners are still honored.
- Share version 0.100.2 across the reusable core, native application and adapter.
  One tagged CI pipeline publishes the tested core to PyPI before creating the
  sole GitHub release, titled `v0.100.2`, with both exact public core distributions.
  Official bundles bind those published bytes to source and runtime content;
  source-only API availability is not published compatibility.
- Preserve and reconcile the original local Python work without reverting later
  provider, cancellation, streaming and UI fixes already integrated on main.
- `./ngn/dev` builds the optimized binary and installed wheels from current working
  source, isolates development state and runs the extracted application. `--test`
  exercises the real packaged CLI/HTTP/WebSocket/PTY paths, including dirty checkouts.
- Native tool duration excludes its own approval waiting; durable timing snapshots
  keep call attribution and active duration consistent in the TUI and browser.
- Compact bounded durations and recognized tool targets in native client summaries.
- Touch browser Return stays in the draft; deliberate Send/Queue and Stop preserve
  composer focus. Mobile conversation controls, scrollable approval previews and
  non-collapsing decision controls have optional real-Chromium acceptance coverage.
- Native list/search respect bounded workspace `.gitignore` rules, including nested
  origins, negation and explicit starting paths. Protected paths remain excluded.
- The Python library exposes device authentication and guarded storage without a
  harness/UI import, with cooperative cross-process refresh ownership.
- Explicit development ChatGPT connections use a host-generated private store,
  negotiated runtime capability and credentials callback without Codex discovery or
  API-key fallback. Native CLI device login/status/logout use bounded redacted
  frames and shared-host/exclusive-writer ownership. Browser login, live entitlement
  checks and publication of the prerequisite core version remain separate gates.
- Bounded exact-root steering at persisted whole-tool-batch/final boundaries, with
  two-sided optional negotiation and durable pending/offered/applied/uncertain/
  undelivered receipts. Nothing is automatically replayed or relabelled as FIFO.
- Explicit browser Steer active turn and native `/steer TEXT`/F7 receipt controls;
  ordinary Enter/Send still queues next-run input. Reconnect only inspects receipts.
- Host schema 3→4 preserves queues/cursors; back up important state before upgrading
  and use a separate state directory or that backup when downgrading older binaries.
- External development Cargo target bases are namespaced by canonical checkout.
  Both installed archive paths include the checked-in ChatGPT example. Synthetic
  auth gates run explicitly; tag publication refuses a published core without its
  public auth API. Private test roots do not depend on the invoking shell's umask.
- Create each missing default/custom development cache ancestor as 0700 without
  changing existing unsafe paths. Acceptance also launches the extracted offline,
  read-only app in the real cache ancestry, not only independent fixture state.

This release is not a rewritten `v0.100.0` or a claim of full legacy
parity. Production deployment and paid-provider/Codex validation are separate gates.

## Official 0.100.1 — durable input queues

- Rust-owned per-session FIFO, stable idempotent request IDs, 32 pending inputs per
  session/256 per host, admission within existing root/run limits and no backlog bypass.
- TUI F6 and `/queue` controls, busy input acceptance, pause/resume, individual
  removal and clear-and-pause; matching browser, HTTP and durable WebSocket controls.
- Stop/failure/shutdown/restart pause pending work. Explicit resume only dispatches
  unstarted inputs; interrupted runs and completed tool actions are never replayed.
- Preserve each queued run's approval mode; a prior grant cannot authorize a future
  turn. Child sessions remain inspect-only and cannot admit/control root queues.
- Host schema 2→3 migration preserves journals and introduces session-scoped queue
  events with an empty run ID. Runtime protocol v1 is unchanged.
- FIFO/capacity/concurrency/recovery and TUI/web regressions, actual model-fixture
  HTTP/WebSocket flows and real PTY removal/resume/clear/restart acceptance.

Steering, follow-ups, original UI parity and the other migration gaps remain open.

## Official 0.100.0 — monorepo release

- Consolidate the Rust application under `ngn/` in `abi-jey/nagents`, retaining the
  Python core and its independent version. No new GitHub repository is used.
- Roll up both local prototypes below, including Rust supervision and the
  successful-tool-result adapter fix. Those standalone tags were local only.
- Shared pre-commit gates and separate Rust CI/release jobs, real TUI/web acceptance,
  adapter-wheel testing, extracted-install checks, commit-bound manifests and checksums.
- The `v0.100.x` tag series is reserved for Rust; it cannot trigger Python/PyPI publishing.
- Support the tested Python core 0.28.x as well as 0.27.1 through the public adapter boundary.

The following entries record **unpublished standalone prototype history**, not
official GitHub releases. Their original local tags/artifacts remain unchanged.

## 0.100.1 — 2026-11-17

### Added

- Rust-owned subagent admission, isolated sessions, immutable parent/root topology,
  per-root depth/concurrency/execution budgets and child execution deadlines.
- `to_agent` foreground/background delegation, `agent_status`, `agent_wait` and
  `agent_cancel`; result/control authorization is scoped to the immediate parent.
- Subtree cancellation and structured cleanup before a parent's terminal event;
  unjoined active children cause failure rather than being silently orphaned.
- TUI F5 agent inspection and Ctrl+X child-only cancellation; browser agent catalog,
  independent child transcript streams, cancellation and attributed child approvals.
- Metadata-only task HTTP endpoint, durable task lifecycle events and interrupted
  child recovery without replay. Host database schema upgrades from 1 to 2.
- Supervisor policy/concurrency/recovery tests and matching TUI, browser, real
  HTTP/WebSocket and PTY regressions using the actual nagents adapter.

### Fixed

- Translate empty wire errors to `ToolResultEvent.error=None`: nagents otherwise
  treated successful Rust tool results as errors in saved/model context. The new
  background-result fixture verifies that the model receives actual tool JSON.
- A cancelled activation does not start a fresh runtime during graceful cleanup.
- Task inspection retains the selected identity while new children are admitted.

Full legacy queues, follow-ups, profiles/settings, voice, channels and original UI
parity remain incomplete. See the migration checklist, not a blanket parity claim.

## 0.100.0 — 2026-11-17

First standalone Rust migration baseline.

### Added

- Native Rust harness admission, per-workspace sessions, durable run/event journal,
  idempotent prompt submission and interrupted-run recovery.
- Ratatui terminal client and Axum local HTTP/WebSocket server with embedded web UI.
- Strict Agent Runtime Protocol v1 with schema-defined tool catalogs, exact
  `(run_id, call_id, name)` binding, cancellation and supervised runner processes.
- Optional nagents adapter using public agent/executor APIs, without importing the
  Python harness, TUI or web server.
- Native Rust file inspection, listing, search, create/replace/edit and shell tools;
  bounded I/O, stale-file checks, read-only mode and one-time approval enforcement.
- Rust-only offline demo and versioned headless JSON events.
- Unit/integration tests and local fixture smoke tests exercising the real CLI,
  nagents runtime, HTTP/WebSocket behavior and terminal interaction.

### Scope

This is a runnable architectural baseline, **not a one-to-one port of every legacy
feature**. Subagent supervision, profiles/settings, MCP/skills, original React/UI
parity, queues, OAuth, A2A, channels and voice remain on the
[migration checklist](docs/migration.md). Linux is the initial validated platform.
No legacy nagents code/configuration/database is removed or migrated implicitly.
