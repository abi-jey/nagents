# Agent Runtime Protocol · v1

**Contract:** the host starts an agent process, sends a run, receives streaming
events and tool requests, and returns tool results. This is bidirectional IPC,
not embedded Python and not a wrapper around the old Python harness.

## Transport and compatibility

- Private stdin/stdout, UTF-8 NDJSON, exactly one JSON object per newline.
- Every envelope contains exactly `protocol_version`, `run_id`, `message`.
- Maximum frame including newline: **1,048,576 bytes**. Oversized/incomplete frames,
  unknown fields, unknown message types, wrong versions and wrong run IDs fail closed.
- Official baseline **0.100.0**, release **0.100.2**, runtime protocol **1**, and browser event
  `schema_version: 1` are independently versioned contracts.
- v1 starts **one process per run**, not one per tool. Conversation storage survives
  process restart. No Python object, callable or pickle crosses the boundary.
- Stdout is protocol-only. The host discards runner stderr; it is never part of
  the local metadata-only runtime log. Terminal errors are bounded and safe.
- Version mismatch is fatal. No negotiation downgrade or automatic fallback.
- Required capabilities are `host_tools` and `cancel`. The optional `safe_steering`
  extension below requires both the Ready capability and `start.run.steering: true`
  before using its boundary handshake; runtimes lacking it retain baseline v1
  behavior and cannot admit steering. The independent `chatgpt_auth` extension
  must be negotiated before its extra auth fields are sent. Provider wire logging
  is another independent opt-in extension (`provider_wire_log`). Other unknown
  optional capability labels may be ignored.

## Handshake

The runtime speaks first; the host waits at most ten seconds:

```json
{"protocol_version":1,"run_id":"","message":{"type":"ready","runtime":"nagents","capabilities":["host_tools","cancel"]}}
```

For explicitly enabled provider payload capture, the host requires
`provider_wire_log` in Ready and adds `"wire_log": true` to `start.run`.
Otherwise the field is omitted. The bundled host binds the subprocess to a
preallocated private file and its device/inode through the child's environment;
there is no runtime-supplied path and no wire payload in protocol frames. The
adapter validates the binding before model execution and writes only the
library's credential-redacted HTTP observations.

The host then sends `start`, binding all further frames to a host-generated ID:

```json
{
  "protocol_version": 1,
  "run_id": "run-012345",
  "message": {
    "type": "start",
    "run": {
      "session_id": "ngn-012345",
      "prompt": "Inspect README.md",
      "system_prompt": "Operator-owned instructions",
      "database": "/private/host-state/nagents.sqlite3",
      "provider": {
        "kind": "openai_compatible",
        "model": "YOUR_MODEL_ID",
        "api": "auto",
        "base_url": "",
        "api_key_env": "OPENAI_API_KEY"
      },
      "tools": [{
        "name": "read_file",
        "description": "Read bounded workspace text",
        "parameters": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"],
          "additionalProperties": false
        }
      }],
      "max_tool_rounds": 100
    }
  }
}
```

The catalog is an immutable snapshot for this run. v1 does not dynamically reload
tools, register arbitrary Python tools, or carry approval grants to the runtime.
API keys are looked up by the runtime from its inherited environment, not passed
in frames. Runtime storage is opaque: the host never opens nagents message tables.

The official 0.100.0 catalog can include Rust-hosted `to_agent`, `agent_status`,
`agent_wait` and `agent_cancel`. No new agent message types are required. Rust
assigns child ancestry, intersects read-only permissions and approval mode with
the parent, and supplies a separate run/session to the same configured runtime.
The runtime cannot request a wider provider, process command, workspace or policy.

## Streaming events

Runtime `message` variants:

```json
{"type":"text_delta","text":"Hello"}
{"type":"text_completed","text":"Hello world"}
{"type":"notice","text":"A bounded diagnostic"}
{"type":"finished","status":"completed","error":""}
```

Each variant is inside the versioned envelope above. `text_completed` is a complete
generation, not additional text to concatenate after its deltas. The only legal
terminal statuses are `completed`, `failed` and `cancelled`. Exactly one terminal
message is required. The process must clean up and exit successfully within five
seconds of that message. Unexpected exit is failure, never implicit success.

The Rust host generates its own durable event ordering and lifecycle records;
runtimes cannot forge `approval_requested`, `approval_resolved`, session IDs,
sequence numbers or host `run_finished` events.

## Tool definitions and binding

1. Rust constructs each tool definition (name, description, JSON schema).
2. The nagents adapter registers inert definitions using its public registry.
3. The library calls a custom `ToolExecutor` **after** its normal model/tool hooks.
4. That executor sends the final call; it never invokes a local Python function.
5. Rust validates arguments, tool availability, mode and path policy.
6. Rust prepares an exact change/command preview and requests approval from its UI.
7. A host-owned one-time decision is bound to that pending call and prepared state.
8. Rust rechecks state, executes, journals the outcome and returns the result.
9. The adapter converts the result to `ToolResultEvent`, preserving id and name;
   `Agent.run()` persists it and continues its ordinary model loop.

