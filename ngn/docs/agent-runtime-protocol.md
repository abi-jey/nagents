# Agent Runtime Protocol · v1

**Contract:** the host starts an agent process, sends a run, receives streaming
events and tool requests, and returns tool results. This is bidirectional IPC,
not embedded Python and not a wrapper around the old Python harness.

## Transport and compatibility

- Private stdin/stdout, UTF-8 NDJSON, exactly one JSON object per newline.
- Every envelope contains exactly `protocol_version`, `run_id`, `message`.
- Maximum frame including newline: **1,048,576 bytes**. Oversized/incomplete frames,
  unknown fields, unknown message types, wrong versions and wrong run IDs fail closed.
- Initial application version **0.100.0** (current patch **0.100.1**), runtime protocol **1**, and browser event
  `schema_version: 1` are independently versioned contracts.
- v1 starts **one process per run**, not one per tool. Conversation storage survives
  process restart. No Python object, callable or pickle crosses the boundary.
- Stdout is protocol-only. Runtime logging uses stderr; the initial host discards
  it to avoid exposing raw provider payloads. Terminal errors are bounded and safe.
- Version mismatch is fatal. No negotiation downgrade or automatic fallback.
- Required capabilities are `host_tools` and `cancel`. Unknown optional capability
  labels may be ignored; adding/changing messages requires a new protocol contract.

## Handshake

The runtime speaks first; the host waits at most ten seconds:

```json
{"protocol_version":1,"run_id":"","message":{"type":"ready","runtime":"nagents","capabilities":["host_tools","cancel"]}}
```

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

Since 0.100.1, the catalog can include Rust-hosted `to_agent`, `agent_status`,
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
| `POST /api/approvals` | `{run_id, approval_id, allow}`; exact pending decision |
| `GET /api/events?session_id=ID&after=N` | WebSocket durable event stream |

HTTP APIs require `X-Ngn-Token`, except bootstrap. Mutations require the exact
same-origin `Origin` and JSON content type. WebSockets require the exact origin
and two subprotocols: `ngn.events.v1` and `ngn.token.<token>`. Tokens never go in
URLs. Subscribe-before-replay and cursor de-duplication avoid event loss on reconnect.
