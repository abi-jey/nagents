# Quick Start

This guide will help you get started with nagents in minutes.

## Basic Usage

### 1. Create a Provider

First, create a provider with your API credentials:

=== "OpenAI"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="sk-...",
        model="gpt-4o-mini",
    )
    ```

=== "Anthropic Claude"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.ANTHROPIC,
        api_key="sk-ant-...",
        model="claude-3-5-sonnet-20241022",
    )
    ```

=== "Google Gemini"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.GEMINI_NATIVE,
        api_key="...",
        model="gemini-2.0-flash",
    )
    ```

### 2. Create a Session Manager

!!! important "Required Component"
    The `session_manager` parameter is **required** when creating an Agent. It handles conversation persistence.

```python
from pathlib import Path
from nagents import SessionManager

session_manager = SessionManager(Path("sessions.db"))
```

### 3. Create an Agent

```python
from nagents import Agent

agent = Agent(
    provider=provider,
    session_manager=session_manager,
    system_prompt="You are a helpful assistant.",  # (1)!
)
```

1. The system prompt is optional but recommended to define the agent's behavior.

### 4. Run a Conversation

```python
import asyncio


async def main():
    async for event in agent.run("What is the capital of France?"):
        if hasattr(event, "chunk"):
            print(event.chunk, end="", flush=True)
    print()  # newline at end

    await agent.close()


asyncio.run(main())
```

---

## Complete Example

Here's a complete, runnable example:

```python title="basic_agent.py" linenums="1"
import asyncio
from pathlib import Path
from nagents import Agent, Provider, ProviderType, SessionManager


async def main():
    # 1. Create provider
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="sk-...",
        model="gpt-4o-mini",
    )

    # 2. Create session manager
    session_manager = SessionManager(Path("sessions.db"))

    # 3. Create agent
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
    )

    # 4. Run conversation
    async for event in agent.run("Hello! What can you help me with?"):
        if hasattr(event, "chunk"):
            print(event.chunk, end="", flush=True)
    print()

    await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## With Tools

Define Python functions as tools that the agent can call:

```python title="agent_with_tools.py" hl_lines="4-11 19"
from nagents import Agent, Provider, ProviderType, SessionManager
from pathlib import Path


def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"The weather in {city} is sunny, 22°C"


def calculate(expression: str) -> str:
    """Evaluate a mathematical expression."""
    return str(eval(expression))


async def main():
    provider = Provider(...)
    session_manager = SessionManager(Path("sessions.db"))

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_weather, calculate],  # (1)!
    )

    async for event in agent.run("What's the weather in Paris?"):
        ...
```

1. Pass functions directly - nagents automatically generates the tool schema from type hints and docstrings.

!!! tip "Tool Documentation"
    Always include docstrings in your tool functions! The LLM uses them to understand when and how to call each tool.

---

## With Session Persistence

Sessions allow the agent to remember previous conversations:

```python title="persistent_sessions.py" hl_lines="10-11 17-18"
from pathlib import Path
from nagents import Agent, SessionManager, Provider, ProviderType


async def main():
    provider = Provider(...)
    session_manager = SessionManager(Path("sessions.db"))

    agent = Agent(provider=provider, session_manager=session_manager)

    # First conversation - use a specific session ID
    async for event in agent.run(
        "Remember my name is Alice",
        session_id="user-123",  # (1)!
    ):
        ...

    # Later, continue the same session
    async for event in agent.run(
        "What's my name?",
        session_id="user-123",  # (2)!
    ):
        # Agent will remember: "Your name is Alice"
        ...
```

1. Use a consistent session ID to maintain conversation history.
2. Same session ID = same conversation context.

---

## Handling Events

nagents emits various event types during generation:

```python title="event_handling.py"
from nagents import (
    TextChunkEvent,
    TextDoneEvent,
    ToolCallEvent,
    ToolResultEvent,
    ErrorEvent,
    DoneEvent,
)

async for event in agent.run("Hello"):
    match event:
        case TextChunkEvent(chunk=chunk):
            print(chunk, end="")

        case ToolCallEvent(name=name, arguments=args):
            print(f"\n:material-tools: Calling tool: {name}")

        case ToolResultEvent(result=result):
            print(f":material-check: Tool result: {result}")

        case ErrorEvent(message=msg):
            print(f":material-alert: Error: {msg}")

        case DoneEvent(final_text=text, usage=usage):
            print(f"\n---")
            print(f"Total tokens: {usage.total_tokens}")
            if usage.session:
                print(f"Session tokens: {usage.session.total_tokens}")
```

??? info "Event Types Reference"

    | Event | Description |
    |-------|-------------|
    | `TextChunkEvent` | Streaming text chunk |
    | `TextDoneEvent` | Complete text (non-streaming) |
    | `ToolCallEvent` | Model is calling a tool |
    | `ToolResultEvent` | Tool execution completed |
    | `ErrorEvent` | Error occurred |
    | `DoneEvent` | Generation complete |

---

## Next Steps

<div class="grid cards" markdown>

-   :material-cog:{ .lg .middle } __Configuration__

    ---

    Learn about generation parameters and HTTP logging.

    [:octicons-arrow-right-24: Configuration](configuration.md)

-   :material-tools:{ .lg .middle } __Tools Guide__

    ---

    Create powerful tools for your agent.

    [:octicons-arrow-right-24: Tools](../guide/tools.md)

-   :material-database:{ .lg .middle } __Sessions Guide__

    ---

    Manage conversation persistence.

    [:octicons-arrow-right-24: Sessions](../guide/sessions.md)

</div>