Runtime request:

```json
{"protocol_version":1,"run_id":"run-012345","message":{"type":"tool_request","call":{"call_id":"call-1","name":"read_file","arguments":{"path":"README.md"}}}}
```

Host response:

```json
{"protocol_version":1,"run_id":"run-012345","message":{"type":"tool_result","result":{"call_id":"call-1","name":"read_file","result":{"content":"1: # Project","sha256":"..."},"error":""}}}
```

Identity is **`(run_id, call_id, name)`**. Call IDs must be unique within a run.
Duplicate requests terminate the run instead of executing twice. Swapped results
are rejected by the adapter. The host has no `approved` flag in the agent protocol;
model/runtime text cannot grant approval. Schema-valid data is not authorization.

v1 handles tool requests sequentially. It does not promise multiplexed parallel
tool execution, cross-process replay, or exactly-once side effects.

The separate native host event stream uses `tool_started` as a provisional
notification. The native Agents sidebar shows `Tool` only from the live host's
exact child dispatch state, after preparation and any required approval.
Dispatch adds no journal entry or wire event and does not imply success.
Without that live proof (for example, when replaying history), a provisional
tool start cannot establish execution. On lag, bounded recovery preserves
confirmed activity and applies newer exact phase edges without rewinding its
event cursor. The host also retains the last committed public text-phase edge
for each live child, so an omitted completion still closes `Responding` even
if later notices fill the replay tail. This snapshot retains no text payload
and does not advance the durable event cursor.

## Cancellation and recovery

```json
{"protocol_version":1,"run_id":"run-012345","message":{"type":"cancel"}}
```

The host cancels pending approvals and active host tools, requests graceful agent
cleanup, and force-stops an unresponsive runner after a short grace period. On
Unix, shell and runtime process groups are cleaned up as well. No tool is replayed
or retried automatically. Already completed actions are not rolled back.

At host restart, durable `running` runs become `interrupted`; a recovery event is
recorded. The Python library repairs unresolved saved tool-call context when a new
human run is explicitly started, without re-executing those calls.

## HTTP/client boundary

This private agent protocol is **not** exposed directly over HTTP. The browser
talks only to the Rust host's same-origin API:

| Route | Purpose |
| --- | --- |
| `GET /api/bootstrap` | Non-secret metadata and per-host web token |
| `GET /api/sessions` | Workspace session catalog |
| `GET /api/sessions/{id}/tasks` | Metadata-only task catalog and configured root budgets |
| `POST /api/sessions` with `{}` | Create a session |
| `POST /api/sessions/{id}/runs` | Submit `{prompt, request_id}` (UUID idempotency key) |
| `GET /api/sessions/{id}/events?after=0` | Durable bounded cursor replay |
| `GET /api/state` | Active runs and currently pending approvals |
| `POST /api/runs/{id}/cancel` with `{}` | Cancel a run |
| `GET /api/runs/{id}/steer` | Exact run steering state and durable receipts |
| `POST /api/runs/{id}/steer` | Explicit active-root steering `{prompt, request_id}`; no FIFO fallback |
| `POST /api/approvals` | `{run_id, approval_id, allow}`; exact pending decision |
| `GET /api/events?session_id=ID&after=N` | WebSocket durable event stream |

HTTP APIs require `X-Ngn-Token`, except bootstrap. Mutations require the exact
same-origin `Origin` and JSON content type. WebSockets require the exact origin
and two subprotocols: `ngn.events.v1` and `ngn.token.<token>`. Tokens never go in
URLs. Subscribe-before-replay and cursor de-duplication avoid event loss on reconnect.
# Host queue events (0.100.1)

The runtime still receives only protocol-v1 activations. Queuing is a host concern:
`run_queued` records acceptance, `run_started` records the atomic claim, and
`run_finished` records completion or cancellation. A pending run has host status
`queued`, which a runtime cannot use as its terminal status. `queue_updated`
records pause/resume for a session and has `run_id: ""`, including on empty
conversations. Clients must still filter by `session_id` and durable cursor; an
empty run identity grants no tool authority. Restart pauses unstarted inputs and
marks claimed work interrupted without replaying either tools or model runs.

## Host tool timing (0.100.2)

`tool_approval_timing` is a durable **client** event, not a runtime message. Its
`call_id`, `elapsed_ms` and `waiting` fields describe the active time of the exact
host call before/after its approval decision. The run/session envelope and cursor
remain host assigned. Tool output cannot supply this event or change its owner.
`tool_completed.duration_ms` excludes that call's approval waiting, but includes
validation, preparation and execution. This is elapsed active time, not CPU time;
foreground delegation can still include time spent waiting for a child.

The wire stores integer milliseconds. TUI/web format compact adjacent units such
as `125ms`, `1.25s`, `1m 2s` or `1h 1m`; an approval snapshot stays fixed while
waiting. Older clients can ignore the additive event and retain ordered replay.
Runtime protocol v1 is unchanged. Timing events do not require a storage upgrade;
the separate steering extension below upgrades the host database to schema 4.

