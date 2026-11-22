# ADR 0001: API and TUI migration inventory

This is the Phase 0 baseline for the [migration plan](migration.md). It maps
what the current servers and terminal call to their intended owner. It is not
an API specification: Phase 1 fixes the new paths and schemas, and no shipped
route is removed by this inventory.

- **Keep:** preserve the capability with the named owner. Features outside
  the first contract stay available in the existing Python app until their
  equivalent is tested.
- **Merge:** move duplicate responsibilities to one engine operation, snapshot
  or event stream. Keep the existing transport usable until its client switches.
- **Drop:** retire the old route or in-process dependency after its successor
  exists. This never means discarding stored history or replaying work.

## Python web

The 97 explicit HTTP, WebSocket and static mount bindings below are relative
to the current app in [`src/nagents/web`](https://github.com/abi-jey/nagents/tree/main/src/nagents/web).
Combined methods count separately. FastAPI's documentation routes are disabled.

| Source | Existing bindings | Disposition |
| --- | --- | --- |
| `http/app.py` | `GET /`, `GET /dashboard`, `MOUNT /assets` | **Merge** into browser hosting under `ngn serve`; no UI files served by the headless engine. |
| `http/app.py` | `GET /api/bootstrap` | **Merge** safe metadata with engine version/config reads; keep browser connection setup in the proxy. Never return the engine bearer token to the browser. |
| `http/app.py` | `GET /api/dashboard` | **Keep** engine diagnostics outside the initial contract; expose only operator-safe summaries. |
| `http/app.py` | `WS /api/events`, `GET /api/activity/{session_id}/{after}` | **Merge** live updates and catch-up with the engine event stream and bounded history reads. Preserve exact session attribution. |
| `http/app.py` | `POST /api/messages`, `POST /api/cancel` | **Merge** with engine run admission and exact-run cancellation. Keep request identities, delivery mode and safe steering boundaries explicit. |
| `http/app.py` | `POST /api/approval` | **Merge** exact-call decisions into engine-owned approvals. Do not silently turn a one-time decision into a lasting tool grant; retain Python grant behavior separately until its policy is specified. |
| `http/app.py` | `GET /api/sessions`, `GET /api/sessions/{session_id}`, `POST /api/sessions/new` | **Merge** session list/get/create with one engine store. Return state without changing another client's selected conversation. |
| `http/app.py` | `POST /api/sessions/resume` | **Drop** global session selection in v1. Select locally after an authorized session read; move voice shutdown and other execution changes to explicit actions, not a viewer's navigation. |
| `sessions/actions.py` | `POST /api/sessions/{session_id}/fork`, `POST /api/sessions/{session_id}/rename` | **Keep** as engine session operations in the first contract. Preserve ancestry on forks. |
| `http/app.py` | `GET /api/sessions/{session_id}/context`, `DELETE /api/sessions/{session_id}` | **Keep** context accounting and deletion as later engine features. Preserve permanent-delete confirmation and live-run checks. |
| `http/app.py` | `GET /api/trash`, `PUT /api/trash/settings`, `POST /api/trash/{session_id}/restore`, `DELETE /api/trash/{session_id}` | **Keep** engine trash lifecycle outside the first contract; retain revision and deletion-generation checks. |
| `sessions/groups.py` | `GET /api/session-groups`, `POST /api/session-groups/create`, `POST /api/session-groups/update`, `POST /api/session-groups/move`, `POST /api/session-groups/reparent`, `POST /api/session-groups/remove` | **Keep** engine session organization outside the first contract; only mutate groups and sessions in scope. |
| `http/app.py` | `GET /api/settings`, `GET /api/settings/global` | **Merge** with safe engine config reads; browser and terminal appearance stay client-local. |
| `http/app.py` | `POST /api/settings`, `POST /api/settings/reset`, `POST /api/settings/global`, `POST /api/settings/global/reset` | **Keep** revision-checked runtime config edits outside the first contract. No agent or credential writes in a UI client. |
| `http/app.py` | `GET /api/models`, `GET /api/reasoning-options`, `GET /api/providers`, `GET /api/provider-scopes/{scope}/providers`, `GET /api/providers/{name}/models`, `GET /api/provider-scopes/{scope}/providers/{name}/models` | **Merge** into engine provider and model reads. Return scoped metadata/capabilities, not secrets. |
| `http/app.py` | `PUT/DELETE /api/provider-scopes/{scope}/providers/{name}`, `POST /api/provider-scopes/{scope}/providers/{name}/activate`, `POST /api/providers/inherit` | **Keep** scoped provider administration outside the first contract; credentials and active bindings remain engine-owned. |
| `http/app.py` | `GET/POST /api/login/chatgpt`, `POST /api/login/chatgpt/cancel` | **Keep** the device-auth lifecycle in the engine, with explicit operator actions and no credential values in client state. |
| `sessions/agents.py` | `GET/POST /api/sessions/{session_id}/agent` | **Keep** session agent binding outside the first contract; don't couple it to a client's selection. |
| `settings/agents.py` | `GET/PUT /api/agent-settings` | **Keep** engine agent definitions and their scoped updates outside the first contract. |
| `settings/agent_models.py` | `GET/PUT/DELETE /api/agent-models/{name}` | **Keep** agent model overrides in the engine outside the first contract. Preserve inheritance rather than copying provider configuration. |
| `tasks/routes.py` | `GET /api/sessions/{root_session_id}/tasks`, `GET /api/sessions/{root_session_id}/tasks/{session_id}` | **Merge** agent catalog and inspector reads with the engine task and history operations. Enforce the exact root/child relationship. |
| `tasks/routes.py` | `POST /api/sessions/{root_session_id}/tasks`, `POST /api/sessions/{root_session_id}/tasks/{session_id}/messages`, `PUT /api/sessions/{root_session_id}/tasks/{session_id}/settings` | **Keep** advanced task actions outside the first contract. Apply parent/child rights and permission ceilings in the engine. |
| `settings/tools.py` | `GET/POST /api/tools`, `POST /api/tools/approvals/revoke`, `POST /api/tools/mcp/retry` | **Keep** tool registry, saved grants and MCP operations in the engine outside the first contract. Preserve revocation and exact-definition binding. |
| `settings/skills.py` | `GET /api/skills` | **Keep** engine skill discovery outside the first contract. |
| `settings/remote_agents.py` | `GET /api/remote-agents`, `POST /api/remote-agents/discover`, `PUT /api/remote-agents/{name}`, `PUT /api/remote-agents/{name}/trust`, `POST /api/remote-agents/{name}/login`, `POST /api/remote-agents/{name}/probe`, `GET /remote-agent-login/callback` | **Keep** remote-agent administration in the engine outside the first contract. Retain trust, connection and callback identity checks; do not expose it by widening the local proxy boundary. |
| `http/app.py` | `GET /api/channels`, `POST /api/channels/refresh`, `PUT/DELETE /api/channels/{id}` | **Keep** engine channel lifecycle outside the first contract; preserve explicit activation and credential boundaries. |
| `channels/local_delivery.py` | `GET /api/sessions/{session_id}/deliveries/{delivery_id}/assets/{asset_id}` | **Keep** session-bound delivery reads outside the first contract. |
| `channels/uploads.py` | `GET /api/sessions/{session_id}/uploads`, `POST/DELETE /api/sessions/{session_id}/uploads/{upload_id}`, `GET /api/sessions/{session_id}/uploads/{upload_id}/preview` | **Keep** bounded session uploads outside the first contract. Check the owning session on every read and mutation. |
| `sessions/artifacts.py` | `GET /api/sessions/{session_id}/artifacts/{history_id}/{part_index}` | **Keep** engine artifact reads outside the first contract with the same session authorization and content rules. |
| `live/instructions.py` | `GET/PUT /api/live/instructions` | **Keep** engine-owned voice instructions outside the first contract. |
| `live/routes.py` | `GET /api/live`, `GET/POST/DELETE /api/live/settings`, `POST /api/live/sessions`, `WS /api/live/sessions/{session_id}/audio`, `GET /api/live/sessions/{session_id}`, `GET /api/live/sessions/{session_id}/delegation-details`, `GET /api/live/sessions/{session_id}/context`, `POST /api/live/sessions/{session_id}/close` | **Keep** live session/control and media transport in the engine outside the first contract. Audio is not replayable journal data. |
| `shells.py` | `GET /api/sessions/{session_id}/shells`, `GET /api/sessions/{session_id}/shells/{shell_id}`, `POST /api/sessions/{session_id}/shells/{shell_id}/stop`, `GET /api/sessions/{session_id}/shells/{shell_id}/output` | **Keep** engine-owned terminal lifecycle outside the first contract. Scope output and stop requests to the owning session, even if a native helper owns the PTY. |

## Native browser host

The 16 explicit bindings in `ngn/crates/ngn/src/web.rs` don't count Axum's
implicit HEAD handling. `WS` is the GET upgrade at `/api/events`.

| Existing bindings | Disposition |
| --- | --- |
| `GET /`, `GET /assets/{*path}` | **Keep** on `ngn serve`. Serve only built assets; retain origin checks, no-store, frame denial and the current CSP. |
| `GET /api/bootstrap` | **Merge** safe engine metadata with browser-only proxy setup. The engine token stays server-side. |
| `GET /api/state` | **Merge** active runs and pending approval snapshots with engine state and live events. A replayed approval is not pending. |
| `GET/POST /api/sessions`, `GET /api/sessions/{id}/events` | **Merge** list/create/history with the engine session API and replay cursor. Preserve stable IDs and read-only archives. |
| `POST /api/sessions/{id}/runs`, `GET/POST /api/sessions/{id}/queue`, `POST /api/runs/{id}/cancel` | **Merge** run and queue operations. Keep UUID-bound admission, FIFO pause/clear/remove and exact-run cancellation. |
| `GET /api/sessions/{id}/tasks` | **Merge** with the engine task catalog and exact child history; never promote a child to a root. |
| `GET/POST /api/runs/{id}/steer` | **Keep** the engine's exact-run steering/receipt capability. No fallback to FIFO or another activation. |
| `POST /api/approvals` | **Merge** with engine approval decisions for the exact call, including child-owned calls. First valid decision wins. |
| `WS /api/events` | **Merge** with the resumable engine stream. The proxy keeps exact browser-origin checks and forwards authorized subscriptions. |

## Terminal dependencies

These are production `Host` operations in `tui.rs`, including the session
helper in `main.rs` and store recovery in `tui/agents/activity.rs`. `view.rs`
already renders records without calling `Host`.

| Current operations or fields | Disposition |
| --- | --- |
| `store.sessions`, `store.session`, `store.latest_native_session`, `store.create_session` | **Merge** into engine list/get/create. Continue selects an eligible recent session; switching is local and cannot stop other clients' work. |
| `store.events`, `events.subscribe` | **Merge** into history and one resumable event stream. Resync on lag instead of discarding events silently. |
| `conversation_activity`, `approvals` | **Merge** into scoped live snapshots plus events, separating pending approval state from replay. |
| `store.tasks`, `store.task_activity_tail`, `child_tool_dispatches` | **Merge** into the engine's task projection with bounded catch-up and exact live tool attribution. No direct store access by the agents rail. |
| `submit_idle_with_receipt`, `submit_with_receipt`, `input_receipt` | **Merge** into explicit idle vs. FIFO admission and request receipt lookup. Preserve rejection versus unknown acceptance and never rebind a draft. |
| `queue`, `control_queue` | **Keep** engine queue reads and mutations, keyed by session. |
| `steer`, `steering` | **Keep** engine steering and receipt operations keyed by exact run. |
| `decide`, `cancel` | **Keep** engine approval and cancellation operations. Root, child and pending queue targets remain distinct. |
| `config`, `config.workspace` | **Merge** public workspace/model/provider and permission metadata into safe config reads. Drawing no longer receives the host configuration object. |
| `config.legacy_animations`, `config.legacy_composer_theme` | **Keep** as terminal preferences, not engine state. |
| `runtime_log_path`, `wire_log_path` | **Keep** safe diagnostics with the process that owns them. A remote path is not a local file, and capture remains explicit. |
| `shutdown`, raw `store` access | **Drop** from the UI. The launcher releases only an engine it owns, and store access stays in the engine. |

Phase 1 should cover the common session, history, run, queue, approval, task and
config rows with cross-language fixtures. Before retiring either client
transport, rerun the existing HTTP/WebSocket and PTY smoke tests and the
desktop/touch browser acceptance against the engine, including two clients
reading the same stream and competing to resolve an exact pending approval.
