# nagents

A lightweight, dependency-free LLM agent framework with direct HTTP-based provider integration.

## Features

- **Multi-Provider Support**: OpenAI, Anthropic Claude, and Google Gemini APIs
- **Streaming Events**: Real-time text chunks, tool calls, and usage statistics
- **Tool Execution**: Register Python functions as tools with automatic schema generation
- **Session Management**: SQLite-based conversation persistence
- **Batch Processing**: Process multiple requests efficiently
- **Minimal Dependencies**: Only `aiohttp` and `aiosqlite` required

## Quick Start

```python
import asyncio
from nagents import Agent, Provider, ProviderType

async def main():
    # Create a provider
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="your-api-key",
        model="gpt-4o-mini",
    )

    # Create an agent
    agent = Agent(provider=provider)

    # Run a conversation
    async for event in agent.run("Hello, how are you?"):
        if hasattr(event, 'chunk'):
            print(event.chunk, end="")

    await agent.close()

asyncio.run(main())
```

## Installation

```bash
pip install nagents
```

## Providers

nagents supports three provider types:

| Provider | Type | Models |
|----------|------|--------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | gpt-4o, gpt-4o-mini, etc. |
| Anthropic | `ProviderType.ANTHROPIC` | claude-3-5-sonnet, claude-3-opus, etc. |
| Google | `ProviderType.GEMINI_NATIVE` | gemini-2.0-flash, gemini-1.5-pro, etc. |

## Documentation

- [Installation](getting-started/installation.md)
- [Quick Start](getting-started/quickstart.md)
- [Providers Guide](guide/providers.md)
- [Tools Guide](guide/tools.md)
- [API Reference](api/agent.md)
