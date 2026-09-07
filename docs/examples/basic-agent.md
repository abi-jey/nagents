# Basic Agent

Create a conversational agent with a `Provider` and a required `SessionManager`.
Set `OPENAI_API_KEY` and `OPENAI_MODEL` to credentials and a model available to
your account before running these examples. `Provider` does not read environment
variables automatically; the examples pass them explicitly.

## Simple Chat Agent

```python title="basic_chat.py" linenums="1"
import asyncio
import os
from pathlib import Path

from nagents import Agent, DoneEvent, ErrorEvent, Provider, ProviderType, SessionManager, TextChunkEvent


async def main() -> None:
    agent = Agent(
        provider=Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ["OPENAI_MODEL"],
        ),
        session_manager=SessionManager(Path("sessions.db")),
        system_prompt="You are a helpful assistant.",
        streaming=True,
    )

    try:
        async for event in agent.run("Hello! What can you help me with?"):
            if isinstance(event, TextChunkEvent):
                print(event.chunk, end="", flush=True)
            elif isinstance(event, ErrorEvent):
                print(f"\nError: {event.message}")
            elif isinstance(event, DoneEvent):
                print(f"\nSession: {event.session_id}")
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
```

The first `run()` initializes the database and verifies the provider model.
Initialization failures can raise exceptions before any events are emitted.
`streaming` defaults to `False`; enable it to receive text chunks. For a
non-streaming agent, read the completed answer from `DoneEvent.final_text`.

## Interactive Chat Loop

Reuse a string session ID to retain context. `Agent.run()` creates that session
if it does not exist; there is no separate `create_session()` call.

```python title="interactive_chat.py" linenums="1"
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from nagents import Agent, ErrorEvent, Provider, ProviderType, SessionManager, TextChunkEvent


async def chat() -> None:
    agent = Agent(
        provider=Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ["OPENAI_MODEL"],
        ),
        session_manager=SessionManager(Path("sessions.db")),
        system_prompt="Be concise but helpful.",
        streaming=True,
    )
    session_id = f"chat-{uuid4().hex}"
    print(f"Session: {session_id}. Type 'quit' to exit.")

    try:
        while True:
            user_input = (await asyncio.to_thread(input, "You: ")).strip()
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if not user_input:
                continue

            print("Assistant: ", end="", flush=True)
            async for event in agent.run(user_input, session_id=session_id):
                if isinstance(event, TextChunkEvent):
                    print(event.chunk, end="", flush=True)
                elif isinstance(event, ErrorEvent):
                    print(f"\nError: {event.message}")
            print()
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(chat())
```

To resume after restarting the process, reuse the printed ID and the same
database. Serialize turns for a given session rather than running them in
parallel.

## Handling Events and Usage

Use event classes, or compare `event.type` with an `EventType` member, not a
string. Usage is attached to events, not emitted as a separate `"usage"` event.
The following fragment assumes the streaming agent above:

```python
from nagents import DoneEvent, ErrorEvent, ReasoningChunkEvent, TextChunkEvent, TextDoneEvent

async for event in agent.run("Explain quantum computing"):
    match event:
        case TextChunkEvent(chunk=chunk):
            print(chunk, end="", flush=True)
        case ReasoningChunkEvent(chunk=chunk):
            print(f"\n[Reasoning: {chunk}]")
        case TextDoneEvent(finish_reason=reason):
            print(f"\nModel response finished: {reason.value}")
        case DoneEvent(usage=usage):
            print(f"Tokens: {usage.prompt_tokens} in, {usage.completion_tokens} out")
        case ErrorEvent(message=message):
            print(f"Error: {message}")
```

Reasoning and usage availability depend on the provider, HTTP contract, and
model. Do not print both chunks and completed text unless you want to display
the answer twice. See the [Events Guide](../guide/events.md).

## Configuration Options

| Setting | Where to pass it | Description |
|---------|------------------|-------------|
| `provider` | `Agent(...)` | Required provider instance |
| `session_manager` | `Agent(...)` | Required conversation storage |
| `system_prompt` | `Agent(...)` | Instructions for the agent |
| `tools` | `Agent(...)` | List of Python callable functions |
| `streaming` | `Agent(...)` | Enable streaming; defaults to `False` |
| `max_tool_rounds` | `Agent(...)` | Limit the tool execution loop |
| `max_tokens`, `temperature` | `GenerationConfig(...)` | Per-run generation settings, subject to model support |

```python
from nagents import DoneEvent, GenerationConfig

async for event in agent.run(
    "Explain Python generators",
    config=GenerationConfig(max_tokens=2048, temperature=0.7),
):
    if isinstance(event, DoneEvent):
        print(event.final_text)
```

## Resource and Session Management

Always close the agent in `finally` to release provider connections. If you
stop consuming a run early, explicitly close the iterator as well:

```python
from contextlib import aclosing
from nagents import ErrorEvent, TextChunkEvent

async with aclosing(agent.run("Tell me a joke")) as events:
    async for event in events:
        if isinstance(event, ErrorEvent):
            print(event.message)
            break
        if isinstance(event, TextChunkEvent):
            print(event.chunk, end="", flush=True)
```

For manual session operations, use
`await session_manager.get_or_create_session(session_id, user_id)` and
`await session_manager.delete_session(session_id)`. `list_sessions()` returns
session dictionaries and accepts an optional `user_id`, not an `older_than`
filter. Apply your application's retention policy to those records before
deleting anything.

## Next Steps

- Compare models with [Multi-Provider](multi-provider.md) examples
- Add capabilities with [Tool Usage](tool-usage.md)
- See the [Sessions Guide](../guide/sessions.md) and [Agent API](../api/agent.md)
