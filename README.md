# nagents

[![PyPI version](https://img.shields.io/pypi/v/nagents.svg)](https://pypi.org/project/nagents/)
[![Python versions](https://img.shields.io/pypi/pyversions/nagents.svg)](https://pypi.org/project/nagents/)
[![CI](https://github.com/abi-jey/nagents/actions/workflows/ci.yml/badge.svg)](https://github.com/abi-jey/nagents/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/abi-jey/nagents/branch/main/graph/badge.svg)](https://codecov.io/gh/abi-jey/nagents)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docs](https://img.shields.io/badge/docs-mkdocs-blue.svg)](https://abi-jey.github.io/nagents/)

A lightweight LLM agent framework with direct HTTP-based provider integration.

## Rust harness migration

This repository also contains **[ngn, the Rust harness, TUI and web host](ngn/README.md)**
under `ngn/`. The Python `nagents` library remains under `src/nagents/`, with its
existing interfaces and independently usable APIs. The two communicate through a private,
versioned runtime protocol; Python does not own Rust tool execution or approvals.

The library, native application and adapter share one version and one GitHub
release. The Rust client is **not yet a feature-complete replacement** for the Python
client below; see the [migration checklist](ngn/docs/migration.md).

## Install native ngn

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh
ngn doctor
```

Self-contained (bundled Python, core, adapter and dependencies) for Linux x86_64 (glibc 2.39+) and macOS 11+ (Apple silicon or Intel).
It never overwrites an existing Python `ngn`; see the
[native installer guide](docs/guide/native-ngn.md) for options, configuration and updates.

## Python library and client features

- **Multi-Provider Support**: OpenAI, Anthropic Claude, and Google Gemini APIs
- **Streaming Events**: Real-time text chunks, tool calls, and usage statistics
- **Tool Execution**: Register Python functions as tools with automatic schema generation
- **Session Management**: SQLite-based conversation persistence
- **Batch Processing**: Process multiple requests efficiently
- **Python Harness Extensions**: Custom context transforms, lifecycle hooks, and replaceable compaction algorithms
- **Optional Terminal Client**: `ngn` provides interactive coding, approvals, sessions, and headless JSON events
- **Minimal Dependencies**: Three direct dependencies: `aiohttp`, `aiosqlite`, and `PyYAML`; no mandatory provider SDK

## Library Installation

Use a Python 3.12–3.15 virtual environment, or `poetry add nagents` in an
existing Poetry project. Do not replace an existing environment:

```sh
python -m pip install nagents
```

## Python ngn terminal client

`ngn` is an optional terminal and headless coding harness, available in published
distributions with the `tui` extra. Published wheels also include `ngn serve`,
the `web` extra, and built frontend assets, verified in
[v0.15.0](https://github.com/abi-jey/nagents/releases/tag/v0.15.0). This README
describes the **current source checkout**, which may include newer changes. See
[release availability](docs/guide/ngn-installation.md#release-availability) when
checking the commands and fixes in an installed distribution.

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

For a real model, select a named connection in `providers.json`, set its referenced
environment variable, and launch through the same environment, for example
`poetry run ngn --provider-id my-connection --model MODEL_ID`.
With the environment activated, use `ngn run --json "your prompt"` for headless
integration. Non-interactive approvals fail closed.

Type `/` for selectable commands from the harness, skills, and trusted plugins.
Shift+Enter inserts a newline; Tab navigates widgets. Use `/agent` or `--agent`
to select a configured profile; `assistant` is the default. Input queues while work
is active, or use `--submit-mode interrupt`. The default `terminal` theme inherits
terminal colors; try `--theme graphite`, `ocean`, or `ember`, and disable motion
with `--no-animations`. Delegation never exceeds the parent's permissions and defaults to depth two
(root 0, child 1, grandchild 2).

For local Live voice, install `nagents[tui,voice]` (or
`poetry install -E tui -E voice`). Open `/voice settings` to enable it and choose
an API-key, Foundry, or eligible ChatGPT/Codex connection, then use **Ctrl+G** or
`/voice start`. `/voice mute` toggles the microphone, `/voice devices` selects
local audio devices, and `/voice stop` ends the call without stopping assistant
work. Voice shares the web UI's settings and uses the current assistant's tools
and approvals; no browser or web server is needed. Use headphones to avoid echo.
See [terminal Voice](docs/guide/ngn.md#live-voice) for setup and limitations.

For ChatGPT/Codex subscription access, select a named OpenAI connection with
`"auth": "chatgpt"` or `"auth": "codex"` in `providers.json`. Use `/login`
or `ngn login chatgpt` for the ChatGPT device flow. Device login must
be enabled in your ChatGPT security/workspace settings. This uses a separate
Codex Responses route, not a general-purpose OpenAI API key. `ngn login` also
selects OpenRouter through a browser sign-in or stores API keys for OpenAI,
Anthropic, Gemini, and custom OpenAI-compatible endpoints; keys are entered
hidden or referenced with `--api-key-env`, and `ngn login --status`/`ngn logout`
manage stored credentials. A saved login does not select the active connection;
choose it in Provider connections or `providers.json`.

Configuration uses JSON with separate `config.json` and `providers.json` files.
Priority is built-ins < `NGN_*` environment defaults
< global JSON < trusted project JSON < explicit JSON < CLI. The global file is
`~/.config/ngn/config.json` (respecting `XDG_CONFIG_HOME`); workspace
`.ngn/config.json` needs `--trust-project`, or explicit selection with `--config`.
Any explicitly selected config file is trusted.
See the [full schema and recipes](docs/guide/ngn-configuration.md) for types,
defaults, ranges, profile replacement, relative paths, and LiteLLM endpoints.

Python plugins can change how the agent builds context, compacts history, and
executes tools. The TUI uses the same harness as the headless client; Textual
remains an optional dependency. See the [ngn usage guide](docs/guide/ngn.md),
[custom Python behavior example](examples/harness/custom_behavior.py), and
[upstream research](docs/development/harness-research.md).

## Python web client and HTTP API

`ngn serve` runs the React client and HTTP API for the ngn coding harness,
sharing its workspace sessions, tools, and approvals. It is also the Docker
image's default command. Install `nagents[web]` for a published wheel with built
frontend assets, or build an editable checkout. See the
[web client guide](docs/guide/ngn-web.md).

The frontend source lives in `src/nagents/web-ui/`; its Vite build goes to
`src/nagents/web/static/`. Plain `ngn serve` uses the bundled assets without
building or reloading. Use `ngn serve --dev` in an editable checkout to build stale
assets and reload after frontend or Python edits. Source build/check commands are in
[Contributing](docs/development/contributing.md#web-client-development).
For the runtime image's no-config startup, mounted YAML and Kubernetes Secret or
environment-variable examples, see the [container configuration guide](docs/guide/ngn-container.md).

The web client is a trusted-operator tool, not a sandbox or multi-user service.

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
        model="gpt-6-luna",
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

nagents integrates with OpenAI, Anthropic, Google Gemini and OpenAI-compatible
services. Select an exact model ID offered by your account and endpoint; example
IDs are not a current catalog or an entitlement guarantee.

| Provider documentation | Library type |
| --- | --- |
| [OpenAI](https://developers.openai.com/api/docs/models) | `ProviderType.OPENAI_COMPATIBLE` |
| [Anthropic](https://platform.claude.com/docs/en/models/overview) | `ProviderType.ANTHROPIC` |
| [Google Gemini](https://ai.google.dev/gemini-api/docs/models) | `ProviderType.GEMINI_NATIVE` |

For OpenAI-compatible
connections, `await provider.get_model_list()` discovers the endpoint's catalog;
see [model discovery and provider-specific limits](docs/guide/providers.md#discover-model-ids).

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

- [Native ngn installer and usage](docs/guide/native-ngn.md)
- [Native migration and safety](docs/guide/native-ngn-migration.md)
- [Shared-version release workflow](docs/guide/releases.md)
- [ngn Installation](docs/guide/ngn-installation.md)
- [API Changes in 0.23](docs/guide/api-changes.md)
- [ngn Usage Guide](docs/guide/ngn.md)
- [ngn Configuration Reference](docs/guide/ngn-configuration.md)
- [ngn Local Web Client](docs/guide/ngn-web.md)
- [Installation](https://abi-jey.github.io/nagents/getting-started/installation/)
- [Quick Start](https://abi-jey.github.io/nagents/getting-started/quickstart/)
- [Providers Guide](https://abi-jey.github.io/nagents/guide/providers/)
- [Tools Guide](https://abi-jey.github.io/nagents/guide/tools/)
- [API Reference](https://abi-jey.github.io/nagents/api/agent/)

## License

MIT
