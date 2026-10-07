# Events API

`Agent.run()` yields event objects. Use `isinstance(event, TextChunkEvent)` or
compare `event.type` to an `EventType` member; `EventType` is not a string enum.
Usage is attached to events, not emitted as a separate `"usage"` event.
See [Events](../guide/events.md) for consumption patterns.

`TextDoneEvent` completes a model response; `DoneEvent` completes the agent
interaction. Do not display both text chunks and final text as if they were
different answers. Tool failures use `ToolResultEvent.error`; generation
failures use `ErrorEvent.message`. Initialization and extension failures can
also raise exceptions instead of emitting an error event.

Streaming tool generation emits `ToolCallProgressEvent` before an executable
`ToolCallEvent` is available. The preview contains a cumulative `arguments_text`
snapshot (at most 16,384 characters), `arguments_truncated`, a unique
`generation_id`, and a stable output `index`. Call IDs and names may arrive after
the first snapshot. Use the generation/index pair to update one preview rather
than appending every snapshot. The final call carries the same identifiers in
`extra` and retains its complete, validated arguments.

Progress status is `streaming`, `ready` after protocol validation, or `abandoned`
after a failed attempt. A preview is never executed or stored as conversation
history. A cancelled consumer may close before an abandonment event can be sent;
clients must stop loading indicators when their enclosing run ends. A ready call
can still be queued behind another call or require approval.

`ToolExecutionStartedEvent` identifies the next tool the agent invokes. The tool
may request approval as part of that invocation; this event is not proof that
side effects have occurred. `ToolResultEvent` remains the execution outcome.

When the agent exhausts its configured model/tool rounds, it emits
`ErrorEvent(code="MAX_TOOL_ROUNDS", recoverable=False)` followed by
`DoneEvent(finish_reason=FinishReason.UNKNOWN)`. The existing error message and
usage totals are retained. For a positive plain integer limit, the error includes
`extra={"max_tool_rounds": 30}` (using the actual configured value); booleans and
other values are never coerced into this metadata. This is a round budget, not an
aggregate tool-call count: one round can contain several calls. No extra model
turn or automatic retry is added when the budget is exhausted.

The web UI describes this known error as a round limit using fixed safe text.
A native child that reaches its limit remains failed, retains its history, and
reports a fixed budget explanation to its parent. These diagnostics do not change
the library, Harness, or child round limits or their cleanup behavior.

If a descendant finishes while its immediate parent is stopping or cancelled,
the Harness keeps the descendant's `TaskCompleted` outcome and withholds delivery.
It emits the host-owned `TaskDeliveryWarning` subtype of `ErrorEvent`, with code
`TASK_DELIVERY_SKIPPED` and `recoverable=True`. Recoverable refers to the root run:
it may continue or delegate replacement work, but descendant data is never routed
directly to Main and the failed/cancelled child outcomes are not changed.

This diagnostic carries `task_id`, `task_name`, `parent_task_id`,
`parent_session_id`, `child_session_id`, `depth`, `activation`, and `followup`.
The web client retains only these owned scope fields and fixed safe text, so a
child warning does not clear Main's tool preview or mark its run failed. Ordinary
provider errors cannot gain this scope by copying the code or adding metadata.
Missing or mismatched parent identities remain fatal errors.

Built-in text providers attach safe diagnostics to caught generation failures in
`ErrorEvent.extra["transport"]`. Existing `code`, `message`, and `recoverable`
values keep their meaning; diagnostics do not enable retries or change deadlines.
Authentication, model-discovery, and other failures may omit this metadata.
Native Codex also omits it for early request validation and non-200 HTTP replies,
which keep their existing specific error codes.

| Field | Meaning |
| --- | --- |
| `category` | A fixed classification: `dns`, `connect`, `tls`, `connect_timeout`, `read_timeout`, `timeout`, `connection_lost`, `connection`, `response_payload`, `decode`, `invalid_data`, `protocol`, `http`, or `unknown`. |
| `phase` | Native Codex reports `request` before response headers and `response` after headers arrive. The shared provider path reports `unknown` because its outer error handler does not observe that boundary. |
| `generation_elapsed_ms` | Elapsed time for the generation invocation through failure, including retries and backoff; this is not an individual HTTP request duration. |
| `model_call_id` | The current observation scope's existing call ID, when available and valid. It is not a newly minted HTTP request ID; direct provider calls can inherit the current context or omit it. |

A generic timeout remains `timeout`; it does not prove a read, connect, or total
request deadline expired. Generic `ValueError` remains `invalid_data`, rather
than being misreported as decoding. Diagnostics contain no exception messages,
URLs, headers, response/request bodies, or authentication values. Raw HTTP
logging and the opt-in payload observer remain separate facilities. Library
consumers and `ngn --json` retain the nested metadata. The normal web error view
and other human-readable clients continue to show their existing safe messages.

::: nagents.EventType

::: nagents.Event

::: nagents.TextChunkEvent

::: nagents.ReasoningChunkEvent

::: nagents.TextDoneEvent

::: nagents.ToolCallEvent

::: nagents.ToolCallProgressEvent

::: nagents.ToolExecutionStartedEvent

::: nagents.ToolResultEvent

::: nagents.ErrorEvent

::: nagents.RateLimitEvent

::: nagents.DoneEvent

::: nagents.FinishReason

::: nagents.Usage

::: nagents.TokenUsage

::: nagents.CompactionStartedEvent

::: nagents.CompactionDoneEvent
