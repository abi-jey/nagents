# Rust migration status · 0.100.1

## Decision

`ngn` is its own repository and application. Rust owns the harness, interfaces and
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
- [x] Native offline Rust demo requiring neither Python nor model credentials.
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
  exist in 0.100.1; they are not the full old orchestration feature set.
- Input queueing and steering/interrupt delivery semantics during an active model run.
- Skills, nested instructions, Git-ignore discovery, MCP and custom Python plugins.
- Model context accounting, compaction controls, usage UI and model discovery UI.
- OAuth/device login, ChatGPT/Codex routes, Foundry Entra integration and key storage.
- Attachments, image/artifact delivery, session forks/rename/trash/grouping.
- Voice, realtime/WebRTC, local audio devices, speech-driven delegation.
- Channels/connectors (Telegram/Twilio/etc.), durable external inbox/routing ownership.
- A2A client/server/authentication and remote agent management.
- Docker/Kubernetes migration, remote multi-user authentication, signed release assets.
- Legacy configuration/database import, complete platform compatibility and benchmarks.

Provider API support inherited through nagents is separate from host integration:
the adapter currently accepts environment API-key connections, not every legacy
authentication or media mode. Local fixture coverage is not paid-provider validation.

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
