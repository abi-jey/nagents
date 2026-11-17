# Changelog

## 0.100.2 — development, not yet released

- Preserve and reconcile the original local Python work without reverting later
  provider, cancellation, streaming and UI fixes already integrated on main.
- `./ngn/dev` builds the optimized binary and installed wheels from current working
  source, isolates development state and runs the extracted application. `--test`
  exercises the real packaged CLI/HTTP/WebSocket/PTY paths, including dirty checkouts.
- Native tool duration excludes its own approval waiting; durable timing snapshots
  keep call attribution and active duration consistent in the TUI and browser.
- Compact bounded durations and recognized tool targets in native client summaries.

This development candidate is not a rewritten `v0.100.0` or a claim of full legacy
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