## Opt-in ChatGPT authentication extension

See [device authentication](chatgpt-auth.md) for development/release limitations,
storage/ownership rules and the separate bounded CLI auth protocol. A runtime
advertising `chatgpt_auth` may receive `provider.auth: "chatgpt"` (only with
`kind: "openai"`, no API-key env, no custom endpoint) and an absolute
`run.auth_store` generated by Rust as `<data_dir>/auth/openai.json`. The adapter
constructs an explicit public `OpenAIAuth` and passes its async credentials callback
to `OpenAIProvider`. Only Python handles tokens/refresh; no credential values cross
the protocol. The conversation database remains independent.

The host verifies this capability **before sending start**. Without it, a
subscription run fails with a safe installation error. API-key starts omit both
new fields, preserving their original v1 wire representation and compatibility
with earlier runtimes. This adapter's package now requires the public-auth core
`nagents>=0.100.2,<0.101`; the previous 0.100.1 API-key adapter remains unchanged.
Missing public core auth never triggers Codex cache discovery, legacy imports,
API-key fallback, or an implicit login.

A runtime advertising `codex_auth` may receive `provider.auth: "codex"`, with the
same shape rules as `chatgpt` but no `run.auth_store`. The adapter first requires
`config.toml` or `auth.json` under `$CODEX_HOME` (or `~/.codex`), then uses
`OpenAIProvider` without host-supplied credentials. The local Codex policy can
select OAuth, a cached key, or an environment key. An unrelated environment key
alone cannot select Codex, and an unusable Codex OAuth login does not switch to a
key. The host verifies `codex_auth` before sending start.

## Optional safe steering (0.100.2)

The supplied adapter now advertises `safe_steering` alongside the required
capabilities. Baseline runtimes are still accepted without it; steering is refused
until this runtime capability has been observed for the exact root activation.
**Advertising the capability alone does not enable steering.** A supporting host
acknowledges it with the optional strict boolean `steering` in `start.run`:

```json
{"steering":true}
```

This is an additional field on the existing run object, not a separate frame.
The host sets it true **only** after observing `safe_steering` in Ready. Its default
is false and the host **omits it entirely when false**, preserving the identical
old Start shape for third-party runtimes without the capability. Strings, numbers,
null and other nonboolean values are rejected; unknown fields remain rejected.

An older v1 host may ignore the new Ready label and omit this acknowledgment. In
that case (also for explicit false), the new adapter installs no steering plugin,
runs one ordinary `Agent.run`, and emits Finished without any steering boundaries
or acknowledgment wait. Existing positional `Start` constructors also default to
false. Only a capability **and** explicit host true enable the extension below.

When enabled, only the runtime's private built-in public-API plugin asks at `after_tools`, after
the **whole** tool batch has been persisted. The outer adapter asks at `final`
after a successful no-tools response is persisted and its iterator closed:

```json
{"type":"steering_boundary","boundary_id":"f1f5ec61-a3db-4909-95bd-e041d2c95bec","kind":"after_tools"}
```

Rust commits an offered receipt **before** sending its reply:

```json
{"type":"steering_offer","boundary_id":"f1f5ec61-a3db-4909-95bd-e041d2c95bec","input":{"input_id":"steer-01234567890123456789012345678901","request_id":"5579032c-b257-4948-80bb-605944cbbadc","position":1,"prompt":"Explicit operator input"}}
```

Only after actual conversation persistence does the runtime acknowledge:

```json
{"type":"steering_applied","boundary_id":"f1f5ec61-a3db-4909-95bd-e041d2c95bec","input_id":"steer-01234567890123456789012345678901"}
```

All examples are `message` objects inside the standard exact-run envelope. Fields
are strict; frame bounds still apply. Boundary IDs are fresh canonical UUIDs,
unique within the process across all its `Agent.run` calls. Rust rejects reused
boundaries, foreign run envelopes, wrong input/boundary acknowledgments, and model
or tool work before the persistence acknowledgment. An explicit failed/cancelled
terminal message may end an unconfirmed offer without claiming application.

An empty decision is `{"type":"steering_offer","boundary_id":"...","input":null}`.
At `after_tools` this continues the current loop. At `final`, Rust atomically seals
future admission under its active lock **before** writing the empty reply; only a
terminal runtime message can follow. A steering-enabled activation cannot complete without
this final closure. When a final boundary instead offers input, the adapter
explicitly calls `Agent.run(prompt, same_session_id)` within the same host run,
and acknowledges from `before_generation` after the initial message is persisted.
It never appends that continuation twice. See [steering semantics](steering.md)
for HTTP receipts, bounded admission and interruption guarantees.

`steering_updated` is a host-only durable **client event**, not a runtime message.
Its `input_id` identifies a receipt to refresh (empty for negotiation/admission
closure); exact run/session attribution and cursor ordering come from the host.
Native clients refresh protected receipt snapshots on this event and `run_finished`.
Reconnect never resubmits steering. Browser Steer and TUI `/steer` are explicit
active-root actions; ordinary Enter/Send remains the separate next-run FIFO.
