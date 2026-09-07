# Install ngn from source

`ngn` is the terminal and headless client included in the **current source
checkout** of nagents. It is not yet a published CLI release. Installing the
latest `nagents` from PyPI does not guarantee these unreleased commands, extras,
or configuration fields are present, even if its version matches the checkout.
Use a local checkout that actually contains `src/nagents/cli.py` and the `ngn`
entry point in `pyproject.toml`.

This page uses local checkout paths so development launches cannot silently
select the older published package. Choose one environment workflow; do not let Poetry and uv
independently manage the same virtualenv.

## Requirements

- Python `>=3.11, <4.0`; Python 3.13 is used in the uv examples below.
- A POSIX host for the guarded coding tools and shell process-group cleanup.
  Windows is not yet a supported coding-tool host.
- An interactive terminal for the full-screen TUI. Use `ngn run` for pipes,
  scripts, and environments without a terminal.
- The `tui` extra for Textual. Headless use and importing the nagents library do
  not require it.
- A provider account/API key for live model requests, or eligible ChatGPT/Codex
  device login for the default OpenAI route. Neither is needed for `--demo`.

The base runtime dependencies are `aiohttp>=3.9.0` and `aiosqlite>=0.20.0`;
the TUI adds `textual>=8.2.8` and its dependencies. The project uses minimum
version constraints, not a promise that one frozen dependency set is always the
latest. A fresh uv pip install resolves versions satisfying those constraints;
Poetry uses its lockfile. The CLI does not require the OpenAI or Anthropic SDK,
or a heavyweight gateway SDK. Connecting to a LiteLLM server does not install or
start that server.

## macOS notes

The coding harness targets Linux and macOS. CI exercises Python 3.11-3.14 on
`macos-latest`, plus Python 3.13 on an Intel macOS runner. It uses fake audio and
HTTP fixtures: passing CI does not validate a physical microphone, terminal key
mapping, or access to a paid provider account.

- Use a supported Python installation and a UTF-8 terminal. The Poetry and uv
  commands on this page also apply to macOS.
- Shift+Enter requires the terminal to send a distinct key sequence. If it does
  not, use Ctrl+J for a newline. Alt/Option+Enter also works when the terminal is
  configured to send Option as Meta. Command-key shortcuts are not aliases for
  the documented Ctrl-key bindings.
- macOS configuration stays at `~/.config/ngn/config.toml` and state at
  `~/.local/share/ngn`, unless XDG settings override them; ngn does not switch to
  `~/Library/Application Support` on this platform.
- Use consistent on-disk casing when selecting a workspace: case variants can
  currently select different session scopes even on case-insensitive APFS.
  Prefer workspace-relative tool paths. macOS aliases such as `/tmp` versus
  `/private/tmp` are not interchangeable for absolute guarded-tool paths; a custom
  `XDG_DATA_HOME` must use physical, non-symlinked ancestors. The ordinary defaults
  under `/Users/...` avoid these system aliases.
- Dictation requires microphone permission for the application launching ngn,
  such as Terminal, iTerm, or an IDE. Check **System Settings > Privacy & Security
  > Microphone** and the selected audio input device. The macOS `sounddevice`
  wheel normally includes PortAudio; if it cannot load PortAudio, install the
  library with `brew install portaudio` and use a Python/library architecture
  matching your Mac.
- A shell command or trusted plugin can still be OS-specific. ngn does not
  translate Linux utilities into macOS equivalents. The guarded shell uses
  `/bin/sh`, not the user's interactive zsh configuration.

## Existing Poetry workflow

From the repository root, keep the project's existing local workflow:

```bash
poetry install -E dev -E tui
poetry run ngn --demo
```

For a terminal-only environment without development tools, use
`poetry install -E tui` instead. Continue using `poetry run` for that environment:

```bash
poetry run ngn --help
poetry run ngn --version
poetry run ngn run --demo --json "Explain the offline demo"
poetry run ngn doctor --demo
```

Do not recreate an existing Poetry-managed `.venv` with `uv venv`. There is no
need to migrate an environment or rewrite a lockfile just to try ngn.

If the Poetry shell plugin is installed, your existing shell workflow also works:

```bash
poetry shell
ngn --demo
```

This uses the editable checkout in Poetry's environment, not a separate global
`ngn` executable. Without the plugin, `poetry run ngn` needs no shell activation.

## Local editable install with uv

