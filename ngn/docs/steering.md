# Bounded active-run steering · development 0.100.2

This is an explicit host/API boundary, **not legacy UI parity**. Browser and TUI
steering controls are not wired in this change. Existing Enter/submit behavior
remains the durable **next-run FIFO**. `run --json` still starts one activation;
it does not consume additional steering from stdin.

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

The [typed NDJSON extension](agent-runtime-protocol.md#optional-safe-steering-01002-development)
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

Rust and adapter regressions cover full two-tool-batch ordering, FIFO identities,
retry mismatch, absent capability, child/foreign targeting, final-empty/admission
races, wrong acknowledgments, limits, and failure/cancel/crash recovery without
replay. `scripts/steering_smoke.py` exercises the real source adapter, native Rust
tools, explicit approval, HTTP receipts, final continuation, cancellation and
restart idempotency against a **loopback-only fixture**, not a live provider.
It also runs a baseline v1 runtime without the capability, rejects a synthetic
wrong acknowledgment, and SIGKILLs its own host in the offer/ack window to verify
`uncertain`/`undelivered` recovery without replay. The existing `scripts/smoke.py`
still covers local CLI, HTTP/WebSocket and real-PTY TUI/FIFO/subagent regressions.
Legacy-host checks run the actual new adapter without the Start acknowledgment
through both no-tool finals and ordinary tool/results to Finished without hanging.

Run it in an explicit venv with private `TMPDIR` under `/tmp` and a debug binary:

```sh
python ngn/scripts/steering_smoke.py --binary /path/to/target/debug/ngn
```

Browser/TUI steering affordances, receipt displays and actual steering input over
CLI/PTY are separate client gates; no full migration/parity claim is made here.
