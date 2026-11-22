# Bounded active-run steering · 0.100.2

> **Since 0.100.47** the Python engine owns this boundary and serves it as
> `GET`/`POST /api/v1/runs/{run_id}/steer`; `ngn serve` forwards the browser to
> it. The receipt states, limits, idempotency and safe points below are
> unchanged. Passages about the Rust host, `Host::steer`, the per-run adapter
> and its NDJSON acknowledgment describe the 0.100.2–0.100.46 implementation,
> which has been removed.

This is an explicit engine/API/client boundary, **not legacy UI parity**. Both the
browser and the TUI steer on active-turn **Enter**, with **Ctrl+Enter** for the FIFO. `run --json`
still starts one activation; it does not consume additional steering from stdin.

## Operator API

`Host::steer(run_id, prompt, request_id)` and
`POST /api/runs/{run_id}/steer` accept exactly `{prompt, request_id}`. The request
must name the exact **running root** in this host, not a session, child, previous
activation or foreign host run. Until its runtime advertises `safe_steering`,
admission fails. Unsupported, stopped or sealed runs fail with HTTP 409; there is
**no conversion into FIFO input**. HTTP requires the existing exact Origin,
JSON content type and per-host token. GET is token protected as well.

Runtime enablement is **two-sided**: after observing the Ready capability, the
host explicitly sends `start.run.steering: true`. The optional field is a strict
boolean, defaults false, and is omitted from serialized Start when false. Thus
runtimes lacking the capability receive the identical baseline v1 Start shape.
When an older host ignores the Ready capability and omits the field (or sends
false), the new adapter runs ordinary baseline `Agent.run`/Finished without a
steering plugin, boundary messages or waiting for a steering acknowledgment.
Only explicit true enables the safe-point extension. This leaves existing
positional `Start` constructors and direct runtime/auth fixtures disabled by default.

Limits are 1–32,000 UTF-8 bytes of nonblank prompt text, **32 lifetime admissions
per run** (including applied inputs), and **256 pending/offered inputs per host**.
The lifetime limit is deliberately stricter than an unbounded stream of small
batches. Idempotent retries do not consume additional capacity.

POST returns `{"receipt": ...}`. `GET /api/runs/{run_id}/steer` returns
`{"steering": {run_id, session_id, run_status, supported, accepting, receipts,
run_limit, host_limit, prompt_byte_limit}}`. `supported` describes the currently
active root runtime; it is false when there is no active negotiated runtime.
Receipts are in ascending host-assigned `position` order and contain:

- `input_id`, `request_id`, exact `run_id` and `session_id`;
- `position`, original `prompt`, `status`, last `boundary_id`;
- `accepted_at_ms` and `updated_at_ms` (host timestamps).

The UUID idempotency namespace is separate from run/FIFO submission UUIDs. Within
the steering namespace a UUID is globally bound to its exact run and prompt.
Canonicalized same-UUID/same-payload retries return the same durable identity and
current status, even after stop or restart. Different text **or run** conflicts.
Neither retry nor inspection causes runtime delivery. Receipts/prompts are
sensitive local conversation data, never bootstrap/provider credentials.

| Receipt status | Meaning |
| --- | --- |
| `pending` | Admitted for this activation; not offered to its runtime yet |
| `offered` | Durably offered before writing to runtime; persistence **not confirmed** |
| `applied` | Runtime acknowledged after conversation persistence; not proof of model consumption or task success |
| `uncertain` | An offered input was interrupted without confirmation; it **may** be persisted |
| `undelivered` | Activation ended/stopped before the input was offered |

## Deliberate client controls

### Browser

While a turn runs, **Enter** (or **Steer**) steers it with the composer text and
**Ctrl+Enter** (⌘+Enter on macOS) queues the text for a separate next-run turn.
On touch, Return inserts a newline and the composer shows **Steer** and **Queue**.
When idle, Enter sends as usual. Steering is offered only after an exact active-root snapshot says `supported` and
`accepting`; the host still makes the authoritative admission decision. A child
inspector never becomes the steering target. Open **Active-turn steering · receipts**
to inspect original text, exact run/input identities and host-position ordering.
All prompt/receipt content is rendered as text, never HTML.

Network failure keeps the draft and a separate steering binding. **Check / retry
exact run** reuses both the original UUID and original run, even after activation
changes. It inspects/retries admission, not runtime delivery. A bound draft cannot
silently switch to Queue or another root. An accepted response cannot erase a
newer edited draft or replace another session's state. **Clear bound draft** is an
explicit local action for a confirmed binding; it never cancels or replays input.
**Release rejected draft** only forgets a definite host rejection, leaves the text
local and requires a separate next action; it does not convert or submit it.
An unsupported/closing/racing-final rejection retains the draft, without FIFO
fallback. Touch Return still inserts a newline; deliberate Send/Steer/Stop gestures
preserve composer focus. Mobile conversation controls and child inspection remain
independent.

### Native TUI

