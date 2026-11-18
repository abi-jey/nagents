# Rust migration status · 0.100.2

## Decision

`ngn` is an independent application **in the same repository** as the Python core.
Its Cargo workspace lives under `ngn/`; the Python package stays at `src/nagents/`.
Rust owns the harness, interfaces and
native tool bindings; `nagents` remains a reusable Python agents/integration
library. The first live runtime is an optional adapter, **not** a Python ngn worker.
Native Rust agent/model backends can implement the same protocol later without
changing the TUI or web host.

Repository inspection found approximately **15,750 Python harness lines, 21,615
web lines, and 7,958 TUI lines** in the originating checkout. That checkout also
contained uncommitted provider, runtime, UI and test work. It was left intact.
This release establishes the architectural boundary rather than deleting or
silently superseding that existing implementation.

## Ownership and implementation

| Concern | Owner in the new application |
| --- | --- |
| CLI, terminal rendering, HTTP and WebSockets | Rust `crates/ngn` |
| Session lifecycle, admission/idempotency, run state, durable event journal | Rust `ngn-core` |
| Native file/shell tools, validation and human approvals | Rust `ngn-core` |
| Runtime supervision, cancellation and process cleanup | Rust `ngn-core` |
| Agent/model/tool loop, provider authentication/API transport | Optional runtime (`nagents` initially) |
| Conversation/model context storage and repair | Runtime-owned private database |
| Browser rendering | Embedded plain HTML/CSS/ES modules, no Node server |

The old React frontend is **not claimed to be ported**. Its large existing API
contract must be deliberately reconciled with the new host before reuse; the
initial browser client proves the new shared lifecycle and tool boundary.

## Implemented baseline

- [x] Independent Cargo workspace and binary, application version `0.100.0`.
- [x] Strict versioned agent protocol, tool schema catalog and exact call binding.
- [x] Public-library nagents adapter, with no Python UI/harness/server imports.
- [x] Native CLI/headless client and Ratatui terminal client.
- [x] Native Axum web server, embedded browser client, authenticated same-origin API.
- [x] Durable cursor-based events, session listing/resume and restart interruption.
- [x] Independent sessions, bounded run admission and submission idempotency.
- [x] Native bounded read/list/search/write/edit/shell tools and one-time approvals.
- [x] Read-only mode, non-interactive fail-closed behavior, stale file detection.
- [x] Cancellation, shutdown joins and Unix child-process-group cleanup.
- [x] Rust-owned foreground/background subagent supervision with isolated sessions,
  immutable ancestry, permission ceilings, shared budgets and child deadlines.
- [x] Direct-parent result/control tools, subtree cancellation and fail-closed
  cleanup of unjoined active children.
- [x] TUI/web task catalogs, independent child inspectors, child-only cancellation
  and root-operator approval of correctly attributed child calls.
- [x] Rust-owned durable per-session FIFO queues, transactional ordering/admission,
  bounded backlog, stable request IDs and matching TUI/HTTP/WebSocket controls.
- [x] Pending-input removal, clear/pause/resume and paused restart recovery without
  replaying interrupted runs or transferring a prior tool approval.
- [x] Call-owned active tool clocks, approval-wait exclusion and compact target/time
  summaries in both native clients.
- [x] Touch Return/focus behavior, mobile conversation navigation and independently
  scrollable approval previews, with real desktop/touch Chromium acceptance.
- [x] Bounded workspace-root/nested `.gitignore` discovery and ancestor-aware search,
  retaining non-negatable protected paths and instruction-payload isolation.
- [x] Explicit ChatGPT subscription configuration, capability-gated
  runtime credentials callback and bounded native `auth login|status|logout`.
  Shared host/runtime identity guards exclude concurrent identity mutations.
- [x] Bounded explicit active-root [steering API](steering.md), two-sided runtime
  negotiation, complete persisted tool-batch/final safe points and durable
  pending/offered/applied/uncertain/undelivered receipts without automatic replay.
- [x] Separate browser Steer active turn and TUI `/steer TEXT`/F7 receipt controls,
  exact-run retry bindings and terminal receipt snapshots. Enter/Send remains FIFO.

Host storage now uses **schema 4**, upgraded transactionally from schema 3 while
preserving FIFO state and event cursors. Stop the host and back up important state
before upgrading. Schema-3 binaries refuse schema 4: downgrade with a separate
state directory or restore the backup, never edit `user_version`. The runtime
database remains opaque during normal operation. Without `--config`, the host
reads Python ngn provider settings without modifying them. History is never
imported at startup; an explicit `ngn upgrade` copies supported history into
separate Rust-owned databases without modifying the Python database.

## Explicitly not migrated yet

These are **gaps**, not features hidden behind a compatibility fallback:

- Original animated Textual design, themes, startup/exit motion, advanced editor,
  Markdown/link rendering, tool cards, diagnostics/performance dashboard.
- Original React application, full settings navigator and provider/model editors.
- Global/workspace/agent preference inheritance and optimistic configuration revisions.
- Agent Markdown definitions/profiles, hot reload and provider connection registry.
- Full legacy delegation UX, agent profiles/model overrides, child follow-ups,
  automatic completion notifications at model boundaries and wake-up timers.
  Basic Rust supervision, inspection, budgets and explicit `agent_wait` delivery
  exist in the official 0.100.0 rollup; they are not the full old orchestration feature set.
- Interrupt delivery during active model generation, queue reordering/editing,
  attachments and scheduled/follow-up delivery. Explicit safe-point steering exists
  in the API and native clients; plain FIFO remains separate.
- Skills, nested instruction loading, MCP and custom Python plugins. Native discovery
  supports workspace `.gitignore` only; user/global Git sources are not imported.
- Model context accounting, compaction controls, usage UI and model discovery UI.
- Browser auth UI/HTTP mutations, live ChatGPT login/entitlement validation,
  Foundry Entra integration and OS keyring storage. Explicit native CLI device
  auth/subscription development is [bounded separately](chatgpt-auth.md); its
  official release is blocked until the public core auth API is published.
- Attachments, image/artifact delivery, session forks/rename/trash/grouping.
- Voice, realtime/WebRTC, local audio devices, speech-driven delegation.
- Channels/connectors (Telegram/Twilio/etc.), durable external inbox/routing ownership.
- A2A client/server/authentication and remote agent management.
- Docker/Kubernetes migration, remote multi-user authentication, signed release assets.
- Full legacy configuration/database parity, complete platform compatibility and benchmarks.

Provider API support inherited through nagents is separate from host integration:
the adapter accepts environment API-key connections and an opt-in, capability-gated
ChatGPT subscription extension, not every legacy authentication or media mode.
Local fixture coverage is not paid-provider validation.

## Iteration order

1. **0.100.x:** validate the protocol boundary and first TUI/server with real user
   workflows; improve parity tests, logs, error presentation and keyboard behavior.
2. Port trusted config/provider definitions and per-agent immutable run snapshots.
3. Extend supervised delegation with queues/follow-ups/automatic result delivery,
   preserving the original ownership and approval invariants.
4. Reconcile or port the full web experience, settings, artifacts and session actions.
5. Port MCP/skills/extensions using explicit capability contracts, not Python imports
   inside the Rust host. Expose trusted Python behavior only as an optional runtime.
6. Migrate authenticated integrations, channels, A2A and voice as separately tested
   services/transports. Add explicit opt-in legacy data migration last.

Do not remove the existing Python ngn entry point, change published nagents extras,
overwrite its databases, or switch installed defaults until the relevant parity
and recovery tests pass and an operator explicitly chooses the new application.
