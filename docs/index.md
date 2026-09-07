# nagents

[![PyPI version](https://img.shields.io/pypi/v/nagents.svg)](https://pypi.org/project/nagents/)
[![Python versions](https://img.shields.io/pypi/pyversions/nagents.svg)](https://pypi.org/project/nagents/)
[![CI](https://github.com/abi-jey/nagents/actions/workflows/ci.yml/badge.svg)](https://github.com/abi-jey/nagents/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A lightweight, dependency-free LLM agent framework with direct HTTP-based provider integration.

---

<div class="grid cards" markdown>

-   :material-lightning-bolt:{ .lg .middle } __Minimal Dependencies__

    ---

    Only `aiohttp` and `aiosqlite` required. No heavy SDKs.

-   :material-swap-horizontal:{ .lg .middle } __Multi-Provider__

    ---

    OpenAI, Anthropic Claude, and Google Gemini with unified API.

-   :material-tools:{ .lg .middle } __Tool Execution__

    ---

    Register Python functions as tools with automatic schema generation.

-   :material-database:{ .lg .middle } __Session Persistence__

    ---

    SQLite-based conversation history with session management.

</div>

---

## Features

- [x] **Multi-Provider Support** - OpenAI, Anthropic Claude, and Google Gemini APIs
- [x] **Streaming Events** - Real-time text chunks, tool calls, and usage statistics
- [x] **Tool Execution** - Register Python functions as tools with automatic schema generation
- [x] **Session Management** - SQLite-based conversation persistence
- [x] **Context Compaction** - Automatic summarization to stay within context limits
- [x] **Batch Processing** - Process multiple requests efficiently
- [x] **HTTP Logging** - Debug with full HTTP/SSE traffic logging
- [x] **Tool Hallucination Handling** - Graceful handling of unknown tool calls

## Quick Start

```python title="main.py"
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

    # Create session manager for conversation persistence
    session_manager = SessionManager(Path("sessions.db"))

    # Create an agent
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
    )

    # Run a conversation
    async for event in agent.run("Hello, how are you?"):
        if hasattr(event, "chunk"):
            print(event.chunk, end="")

    await agent.close()


asyncio.run(main())
```

## Installation

=== "pip (Recommended)"

    ```bash
    pip install nagents
    ```

=== "uv"

    ```bash
    uv add nagents
    ```

=== "poetry"

    ```bash
    poetry add nagents
    ```

## Providers

nagents supports multiple LLM providers with a unified interface:

| Provider | Type | Models |
|----------|------|--------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | gpt-4o, gpt-4o-mini, etc. |
| Anthropic | `ProviderType.ANTHROPIC` | claude-3-5-sonnet, claude-3-opus, etc. |
| Google | `ProviderType.GEMINI_NATIVE` | gemini-2.0-flash, gemini-1.5-pro, etc. |

!!! tip "OpenAI Compatible"
    The `OPENAI_COMPATIBLE` provider works with any OpenAI-compatible API including Azure OpenAI, local models (Ollama, vLLM), and other compatible services.

## Next Steps

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } __Getting Started__

    ---

    Install nagents and create your first agent in minutes.

    [:octicons-arrow-right-24: Installation](getting-started/installation.md)

-   :material-book-open-variant:{ .lg .middle } __User Guide__

    ---

    Learn about providers, tools, sessions, and events.

    [:octicons-arrow-right-24: Providers](guide/providers.md)

-   :material-code-tags:{ .lg .middle } __Examples__

    ---

    See complete working examples with tools and sessions.

    [:octicons-arrow-right-24: Quick Start](getting-started/quickstart.md)

</div>
