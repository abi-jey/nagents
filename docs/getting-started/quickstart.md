# Quick Start

This guide will help you get started with nagents in minutes.

## Basic Usage

### 1. Create a Provider

First, create a provider with your API credentials:

```python
from nagents import Provider, ProviderType

# OpenAI
provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key="sk-...",
    model="gpt-4o-mini",
)

# Anthropic Claude
provider = Provider(
    provider_type=ProviderType.ANTHROPIC,
    api_key="sk-ant-...",
    model="claude-3-5-sonnet-20241022",
)

# Google Gemini
provider = Provider(
    provider_type=ProviderType.GEMINI_NATIVE,
    api_key="...",
    model="gemini-2.0-flash",
)
```

### 2. Create an Agent

```python
from nagents import Agent

agent = Agent(
    provider=provider,
    system_prompt="You are a helpful assistant.",
)
```

### 3. Run a Conversation

```python
import asyncio

async def main():
    async for event in agent.run("What is the capital of France?"):
        if hasattr(event, 'chunk'):
            print(event.chunk, end="", flush=True)
    print()  # newline at end

    await agent.close()

asyncio.run(main())
```

## With Tools

Define Python functions as tools:

```python
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"The weather in {city} is sunny, 22°C"

def calculate(expression: str) -> str:
    """Evaluate a mathematical expression."""
    return str(eval(expression))

agent = Agent(
    provider=provider,
    tools=[get_weather, calculate],
)

async for event in agent.run("What's the weather in Paris?"):
    ...
```

## With Session Persistence

```python
from pathlib import Path
from nagents import SessionManager

session_manager = SessionManager(Path("sessions.db"))

agent = Agent(
    provider=provider,
    session_manager=session_manager,
)

# Use a specific session ID
async for event in agent.run(
    "Remember my name is Alice",
    session_id="user-123",
):
    ...

# Later, in a new conversation
async for event in agent.run(
    "What's my name?",
    session_id="user-123",
):
    # Agent will remember: "Your name is Alice"
    ...
```

## Handling Events

nagents emits various event types during generation:

```python
from nagents import (
    TextChunkEvent,
    TextDoneEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
    ErrorEvent,
    DoneEvent,
)

async for event in agent.run("Hello"):
    if isinstance(event, TextChunkEvent):
        print(event.chunk, end="")
    elif isinstance(event, ToolCallEvent):
        print(f"Calling tool: {event.name}")
    elif isinstance(event, ToolResultEvent):
        print(f"Tool result: {event.result}")
    elif isinstance(event, UsageEvent):
        print(f"Tokens: {event.total_tokens}")
    elif isinstance(event, ErrorEvent):
        print(f"Error: {event.message}")
    elif isinstance(event, DoneEvent):
        print(f"Done! Final text: {len(event.final_text)} chars")
```
