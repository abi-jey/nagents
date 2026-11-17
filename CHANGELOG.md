# Changelog

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