When the composer has focus, **Enter** sends ordinary text when idle and steers
the exact active root when busy.
**`/steer TEXT`** remains available. **Ctrl+Enter** submits to the durable FIFO for
a separate turn, even when busy; the existing FIFO order and pause state still
apply to all FIFO submissions. Ctrl+Enter does not execute slash commands.
If a turn starts while an idle Enter is being admitted, the retained draft
does not become steering on another bare Enter. Edit the draft, explicitly
queue it with Ctrl+Enter, or deliberately use `/steer TEXT`.
**F7** (or **`/steering`**) opens receipts with exact run, status and original text;
**PgUp/PgDn** scroll, **F7/Esc** closes the view. F2–F6 sessions/new/tools/agents/queue,
y/n approvals (Ctrl+Y/Ctrl+D still work) and Esc/Ctrl+C Stop retain their roles. A pending approval
keeps the composer visible but blocks both Enter submission shortcuts. Failed
steering keeps its command/draft and its own retry UUID/run
binding, separate from FIFO submission identity. Retrying the same text reuses that
binding, never the latest root. No active/capable root means no new admission.

On Unix the TUI requests only Kitty keyboard disambiguation after startup hands
off input, then restores the terminal's previous mode on exit. Compatible
terminals or multiplexers report Ctrl+Enter separately (for example, `CSI 13;5u`).
Legacy terminals that send the same byte as Enter cannot distinguish the two:
don't press Ctrl+Enter there to queue while busy. Wait until the turn is idle
before sending a new draft, or use the browser's Ctrl+Enter or **Queue** control
when working in the browser. The TUI does not alias another control key or enable
xterm `modifyOtherKeys`.

### Durable updates and reconnect

`steering_updated` is an additive, host-only durable **client event**, with an
`input_id` (empty for capability or admission closure). Its envelope supplies the
exact run/session and ordered cursor. Negotiation, admission, offer and persistence
acknowledgment trigger snapshots; final-empty sealing and Stop also invalidate admission.
It is not a runtime message: model/runtime output cannot forge it. The admission
lock and atomic final-empty boundary are unchanged. `run_finished` refreshes final
receipt settlement as well.

Reconnect uses protected GET snapshots plus durable event replay, **never POST
replay**. Terminal `applied`, `uncertain` and `undelivered` receipts stay inspectable
after Stop/failure/reload. In-memory unconfirmed retry bindings are local to the
open client, not persisted browser storage; after a reload inspect the host's
receipts/conversation rather than resubmitting an ambiguous draft as a new input.

## Safe points and ownership

The private built-in adapter plugin uses only public `AgentPlugin` hooks and
`SessionManager.add_message`. It does not load arbitrary plugins or import
`nagents.harness`, TUI or web. `HostExecutor.execute` never injects inputs between
tool calls. The plugin is installed only when the host explicitly enables steering.
`after_tools` fires only after the complete tool batch is persisted;
inputs are appended as explicit user messages in host FIFO order at that boundary.

Successful no-tools final responses use a separate outer final boundary. If an
input is offered, the adapter explicitly continues `Agent.run` on the same
persisted session, same agent and same host activation. Its first
`before_generation` acknowledges the now-persisted initial continuation prompt.
An empty final decision atomically seals admission before the host sends it.
Racing later requests get 409, never an accepted-but-lost input.

The [typed NDJSON extension](agent-runtime-protocol.md#optional-safe-steering-01002)
does not expose an asynchronous injection channel. The host alone assigns
receipt identities, ordering and run attribution. Runtime boundary/ack messages
cannot admit input, choose another run or forge host acceptance. The selected
runtime remains trusted executable code: Rust deliberately does **not** open the
runtime database to verify an acknowledgment.

## Failure, stop and storage

Cancellation seals admission and settles pending/offered receipts before
cancelling execution; it never executes leftover inputs. Failure and host restart
also settle them to `undelivered`/`uncertain`. Nothing is automatically rebound to
a later activation or replayed, including tools. If persistence races a crash or
cancellation, inspect the conversation manually; retrying the same UUID only
returns its existing receipt. This is **not exactly-once side-effect execution**.

Host schema **4** adds `steering_inputs` in the safe Rust-owned database. Upgrades
from schema 3 preserve FIFO state and durable cursors (also tested from schemas
1/2). Runtime storage remains opaque. Older schema-3 binaries refuse schema 4;
do not downgrade this database in place. Use a separate state directory/backup
when switching back. Versions newer than 4 are rejected without downgrade.

## Evidence and remaining gates

Engine tests (`tests/engine/test_runtime.py`) check that steering is a durable
follow-up of only the chosen run and that queued inputs commit before the next
model request. The Rust
client's unit tests run the TUI against a scripted engine: active-turn Enter
steers the exact root, Ctrl+Enter queues, retries reuse the original request ID,
and nothing falls back to the FIFO. `scripts/python_engine_smoke.py` drives real
Chromium (desktop and touch) and a real PTY against a live engine and a
**loopback-only fixture** model with two approval-gated write tools. It checks
that the next model request holds the full persisted tool batch followed by both
steering messages in order, that a busy Send stays FIFO, Stop leaves receipts
undelivered, receipts survive reload without POST replay, and `/steer` with F7
inspects the exact root. It makes no paid-provider requests.

```sh
# In a venv with nagents[engine], aiohttp and Playwright installed:
python ngn/scripts/python_engine_smoke.py --binary ngn/target/release/ngn \
  --output /path/to/private-evidence --check browser --check pty
```

These are offline local-client gates, not live-provider validation.
