# Changelog

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
