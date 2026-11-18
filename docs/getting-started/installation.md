# Choose an installation

There are three related products. Choose the one you want before installing:

| Product | Install path | What it provides |
| --- | --- | --- |
| **Native Rust ngn bundle** | [Native installer and usage](../guide/native-ngn.md) | Rust CLI, Ratatui TUI, local browser host, and an embedded Python agent runtime. No end-user Python/pip, Rust, Node or compiler installation. Linux x86_64 (glibc 2.39+) and macOS 11+ (arm64, x86_64). |
| **Python nagents library** | Instructions below | Reusable `Agent`, providers, tools and session APIs; Python `>=3.11, <4.0`. |
| **Existing Python ngn client** | [Python client installation](../guide/ngn-installation.md) | Feature-rich Textual TUI, React web client, plugins, voice, channels and other Python harness integrations through optional extras. |

!!! warning "Native release availability"
    The self-contained installer starts at **0.100.2**. Its download URLs require
    the official `v0.100.2` assets; a source checkout is not publication evidence.
    Until publication passes, the URLs will not work. Native `0.100.0` and `0.100.1`
    remain unchanged and do not contain this bundle/installer. The native guide
    includes an available-now source-development route.

Both applications install a command named `ngn`, but their configuration, login
commands and features are **not interchangeable**. The native installer refuses
to overwrite a foreign/Python `ngn`; use a separate bin directory. The Python
library and existing client remain available; the native bundle is not a claim
of full parity or a production replacement.

## Install the Python library from PyPI

No credentials are needed to install or import the library. Live model calls
need credentials accepted by the selected endpoint; local test/demo workflows
do not need a paid provider account.

Create a **new** environment, or keep using your existing environment manager.
Do not recreate an existing `.venv` or let Poetry and another tool independently
manage it. The following directory must not already belong to another workflow:

```sh
python3 -m venv .venv-nagents
. .venv-nagents/bin/activate
python -m pip install nagents
```

In an existing Poetry project, use `poetry add nagents` instead. The base package
has three direct runtime dependencies: `aiohttp>=3.14.4`, `aiosqlite>=0.20.0`,
and `PyYAML>=6.0`. It avoids mandatory provider SDKs; it is **not dependency-free**.

## Optional Python extras

Install only what you need in the selected Python environment. Quote extras so
the shell does not expand brackets:

```sh
python -m pip install 'nagents[tui]'
# For multiple features, for example:
python -m pip install 'nagents[tui,voice,web]'
```

| Extra | Purpose |
| --- | --- |
| `tui` | Textual terminal client. Not required for the library or headless CLI. |
| `web` | Python web server and media dependencies; published wheels include the React assets. |
| `voice` | Local audio/voice dependencies, including sounddevice and aiortc; may need host PortAudio/audio support. |
| `a2a` | Optional A2A SDK/server/authentication integration. |
| `dev` | Testing (including pytest-asyncio), linting, typing and pre-commit tools, plus test integrations. |
| `docs` | MkDocs, Material and mkdocstrings. |

`'nagents[dev,docs]'` means development and documentation extras, **not all
extras**. The authoritative list is
[`pyproject.toml`](https://github.com/abi-jey/nagents/blob/main/pyproject.toml).
Optional `azure-identity` and `python-dotenv` can be installed separately when
your application uses them; neither is a mandatory base dependency.

## Python source development

```sh
git clone https://github.com/abi-jey/nagents.git
cd nagents
poetry install -E dev -E docs
poetry run python -c 'from nagents import Agent, Provider, ProviderType, SessionManager; print("Imports OK")'
```

Or, in an already selected virtual environment, use
`python -m pip install -e '.[dev,docs]'`. Editable Python source changes are
visible on the next launch; dependency/extra changes require reinstalling.
Keep existing Poetry environments and lockfiles intact. See
[contributing](../development/contributing.md) for Python tests, the separate
native build prerequisites, and the optional React development build.

## Verify imports without model access

```python
from importlib.metadata import version
from nagents import Agent, Provider, ProviderType, SessionManager

print(version("nagents"))
print("nagents imports successfully")
```

## Credentials only when needed

Pass credentials explicitly in library code, usually from an environment
variable; see [configuration](configuration.md) and [providers](../guide/providers.md).
The library and both clients do **not** automatically load a `.env` file. If
your own application deliberately uses one, install `python-dotenv` in its
virtual environment and call `load_dotenv()` explicitly. Never commit keys.
Native installation and `ngn doctor` require no provider login.
