# nagents

[![PyPI version](https://img.shields.io/pypi/v/nagents.svg)](https://pypi.org/project/nagents/)
[![Python versions](https://img.shields.io/pypi/pyversions/nagents.svg)](https://pypi.org/project/nagents/)
[![CI](https://github.com/abi-jey/nagents/actions/workflows/ci.yml/badge.svg)](https://github.com/abi-jey/nagents/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A lightweight Python agents library with direct HTTP-based provider integration,
an existing feature-rich Python client, and a separate native Rust harness.

## Choose your starting point

| I want… | Start here |
| --- | --- |
| The native Rust CLI/TUI/browser bundle, without installing Python or build tools | [Native installer and usage](guide/native-ngn.md) |
| Python APIs for providers, agents, tools and sessions | [Python library installation](getting-started/installation.md#install-the-python-library-from-pypi) and [quick start](getting-started/quickstart.md) |
| The existing Textual/React client, voice, plugins and integrations | [Python ngn client installation](guide/ngn-installation.md) |

!!! warning "Native installer release status"
    The self-contained installer is **0.100.2 development, not yet published**.
    The guide's version-bound GitHub download commands will work only after
    the official `v0.100.2` release. It also documents a source-development
    route available now. Native bundles initially support Linux x86_64/glibc
    2.39+; the Python client has its own support matrix.

Both clients use the name `ngn`, but configuration, auth and feature sets differ.
The native installer refuses to overwrite an existing Python/foreign command.
It does not migrate legacy state or claim full parity; see
[native migration and safety](guide/native-ngn-migration.md). Existing Python
library and client documentation remains available below.

---

<div class="grid cards" markdown>

-   :material-lightning-bolt:{ .lg .middle } __Minimal Dependencies__

    ---

    Three direct base dependencies: `aiohttp`, `aiosqlite` and `PyYAML`. No mandatory provider SDKs.

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

## Python library features

- [x] **Multi-Provider Support** - OpenAI, Anthropic Claude, and Google Gemini APIs
- [x] **Streaming Events** - Real-time text chunks, tool calls, and usage statistics
- [x] **Tool Execution** - Register Python functions as tools with automatic schema generation
- [x] **Session Management** - SQLite-based conversation persistence
- [x] **Context Compaction** - Automatic summarization to stay within context limits
- [x] **Batch Processing** - Process multiple requests efficiently
- [x] **HTTP Logging** - Debug with full HTTP/SSE traffic logging
- [x] **Tool Hallucination Handling** - Graceful handling of unknown tool calls

## Python library quick start

Install the [Python package](getting-started/installation.md) first. Set
`OPENAI_API_KEY` and `OPENAI_MODEL` securely for your own account before running
this model-calling example; importing the library requires neither.

```python title="main.py"
import asyncio
import os
from pathlib import Path
from nagents import Agent, ErrorEvent, Provider, ProviderType, SessionManager, TextChunkEvent


async def main():
    # Create a provider
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
    )

    # Create session manager for conversation persistence
    session_manager = SessionManager(Path("sessions.db"))

    # Create an agent
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        streaming=True,
    )

    # Run a conversation
    try:
        async for event in agent.run("Hello, how are you?"):
            if isinstance(event, TextChunkEvent):
                print(event.chunk, end="", flush=True)
            elif isinstance(event, ErrorEvent):
                print(f"\nError: {event.message}")
    finally:
        await agent.close()


asyncio.run(main())
```

## Python package installation

Use a new or existing selected virtual environment, not a global pip install.
Do not recreate an environment already managed by Poetry or another tool.

=== "pip (Recommended)"

    ```bash
    python3 -m venv .venv-nagents
    . .venv-nagents/bin/activate
    python -m pip install nagents
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

| Provider | Type | Model selection |
|----------|------|--------|
| OpenAI | `ProviderType.OPENAI` / `OpenAIProvider` | A model available to your API-key or separate ChatGPT connection |
| Anthropic | `ProviderType.ANTHROPIC` | An account-supported model ID |
| Google | `ProviderType.GEMINI_NATIVE` | An account-supported model ID |
| Compatible services, gateways and Azure | See [providers](guide/providers.md) | Your endpoint's model ID or deployment name |

This is not an exhaustive or current upstream model catalog. Use explicit
[model discovery](guide/providers.md#explicit-catalog-discovery) where supported;
catalog visibility does not guarantee generation entitlement or capabilities.

!!! tip "OpenAI Compatible"
    `OPENAI_COMPATIBLE` supports compatible services such as local Ollama/vLLM
    endpoints; protocol and tool/media capabilities depend on that service.
    Azure also has explicit provider routes; see the provider guide rather than
    assuming all endpoints/authentication modes are interchangeable.

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
