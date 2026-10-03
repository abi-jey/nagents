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

::: nagents.ToolResultEvent

::: nagents.ErrorEvent

::: nagents.RateLimitEvent

::: nagents.DoneEvent

::: nagents.FinishReason

::: nagents.Usage

::: nagents.TokenUsage

::: nagents.CompactionStartedEvent

::: nagents.CompactionDoneEvent
