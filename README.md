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

## Library Installation

```bash
pip install nagents
```

## ngn Terminal Client

`ngn` is an optional terminal and headless coding harness in this **source
checkout**, not yet a published CLI release. `pip install nagents` installs the
published library; do not assume it includes these unreleased CLI features.

Keep the existing Poetry workflow from the repository root:

```bash
poetry install -E dev -E tui
poetry run ngn --demo
```

For a new local editable environment with uv, from the repository root:

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python -e '.[tui]'
uv run --no-project --python .venv/bin/python ngn --demo
```

Do not recreate an existing Poetry-managed `.venv`; keep using Poetry or choose
a separate local environment. This uv workflow does not require `uv sync`,
`uv lock`, or changes to the existing `uv.lock`. The explicit interpreter needs
no activation. See [ngn installation](docs/guide/ngn-installation.md) for optional
activation, headless-only installation, and isolated `uvx`/`uv tool` alternatives.

The offline demo needs no API key, makes no model requests, skips Python plugins,
and performs no workspace edits or shell commands. It does save demo
conversations locally. Send `demo approval` to try a change-preview dialog or
`demo subagents` to exercise three native async jobs without provider calls.

For a real model, set the provider key outside TOML and launch through the same
environment, for example `poetry run ngn --auth api-key --model MODEL_ID` or
`uv run --no-project --python .venv/bin/python ngn --auth api-key --model MODEL_ID`.
With the environment activated, use `ngn run --json "your prompt"` for headless
integration. Non-interactive approvals fail closed.

Type `/` for selectable commands from the harness, skills, and trusted plugins.
Shift+Enter inserts a newline; Tab cycles agent profiles. Input queues while work
is active, or use `--submit-mode interrupt`. The default `terminal` theme inherits
terminal colors; try `--theme graphite`, `ocean`, or `ember`, and disable motion
with `--no-animations`. Build-capable delegation uses the built-in `agent` profile
by default, never exceeds the parent's permissions, and defaults to depth two
(root 0, child 1, grandchild 2).

For ChatGPT/Codex subscription access, start `ngn` without `--demo` and use
`/login`, or run `ngn login --device-auth`. Device login must be enabled in your
ChatGPT security/workspace settings. This uses a separate Codex Responses route,
not a general-purpose OpenAI API key. See the guide for credential storage and
`--auth api-key` selection. Saved OAuth tokens are never sent to custom endpoints.

Configuration uses top-level TOML, **not `[ngn]` sections**. Priority is built-ins
< `NGN_*` environment defaults < global TOML < trusted project TOML < explicit
TOML < CLI. The global file is `~/.config/ngn/config.toml` (respecting
`XDG_CONFIG_HOME`); workspace `.ngn/config.toml` needs `--trust-project`, or explicit
selection with `--config`. Any explicitly selected config file is trusted.
See the [full schema and recipes](docs/guide/ngn-configuration.md) for types,
defaults, ranges, profile replacement, relative paths, and LiteLLM endpoints.

Optional microphone dictation uses the `voice` extra, a separate transcription
API-key reference, and explicit opt-in. Transcriptions become editable previews,
not automatically sent prompts. ChatGPT subscription access does not cover the
paid transcription API. See [dictation](docs/guide/ngn.md#dictation).

Python plugins can change how the agent builds context, compacts history, and
executes tools. The TUI uses the same harness as the headless client; Textual
remains an optional dependency. See the [ngn usage guide](docs/guide/ngn.md),
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

- [ngn Installation](docs/guide/ngn-installation.md)
- [ngn Usage Guide](docs/guide/ngn.md)
- [ngn Configuration Reference](docs/guide/ngn-configuration.md)
- [Installation](https://abi-jey.github.io/nagents/getting-started/installation/)
- [Quick Start](https://abi-jey.github.io/nagents/getting-started/quickstart/)
- [Providers Guide](https://abi-jey.github.io/nagents/guide/providers/)
- [Tools Guide](https://abi-jey.github.io/nagents/guide/tools/)
- [API Reference](https://abi-jey.github.io/nagents/api/agent/)

## License

MIT
