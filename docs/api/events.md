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
