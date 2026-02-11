# Events

nagents uses an event-based streaming architecture.

## Event Types

| Event | Description |
|-------|-------------|
| `TextChunkEvent` | Streaming text chunk |
| `TextDoneEvent` | Complete text (non-streaming) |
| `ToolCallEvent` | Model is calling a tool |
| `ToolResultEvent` | Tool execution completed |
| `UsageEvent` | Token usage statistics |
| `ErrorEvent` | Error occurred |
| `DoneEvent` | Generation complete |

## TextChunkEvent

Emitted for each streaming text chunk:

```python
@dataclass
class TextChunkEvent:
    chunk: str  # The text chunk
```

```python
async for event in agent.run("Hello"):
    if isinstance(event, TextChunkEvent):
        print(event.chunk, end="", flush=True)
```

## TextDoneEvent

Emitted when complete text is available (non-streaming mode):

```python
@dataclass
class TextDoneEvent:
    text: str  # Complete text
```

## ToolCallEvent

Emitted when the model calls a tool:

```python
@dataclass
class ToolCallEvent:
    call_id: str          # Unique call identifier
    name: str             # Tool function name
    arguments: dict       # Tool arguments
```

```python
if isinstance(event, ToolCallEvent):
    print(f"Calling {event.name} with {event.arguments}")
```

## ToolResultEvent

Emitted after tool execution:

```python
@dataclass
class ToolResultEvent:
    call_id: str          # Matches ToolCallEvent.call_id
    name: str             # Tool function name
    result: Any           # Tool return value
    error: str | None     # Error message if failed
    duration_ms: float    # Execution time in milliseconds
```

```python
if isinstance(event, ToolResultEvent):
    if event.error:
        print(f"Tool failed: {event.error}")
    else:
        print(f"Tool result: {event.result} ({event.duration_ms}ms)")
```

## UsageEvent

Emitted with token usage statistics:

```python
@dataclass
class UsageEvent:
    prompt_tokens: int      # Input tokens
    completion_tokens: int  # Output tokens
    total_tokens: int       # Total tokens
```

## ErrorEvent

Emitted when an error occurs:

```python
@dataclass
class ErrorEvent:
    message: str           # Error message
    code: str | None       # Error code
    recoverable: bool      # Can the conversation continue?
```

## DoneEvent

Emitted when generation is complete:

```python
@dataclass
class DoneEvent:
    final_text: str        # Complete response text
    session_id: str | None # Session ID used
```

## Complete Example

```python
async for event in agent.run("What's 2+2?"):
    match event:
        case TextChunkEvent(chunk=chunk):
            print(chunk, end="")
        case ToolCallEvent(name=name, arguments=args):
            print(f"\n[Calling {name}...]")
        case ToolResultEvent(result=result, duration_ms=ms):
            print(f"[Result: {result} in {ms}ms]")
        case UsageEvent(total_tokens=tokens):
            print(f"\n[Tokens: {tokens}]")
        case ErrorEvent(message=msg):
            print(f"\n[Error: {msg}]")
        case DoneEvent(final_text=text):
            print(f"\n[Done: {len(text)} chars]")
```