For a new local environment, run these commands **from the repository root**:

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python -e '.[tui]'
uv run --no-project --python .venv/bin/python ngn --demo
```

`-e` installs this checkout editably: Python source changes are reflected on the
next launch without reinstalling. Re-run the install after changing dependency
metadata or extras. Quote `'.[tui]'` so the shell does not expand the brackets.

The explicit interpreter selects the local environment without activation.
`--no-project` prevents `uv run` from treating this as a uv-managed project and
automatically synchronizing project dependencies. These commands do not require
`uv sync`, `uv lock`, or any changes to an existing `uv.lock`. In particular, do
not use project-lock synchronization as an installation step for this workflow.

If `.venv` already belongs to Poetry or another workflow, keep it intact. Either
use that workflow or choose a different local environment explicitly:

```bash
uv venv --python 3.13 .venv-ngn
uv pip install --python .venv-ngn/bin/python -e '.[tui]'
uv run --no-project --python .venv-ngn/bin/python ngn --demo
```

The relative interpreter path is relative to the shell's current directory.
From another directory, use the absolute path instead:

```bash
uv run --no-project --python /absolute/path/nagents/.venv/bin/python ngn --workspace /path/to/project --demo
```

### Optional activation

For a POSIX shell, activation makes the installed `ngn` command directly
available. Only use the `--active` variant **after activating the intended
environment**:

```bash
source .venv/bin/activate
ngn --demo
uv run --no-project --active ngn --demo
```

Without activation, prefer the explicit `--python .venv/bin/python` form.
`--active` does not create or identify the intended environment for you.

### Explicit environment files

ngn does not load `.env` automatically. If you intentionally keep provider
credentials in a local environment file, uv can load it for the launched process:

```bash
uv run --no-project --python .venv/bin/python --env-file .env ngn --provider openrouter --api-key-env OPENROUTER_API_KEY --model openai/gpt-4.1-mini
```

Use only a file you trust. Keep it out of version control and do not include its
contents in logs, screenshots, or bug reports. The filename is a launcher option;
the secret value is never a CLI argument or TOML field.

### Headless-only install

Omit the extra if you do not need the full-screen interface:

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python -e .
uv run --no-project --python .venv/bin/python ngn run --demo --json "Hello"
```

As above, only create `.venv` if it is not already managed by another workflow.

## Optional isolated tools

These alternatives are **not** the default local editable workflow. They are
useful when you intentionally want uv to manage a separate tool environment.
Replace `/absolute/path/nagents` with a local checkout containing this CLI:

```bash
uvx --from '/absolute/path/nagents[tui]' ngn --demo
```

This is a non-editable, built installation in uv's tool cache, not a live link to
your source tree. Do not expect source changes to appear automatically in a
cached invocation. For a fresh, disposable build after source changes, bypass
both the cache and previously installed tool environments:

```bash
uvx --no-cache --isolated --from '/absolute/path/nagents[tui]' ngn --demo
```

This rebuilds and installs dependencies again; prefer the local editable workflow
for repeated development launches. Let concurrent source edits finish before
building a non-editable snapshot.

For an explicitly installed, isolated tool that *does* track local Python source:

```bash
uv tool install --editable '/absolute/path/nagents[tui]'
ngn --demo
```

The checkout must remain at that path. Follow uv's PATH instructions if `ngn` is
not found, and use `uv tool uninstall nagents` to remove that tool installation.
Tool environments are separate from the repository's Poetry or `.venv`
environment; they are not needed just to run from the checkout.

## Optional microphone support

Dictation is opt-in and uses the `voice` extra (`sounddevice>=0.5.6`) for microphone
capture. In the local uv environment:

```bash
uv pip install --python .venv/bin/python -e '.[tui,voice]'
```

With Poetry, select the same extras, retaining `-E dev` if desired:

```bash
poetry install -E tui -E voice
```

`sounddevice` requires a working audio input device and PortAudio support on the
host. On systems where PortAudio is not supplied with the Python package,
install the appropriate system library. Grant microphone access through the
operating system if needed. Headless containers and SSH sessions often have no
usable local microphone; the ordinary text interface needs none of this.

Installing the extra does not enable recording. Configure
`dictation_enabled = true` and the separate transcription API-key reference as
described in [Dictation](ngn.md#dictation). A ChatGPT subscription/login does not
pay for or authorize the transcription API.

## Verify the installation

With the local uv environment, these checks require no provider credentials:

```bash
uv run --no-project --python .venv/bin/python ngn --version
uv run --no-project --python .venv/bin/python ngn --help
uv run --no-project --python .venv/bin/python ngn run --help
uv run --no-project --python .venv/bin/python ngn doctor --demo
uv run --no-project --python .venv/bin/python ngn run --demo --json "Hello"
```

The JSON command should emit JSON Lines with `schema_version: 1` and finish with
a `done` event. `--demo` makes no model requests, runs no shell commands, performs
no workspace edits, and skips Python plugins. It can list visible workspace paths
and saves demo conversations in local state storage. It is not a no-filesystem-I/O
mode. TOML parsing and validation still happen in demo mode.

For an interactive check, start `ngn --demo` through your selected environment,
then try `demo approval`, `demo subagents`, `/help`, and session resume. The
approval is a preview only; approving it does not edit a file.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ngn` is missing or has different flags | Use the selected environment's launcher and install from this source checkout. A published package with the same version number may not contain the unreleased CLI. |
| TUI extra is missing | Install the local checkout with `[tui]`, or use the headless `run` subcommand. |
| TUI needs a terminal | Use a real interactive terminal, or `ngn run --json "prompt"` in a script. |
| uv tries to synchronize the project or use `uv.lock` | Use `uv pip install --python ...` and `uv run --no-project --python ...`; do not substitute `uv sync` or bare project-managed `uv run`. |
| Changes do not appear in an isolated `uvx` run | It is non-editable. Use the local editable workflow for development, or `uvx --no-cache --isolated --from '/absolute/path/nagents[tui]' ngn --demo` for a fresh build. |
| Missing API key | Configure an environment-variable **name** with `api_key_env`, set its value outside TOML, or use eligible OpenAI device login. `--demo` needs neither. |
| A setting in the environment seems ignored | TOML has higher priority than `NGN_*` defaults. Check [precedence](ngn-configuration.md#precedence). |
| New fields or `voice` extra are unavailable | Check that the local source includes the corresponding implementation; the CLI is under active development, not a published release. |

Continue with the [usage guide](ngn.md) and the
[complete configuration reference](ngn-configuration.md).
