# nagents

[![PyPI version](https://img.shields.io/pypi/v/nagents.svg)](https://pypi.org/project/nagents/)
[![Python versions](https://img.shields.io/pypi/pyversions/nagents.svg)](https://pypi.org/project/nagents/)
[![CI](https://github.com/abi-jey/nagents/actions/workflows/ci.yml/badge.svg)](https://github.com/abi-jey/nagents/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/abi-jey/nagents/branch/main/graph/badge.svg)](https://codecov.io/gh/abi-jey/nagents)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue.svg)](https://abi-jey.github.io/nagents/)

A lightweight LLM agent framework with direct HTTP-based provider integration.

## Features

- **Multi-Provider Support**: OpenAI, Anthropic Claude, and Google Gemini APIs
- **Streaming Events**: Real-time text chunks, tool calls, and usage statistics
- **Tool Execution**: Register Python functions as tools with automatic schema generation
- **Session Management**: SQLite-based conversation persistence
- **Batch Processing**: Process multiple requests efficiently
- **Python Harness Extensions**: Custom context transforms, lifecycle hooks, and replaceable compaction algorithms
- **Optional Terminal Client**: `ngn` provides interactive coding, approvals, sessions, and headless JSON events
- **Minimal Dependencies**: Only `aiohttp` and `aiosqlite` required

## Installation

```bash
pip install nagents
```

## ngn Terminal Client

Try the terminal harness from this checkout:

```bash
poetry install -E dev -E tui
poetry run ngn --demo
```

The offline demo needs no API key and performs no workspace edits or shell
commands. Send `demo approval` to try a change-preview dialog. For a real model,
set your provider key in the environment and run `poetry run ngn --model MODEL_ID`.
Use `ngn run --json "your prompt"` for headless integration.

Python plugins can change how the agent builds context, compacts history, and
executes tools. The TUI uses the same harness as the headless client; Textual
remains an optional dependency. See the [ngn guide](docs/guide/ngn.md),
[custom Python behavior example](examples/harness/custom_behavior.py), and
[upstream research](docs/development/harness-research.md).

## Quick Start

```python
import asyncio
from pathlib import Path
from nagents import Agent, Provider, ProviderType, SessionManager


async def main():
    # Create a provider
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="your-api-key",
        model="gpt-4o-mini",
    )

    # Create an agent
    agent = Agent(
        provider=provider,
        session_manager=SessionManager(Path("sessions.db")),
        streaming=True,
    )

    # Run a conversation
    async for event in agent.run("Hello, how are you?"):
        if hasattr(event, "chunk"):
            print(event.chunk, end="")

    await agent.close()


asyncio.run(main())
```

## Providers

nagents supports three provider types:

| Provider | Type | Models |
|----------|------|--------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | gpt-4o, gpt-4o-mini, etc. |
| Anthropic | `ProviderType.ANTHROPIC` | claude-3-5-sonnet, claude-3-opus, etc. |
| Google | `ProviderType.GEMINI_NATIVE` | gemini-2.0-flash, gemini-1.5-pro, etc. |

## With Tools

```python
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Weather in {city}: Sunny, 22°C"


agent = Agent(
    provider=provider,
    session_manager=SessionManager(Path("sessions.db")),
    tools=[get_weather],
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

# Use a specific session ID for conversation continuity
async for event in agent.run("Remember my name is Alice", session_id="user-123"):
    ...
```

## Documentation

- [Installation](https://abi-jey.github.io/nagents/getting-started/installation/)
- [Quick Start](https://abi-jey.github.io/nagents/getting-started/quickstart/)
- [Providers Guide](https://abi-jey.github.io/nagents/guide/providers/)
- [Tools Guide](https://abi-jey.github.io/nagents/guide/tools/)
- [API Reference](https://abi-jey.github.io/nagents/api/agent/)

## License

MIT
