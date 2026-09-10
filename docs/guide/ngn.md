# ngn: the terminal harness

`ngn` puts a terminal interface on top of the nagents library. The same coding
harness powers interactive and headless use. Python extensions change the agent's
behavior, not just its appearance.

The terminal and headless CLI are included in the published `v0.5.0` release.
This guide describes the current checkout, including later fixes; it is not an
OpenCode feature-parity claim. Read the limitations and trust model below before
using it on important workspaces, and check
[release availability](ngn-installation.md#release-availability).

Related pages: [installation and environment choices](ngn-installation.md),
[complete configuration schema and recipes](ngn-configuration.md), and the new
[local web client](ngn-web.md), which is not included in `v0.5.0`.

## Install and launch

From the repository root, retain the existing Poetry workflow:

```bash
poetry install -E dev -E tui
poetry run ngn --demo
```

Alternatively, for a **new** local uv environment:

```bash
uv venv --python 3.13
uv pip install --python .venv/bin/python -e '.[tui]'
uv run --no-project --python .venv/bin/python ngn --demo
```

Do not overwrite a Poetry-managed `.venv`. Use Poetry or another explicitly
named local environment instead. The uv workflow needs neither `uv sync` nor
changes to an existing `uv.lock`. The [installation page](ngn-installation.md)
explains activation and optional isolated tool installations. Commands written
as bare `ngn` below assume the intended environment is activated; otherwise use
`poetry run ngn` or `uv run --no-project --python .venv/bin/python ngn`.

Install the **local source** with the `tui` extra for the behavior documented
here, or select a published CLI release as described in the installation guide.
Textual is optional: importing `nagents`, using `Agent`, or running the headless
client does not import Textual.

The offline demo needs no API key and does not execute shell commands, change
workspace files, or contact a model. It also skips Python plugins, since even
trusted setup code could perform I/O. It does save demo conversations locally.
Send `demo approval` to inspect an approval dialog with a sample change preview.
Demo actions are explicitly labeled as previews, not completed real work.

For a real model, set the provider key in your shell environment and launch:

```bash
poetry run ngn --provider openai --auth api-key --model gpt-4.1
poetry run ngn --provider anthropic --auth api-key --model YOUR_MODEL_ID --api-key-env ANTHROPIC_API_KEY
poetry run ngn --workspace /path/to/project
poetry run ngn --continue
```

Use a model ID your provider account actually supports. `--base-url` and
`--api-key-env` support custom endpoints without putting credentials in command
arguments or project configuration. Credentials are not copied from another
coding assistant's configuration, and ngn does not automatically load `.env`.

Use `api = "auto"` for provider-native defaults, or choose `chat_completions`,
`responses`, `messages`, or `completions` for an endpoint supporting that route.
Legacy Completions is a text-only library route: ngn rejects it during harness
setup because coding requires conversation roles and tools.
LiteLLM requires an explicit `base_url` and an API-key reference such as
`api_key_env = "LITELLM_API_KEY"`; it does not use ChatGPT OAuth or require a
gateway SDK in ngn. See [provider recipes](ngn-configuration.md#provider-recipes).

## Composer and commands

Typing `/` at the start of the composer opens suggestions immediately. Use
Up/Down or the mouse to select a command. Enter prefills its name; edit or add
arguments, then press Enter again to execute. The menu, Ctrl+P palette, and
`/help` all use the same live harness registry, including:

- Built-in actions such as `/new`, `/model`, `/agent`, `/tasks`, and `/queue`.
- Discovered skills as `/skill:<name> [task]`, loaded on demand through guarded
  file tools. Listing suggestions never runs a skill or a script.
- Trusted Python-plugin commands, labeled with their plugin source.

| Command | Purpose |
| --- | --- |
| `/help` | Show the live command registry. |
| `/new` | Start a new local conversation. |
| `/sessions` | List and resume this workspace's saved conversations. |
| `/compact` | Compact active context with the configured strategy. |
| `/model [model]` | Show or change the runtime conversation model. |
| `/agent [name]` | Show or switch the built-in/custom profile. |
| `/context` | Inspect effective context and configuration. |
| `/plugins` | Inspect configured and loaded Python extensions. |
| `/tasks` | Open the agent tree, inspect conversations, and send follow-ups. |
| `/dictate` | Open opt-in recording controls and return an editable draft. |
| `/queue [resume\|clear]` | Inspect, resume, or explicitly discard queued input. |
| `/login`, `/logout` | Manage ngn's local OpenAI device login. |
| `/quit` | Close the client and clean up managed work. |

Dictation's opt-in command is described [below](#dictation); `/help` is the source
of truth for commands available in the particular source build you installed.

Shift+Enter inserts a newline. Ctrl+J and Alt+Enter remain fallbacks for terminals
that do not transmit Shift+Enter distinctly. Pasted multiline text is never
submitted automatically. Tab cycles agent profiles by default, and Shift+Tab
cycles backward. Agent switching waits until the current run is finished or stopped.

New prompts and plugin/skill commands submitted during work queue in FIFO order
by default. `--submit-mode interrupt` instead cancels and awaits the current run
and its children before starting the new input. Ordinary configuration commands
are not deferred behind an active run.

Escape stops current work and pauses the queue without discarding it. Empty Enter
or `/queue resume` resumes queued input; `/queue` inspects it and `/queue clear`
explicitly discards it. The queue is bounded to 20 inputs and is not persisted
across application exit. Completed file or shell actions are never rolled back.

```bash
ngn --submit-mode queue --tab-action agent
ngn --submit-mode interrupt --tab-action complete
ngn --tab-action focus
```

The equivalent TOML settings are `submit_mode = "queue"` and
`tab_action = "agent"`. Tab alternatives are `complete` for command completion
and `focus` for ordinary widget navigation. They also accept `NGN_SUBMIT_MODE`
and `NGN_TAB_ACTION` environment defaults.

### Selection and clipboard

Drag to highlight text and release the mouse button to copy it automatically.
This works for responses, tool output, code, and mouse selections in text fields;
code indentation and line breaks are preserved. Double-click selections copy too.
Choosing an agent or slash-command row is navigation, not text copying.

Ctrl+C copies selected text instead of cancelling work. Without selected text,
it retains the normal cancel/exit behavior. Ctrl+Shift+C explicitly copies without
cancelling; Command+C also works if the terminal forwards it to ngn. Keyboard
selection in an editable field does not automatically replace the clipboard, so
select-all followed by paste still works normally. Use Ctrl+C to copy that selection.

ngn sends the terminal's OSC 52 clipboard request and also uses a local clipboard
helper when available: `pbcopy` on macOS, `wl-copy` on Wayland, `xclip` or `xsel`
on X11, or PowerShell on Windows. This makes copying work in macOS Terminal,
which does not support Textual's OSC 52-only clipboard path. Helpers receive text
on stdin, not as a shell command or command-line argument. ngn never reads your
desktop clipboard automatically.

Over SSH, only terminal clipboard forwarding is used, not the remote desktop's
clipboard. OSC 52 writing must be allowed by your terminal and, if applicable,
tmux. If the terminal blocks it, configure its clipboard permissions or use the
terminal's own selection/copy mechanism. Usual terminal paste shortcuts continue
to work; pasted multiline text is never submitted automatically.

## Themes and motion

```bash
ngn --theme terminal
ngn --theme graphite
ngn --theme ocean
ngn --theme ember
ngn --theme terminal --no-animations
```

`terminal` is the default. It uses ANSI default foreground/background resets, so
your terminal supplies those colors; it does not sample RGB values or rewrite
your terminal's palette. `graphite` is cool neutral, `ocean` uses blue/cyan, and
`ember` provides the warm palette. Graphite combines lilac/mint/rose, ocean uses
turquoise/blue/orchid, and ember uses terracotta/gold/mauve. User and assistant
turns, tools, task states, notices, headings, code, and diffs have distinct
semantic colors instead of sharing one accent. `theme_background` separately selects
`"auto"`, `"terminal"`, or `"theme"`: follow the preset's automatic behavior,
retain the terminal background, or use the theme background. For example:

```toml
theme = "ocean"
theme_background = "terminal"
animations = false
```

The equivalent CLI choices make background variants easy to compare:

```bash
ngn --theme ocean --theme-background theme
ngn --theme ocean --theme-background terminal
ngn --theme graphite --theme-background theme
ngn --theme ember --theme-background terminal
```

Native backgrounds use terminal defaults rather than attempting to sample the
terminal's actual RGB color. Painted semantic text is tested for 4.5:1 contrast;
native ANSI accent contrast depends on the terminal's own palette. Native
selection uses reverse video, so arrow-key navigation remains visible without
guessing whether the inherited background is light or dark.

Each preset has a subtle three-column ASCII activity animation at eight frames
per second; idle/error status states remain still. In the current checkout,
`--no-animations`, `animations = false` in TOML, or `NGN_ANIMATIONS=false`
disables the indicator, animated scrolling, and input cursor blinking.
`TEXTUAL_ANIMATIONS=none` also disables these motions. Brief button-press feedback
remains. Theme selection also accepts TOML `theme` and `NGN_THEME`; background
selection has the `NGN_THEME_BACKGROUND` environment default. TOML overrides
`NGN_*` environment defaults. These are color/motion presets, not a graphical
Bot avatar or a separate team-view implementation.

## Dictation

Microphone dictation is **off by default**. It is short-recording transcription
into the composer, not realtime speech-to-speech and not an always-listening
assistant. Install the optional `voice` extra alongside `tui` for `sounddevice`
capture, then explicitly enable it in trusted, flat TOML:

```toml
dictation_enabled = true
dictation_model = "gpt-4o-mini-transcribe"
dictation_base_url = "https://api.openai.com/v1"
dictation_api_key_env = "OPENAI_API_KEY"
dictation_language = ""
dictation_max_seconds = 120
```

Set the referenced API key outside TOML. The transcription settings are separate
from `provider`, `model`, `base_url`, and `api_key_env` for the conversation.
For example, a conversation can use Anthropic, LiteLLM, or ChatGPT device login
while dictation uses a separately configured transcription API.

Use `/dictate` or **Ctrl+G**. `--dictation` enables the feature for one launch,
and `--no-dictation` explicitly disables it. Opening the modal does not activate
the microphone. **Start recording** starts capture; **Stop & transcribe** stops
capture and uploads once. At the duration limit, capture stops without uploading
and waits for an explicit **Transcribe** action. Edit the preview, then choose
**Use text** to insert it at the main composer's cursor. Escape or Ctrl+C cancels
the modal and waits for cleanup; exiting ngn also closes its recorder and client.
Finish or stop an active coding run before opening recording controls.

Transcribed text is an **editable preview**. Review and correct it before
explicitly submitting it as a prompt; recording does not automatically send a
chat message. `dictation_max_seconds` bounds recording to `1..300` seconds,
default `120`. Empty `dictation_language` leaves language detection to the
service; otherwise use exactly two lowercase letters, such as `"en"`, supported
by the transcription service.

Audio is sent to the configured transcription service when transcription is
requested. Its account billing, retention, and privacy policies apply. Use a
trusted endpoint and do not record sensitive material without permission. A
ChatGPT subscription or ngn device-login token **does not authorize or pay for**
the separate transcription API; provide a paid API key with access to that
service. A compatible custom endpoint must support the transcription API; a
working chat route does not guarantee audio support.

See [microphone installation](ngn-installation.md#optional-microphone-support)
for PortAudio/device requirements, and the
[dictation schema](ngn-configuration.md#dictation) for all defaults and environment
references. Demo mode is intended to remain offline; do not treat it as a live
transcription test. `/dictate` is disabled in demo mode, even when dictation is
enabled in configuration. Captured WAV data stays in bounded memory, not local
audio files. Uploads have a 60-second timeout, no redirects, and no automatic
retries. Cancellation cannot recall audio already received by the endpoint.

## Native background subagents

The agent can call `delegate(prompt, agent="agent")`; `agent` is the default
target profile. Use `agent="reviewer"` for an explicitly read-only task. The caller
immediately receives a UUID task ID, a random two-word name such as `quiet maple`,
and `running` status.
The child runs as a native asyncio task while the parent continues its own work.

When the parent turn settles, completed results are batched into an explicitly
untrusted background notification, and the parent automatically runs again to
consider them. If children are still working, the harness waits for their results
without holding a model request open. The UI shows each child's task and response;
`/tasks` opens the current-session conversation inspector. Headless JSON includes
`task_started`, `task_message`, `task_completed`, and `task_notification` events,
with a final `done` only after the group settles. Task lifecycle events include
parent/child session IDs, parent task ID, depth, profile, follow-up number,
activation number, and trigger. Notification records identify the actual source
and recipient; they do not themselves assert that another execution completed.

Nested completion notifications target the **immediate parent**. Main can observe
all task lifecycle events without receiving every grandchild result as a separate
model instruction. When an eligible retained child is continued after its parent
has finished, that parent is reactivated in its own conversation, synthesizes
the update, and reports upward. Cancelled or unavailable ancestors are not
silently bypassed. Automatic activations have their own activation number and
trigger; they do not count as human follow-ups or reset the root execution budget.

This preserves provider protocol rules: the delegation call has exactly one
matching tool result (the started acknowledgement). Late completion is not a
second tool result and is not promoted to a system instruction. It is a new,
labeled user-role data notification at a safe turn boundary, using the same
`Agent.run`, hooks, context handling, and permissions as ordinary turns.

The built-in `agent` profile has build mode, like `build`; `reviewer` is read-only.
Children have the **same permission ceiling as their parent**, not independent
authority. A reviewer parent cannot obtain write or shell privileges by
delegating to `agent` or a build-mode custom profile. Build-capable child actions
still need the ordinary guarded-tool approvals. An instruction or profile name
does not bypass this boundary.

`max_subagent_depth` defaults to `2` and accepts integers from `0` through `8`.
Depth counts the root as `0`, a child as `1`, and a grandchild as `2`. The default
therefore permits grandchildren, but not great-grandchildren. Set it to `0` to
disable delegation, or `1` to allow only direct children.

Depth is separate from concurrency and job budgets. Additional initial bounds
are three concurrent subagents and eight child executions per root user run,
shared across the entire descendant tree and including automatic continuations
and accepted human follow-ups. A quota-full delegation fails immediately instead
of waiting indefinitely for a slot; a parent still working or waiting for its
own children occupies a slot. Further limits are at most
`min(max_tool_rounds, 12)` tool rounds per child, a five-minute child timeout,
a 16,000-character delegation prompt, and
12,000-character results. These are implementation limits, not extra TOML knobs.
Children use separate sessions/providers and share the parent's credential
manager for in-process refresh coordination. They receive the delegated prompt
and applicable instruction context, not a copy of the full parent conversation.
Plugins are not re-imported in children. Children currently use the parent's
active model; arbitrary custom-provider cloning and persistent peer-to-peer
inboxes are not implemented.

Tasks are process-local, not durable jobs or persistent Bot identities. Cancelling
or closing a run cancels and awaits its children; undelivered notifications are
discarded, and task outcomes are not replayed after restart. Child transcripts
remain in local storage but do not clutter the ordinary session picker.

The [web client](ngn-web.md#scheduled-wakeups) additionally owns a process-local
timer service for `schedule_wakeup` (historical name: `wake_up_in`). It resumes the
scheduling root or child without keeping a browser request open. Ordinary
CLI/TUI instances do not own that service and reject timer requests explicitly.
Timers do not survive a server or pod restart.

### Agent tree and follow-ups

The right rail contains a scrollable tree of running and finished agents. Nested
delegations appear under their parent, with status labels. Collapse branches with
Left/Right or the mouse; selection and expansion survive status updates. Ctrl+T
focuses the tree, and Enter or a click opens the selected conversation. On narrow
terminals, Ctrl+T opens the same tree inside the conversation inspector instead
of squeezing the main conversation. `/tasks` opens the inspector at any width.

The inspector shows saved user/assistant/tool messages and a per-agent follow-up
draft. Use Up/Down to select, Left/Right to expand or collapse, and Tab or Enter
to move into the draft. Ctrl+Enter or **Continue Agent** sends it. Selecting a
node never sends a message. You can inspect and draft while work runs, but must
wait for that child and its subtree to settle before continuing it. Cancelled
tasks cannot be resumed; delegate a new task instead.

A follow-up continues the **same child conversation and identity**, not a new
unrelated agent. Its human message and returned response are visible in the
parent conversation too. If the parent is already running, it receives the
notification at its next safe turn boundary. If idle, ngn waits for the child
result and runs the parent to consider the exchange. It does not inject a second
tool result or silently treat child output as higher-priority instructions.

Each identity permits eight explicit human follow-ups, and the root retains up
to 64 identities for its process lifetime. Every continuation also consumes one
of the current root run's eight execution slots. Full quotas and unsupported
continuations are rejected explicitly. These handles do not survive restarting
ngn, even though the underlying transcripts remain on disk.

Python clients use the same interfaces:

```python
from contextlib import aclosing

tasks = harness.tasks.list()  # Detached snapshots, including descendants.
messages = await harness.task_history(tasks[0].id, limit=100)
async with aclosing(harness.continue_task(tasks[0].id, "Check this case as well.")) as events:
    async for event in events:
        handle(event)
```

The history limit is `1..200`; inspection does not run a model or repair an
unfinished tool block. During an existing root run, `continue_task()` yields an
acceptance notice; task events keep flowing through that run's existing consumer.
When idle, the returned stream owns execution and must be consumed or explicitly
closed, just like `Harness.run()`.

Try `demo subagents` in `ngn --demo` for three genuine concurrent asyncio jobs
using clearly labeled, scripted offline responses. A live example is:

> Delegate three independent read-only reviews of the README, test setup, and
> package configuration. Continue your own review, then combine the results.

Protocol references: [OpenAI function-call correlation](https://developers.openai.com/api/docs/guides/function-calling)
and [Anthropic tool-result ordering](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

## OpenAI device login

Start `ngn` without `--demo`, then enter `/login`. Select ChatGPT device login,
open the displayed OpenAI link, and enter the one-time code in your browser.
Only approve a code you requested yourself. Escape or Cancel stops waiting.
The terminal never asks for your ChatGPT password.

You can also use the headless login command:

```bash
ngn login --device-auth
ngn login --status
ngn
```

OpenAI currently documents device authentication as **beta**. You may need to
enable it in your [ChatGPT security or workspace settings](https://developers.openai.com/codex/auth/#login-on-headless-devices).
Your account/workspace must have Codex access. If authorization is unavailable,
use an OpenAI Platform API key instead; device login does not bypass account
restrictions or turn a ChatGPT subscription into general API access.

ChatGPT sign-in uses `https://chatgpt.com/backend-api/codex/responses`, not the
ordinary API-key chat-completions endpoint. The initial default `gpt-4.1` is
switched to `gpt-5.6-terra` for this route; use `/model` to select another model
available to your account. The underlying `CodexProvider` lives in the library;
interactive login, token refresh/storage, and route selection live in the harness.

The `auth` configuration field and `--auth` accept:

- `auto`: prefer a saved ChatGPT login only for the default OpenAI endpoint with
  `api = "auto"`; otherwise use the configured API-key environment variable.
- `api-key`: always use the configured API-key environment variable.
- `chatgpt`: require the saved ChatGPT login and the Codex Responses route.

Saved ChatGPT credentials are never sent to a custom `base_url`. Such endpoints
use API-key authentication, and explicitly combining `auth = "chatgpt"` with a
custom endpoint or an `api` override is rejected. Keep `api = "auto"` for
ChatGPT/Codex login, even though its dedicated route uses Responses. Device login
is disabled in offline demo mode.

Tokens are stored in `$XDG_DATA_HOME/ngn/auth/openai.json`, or
`~/.local/share/ngn/auth/openai.json` by default. This is a **plaintext credential
file**, protected by restrictive file/directory permissions on POSIX, not an
encrypted keyring. It is separate from the workspace and session database.
Refresh tokens rotate during use. Treat the entire file like a password: never
commit, attach, or paste it into bug reports. Built-in file tools deny access to
this store even when the workspace is your home directory.

`/logout` or `ngn logout` removes ngn's local saved login. It does not touch other
clients' credentials or revoke remote sessions. ngn never imports `.codex` or
OpenCode credential caches. Device codes are displayed only by the login UI or
explicit login command; they are not added to model context or conversation history.

## Headless use

```bash
poetry run ngn run "Explain how tests are organized"
poetry run ngn run --json "Review the current implementation"
poetry run ngn run --demo --json "Show the interface event stream"
poetry run ngn sessions
poetry run ngn doctor
```

The same commands work through the local uv launcher, for example:

```bash
uv run --no-project --python .venv/bin/python ngn run --demo --json "Hello"
```

`ngn run -` reads a prompt from standard input. JSON mode emits one event per
line with `schema_version: 1` and an `event` discriminator. It uses the same
tools, hooks, and permission checks as the TUI. Non-interactive approvals are
denied, never silently granted. An interactive `ngn run` asks for approval on
standard error. Control sequences in human-readable tool output are sanitized.

`--json` is a `run` option, not an option for every subcommand. Human-readable
text streams to stdout; tool progress and approval prompts use stderr. JSON
mode writes versioned event records to stdout, with command errors/denials still
possible on stderr. Consumers should dispatch on `event`, tolerate additional
fields, and handle tool failures as well as the final response. Subagent events
include `task_started` and `task_completed`.

Exit codes are `0` for a completed command, `1` for a run that emits an agent
error, `2` for a configuration/usage failure, and `130` for interruption. A
completed agent run can contain a denied or failed tool that the model handled;
inspect tool-result events when automating a workflow.

### CLI reference

Run without a subcommand for the TUI. Common options are accepted before or
after the subcommand; prefer supplying each option only once.

| Command | Purpose |
| --- | --- |
| `ngn` | Full-screen terminal client. Requires the `tui` extra and a terminal. |
| `ngn run [--json] [prompt ...]` | Headless prompt execution. `-`, or no prompt with piped stdin, reads stdin. |
| `ngn serve` | New local React client for the harness. Requires the current checkout, `[web]`, and built assets; see [Local Web Client](ngn-web.md). Not the legacy `python -m nagents.server` API. |
| `ngn sessions` | List saved sessions for the selected workspace. |
| `ngn doctor` | Initialize the harness and show configuration/extension diagnostics without an LLM request. Trusted plugins still execute. Use `--demo` to skip them. |
| `ngn login [--device-auth]` | Start OpenAI device-code login; device auth is the default method. |
| `ngn login --status` | Show local login status without a network request. |
| `ngn logout` | Remove only ngn's local saved OpenAI login. |
| `ngn --version` | Print CLI/package version; the version alone does not prove an unreleased feature is present. |
| `ngn --help`, `ngn run --help` | Show options implemented by the installed source build. |

| Common option | Meaning |
| --- | --- |
| `--workspace PATH`, `-C PATH` | Select an existing workspace directory. |
| `--config PATH` | Load and explicitly trust a TOML file at any location. |
| `--trust-project` | Also trust `<workspace>/.ngn/config.toml`. |
| `--provider NAME` | Select provider name/alias. |
| `--model ID`, `-m ID` | Set top-level model selection. A profile's own model can supersede it on activation. |
| `--base-url URL` | Override API endpoint. Never include credentials. |
| `--api auto\|chat_completions\|responses\|messages\|completions` | Select provider HTTP protocol. Legacy Completions is text-only; ChatGPT login requires `auto`. |
| `--api-key-env NAME` | Name the environment variable holding an API key. Never pass the value as this argument. |
| `--auth auto\|api-key\|chatgpt` | Select authentication behavior. |
| `--agent NAME`, `-a NAME` | Select built-in `build`, `agent`, `reviewer`, or a custom profile. |
| `--max-subagent-depth N` | Integer `0..8`; default `2`, with the root at depth `0`. |
| `--theme NAME` | Select `terminal`, `graphite`, `ocean`, or `ember`. |
| `--theme-background auto\|terminal\|theme` | Choose preset-default, terminal-native, or preset background behavior. |
| `--no-animations` | Disable the busy indicator's motion, animated scrolling, and cursor blinking; brief button feedback remains. |
| `--dictation`, `--no-dictation` | Explicitly enable or disable the opt-in microphone feature. Never starts recording automatically. |
| `--dictation-model ID` | Select the separate transcription model. |
| `--dictation-base-url URL` | Set the transcription API base. |
| `--dictation-api-key-env NAME` | Name the environment variable containing the transcription key. |
| `--dictation-language CODE` | Empty for detection, or exactly two lowercase ASCII letters. |
| `--dictation-max-seconds N` | Integer `1..300`; default `120`. |
| `--submit-mode queue\|interrupt` | Control prompts submitted while work is active. |
| `--tab-action agent\|complete\|focus` | Select composer Tab behavior. |
| `--plugin REFERENCE` | Explicitly trust and append a Python extension; repeatable. |
| `--demo` | Use the offline demo. |
| `--continue`, `-c` | Resume the latest session in this workspace, if any. |
| `--resume ID` | Resume a specific session in this workspace. Mutually exclusive with `--continue`. |

Not every TOML field has a matching CLI flag: `api_version`, `data_dir`,
`shell_timeout`, `max_output`, `max_file_bytes`, and `max_tool_rounds` use TOML or
their `NGN_*` defaults. Profile tables use TOML. Use the
[configuration reference](ngn-configuration.md) for types and validation, and
`--help` for the flags present in the particular source build.

### Session storage

Sessions are scoped to the resolved workspace path. The default state root is
`$XDG_DATA_HOME/ngn`, falling back to `~/.local/share/ngn`; each workspace uses a
hashed subdirectory containing `sessions.db`. Set `data_dir` to change the
session-state root. Moving a workspace or changing its resolved path can select
a different scope; `--resume` does not search unrelated workspaces.

Demo conversations persist too. Queued prompts and running subagent jobs do not
survive application exit. Resuming history does not replay completed or uncertain
tool actions, and child transcripts are not ordinary session-picker entries.
Session databases can contain private prompts and workspace content; do not
commit or attach them to bug reports. The OpenAI credential store is separate
and is not relocated by `data_dir`.

## Configuration

User configuration is `~/.config/ngn/config.toml` (or under `$XDG_CONFIG_HOME`).
Project configuration is `<workspace>/.ngn/config.toml` and requires
`--trust-project` **or explicit selection of that file** with `--config`.
`--config /any/path/settings.toml` explicitly trusts that file; its name and
location need not be `.ngn/config.toml`. Settings are top-level TOML with no
`[ngn]` wrapper; named profiles use `[profiles.NAME]`:

```toml
provider = "openai"
model = "gpt-4.1"
api = "auto"
auth = "auto"
api_key_env = "OPENAI_API_KEY"
agent = "build"
shell_timeout = 60.0
max_subagent_depth = 2

[profiles.audit]
mode = "reviewer"
instructions = "Prioritize regressions and missing tests."
```

Precedence is built-in defaults, `NGN_*` environment defaults, user TOML, trusted
project TOML, explicit TOML, then CLI flags. `NGN_MODEL`, `NGN_PROVIDER`,
`NGN_AUTH`, and `NGN_API_KEY_ENV` are examples of environment defaults. Plugin
paths in TOML resolve relative to that file; explicit `--plugin` paths resolve
relative to the shell's current directory. `data_dir` in TOML is also relative
to that file, not the workspace. TOML plugin lists replace earlier lists; CLI
`--plugin` entries append. Profiles merge by name, but a later same-name table
replaces the whole profile, with omitted fields resetting to profile defaults.
No literal API keys belong in TOML.

See the [full configuration reference](ngn-configuration.md) for every accepted
field, exact types/defaults/ranges, provider recipes, and the distinction between
configuration precedence and runtime profile model selection.

## Python behavior extensions

### Register slash commands

Trusted setup functions can register a prompt template or an async handler:

```python
from nagents.harness import CommandResult


async def status(harness, arguments):
    return CommandResult(message=f"Current model: {harness.agent.provider.model}")


def setup(harness):
    harness.commands.register(
        "review-code",
        "Review a selected file",
        prompt="Review this file without editing it: $ARGUMENTS",
        argument_hint="<file>",
        requires_arguments=True,
    )
    harness.commands.register("local-status", "Show local status", handler=status)
```

Prompt templates replace only literal `$ARGUMENTS`; JSON/Python braces are not
interpreted. A `CommandResult` contains either a local `message` or a `prompt`
for the normal agent loop. Discovery does not execute handlers. Built-in names
and the `skill:` namespace are reserved, and duplicate names are rejected.
Plugin attribution is automatic while `setup` runs. Async handlers retain full,
trusted Python access; they are not sandboxed or automatically mediated as tools.

See `examples/harness/commands.py` in the checkout, or launch it with
`ngn --plugin examples/harness/commands.py:setup` and type `/`.

### Change agent behavior

The supported interfaces live in `nagents.extensions` and are also exported
from `nagents`. They do not depend on the coding harness or TUI.

| Interface | Purpose |
| --- | --- |
| `AgentPlugin.before_run` | Transform an incoming message |
| `AgentPlugin.before_model` | Transform request-local messages, tools, and generation configuration |
| `AgentPlugin.before_tool` | Transform tool arguments before the execution permission boundary |
| `AgentPlugin.after_tool` | Transform the tool result |
| `AgentPlugin.on_event` | Observe core events |
| `AgentPlugin.after_run` | Run lifecycle cleanup |
| `CompactionStrategy.should_compact` | Decide when custom compaction should run |
| `CompactionStrategy.compact` | Replace the active message context using arbitrary Python logic |

Hooks run in plugin order. A plugin receives a `RunContext` containing the agent,
session, user, and current round. This gives trusted code access to the underlying
library rather than a second, restricted imitation of its API.

The plugin chain is snapshotted at the start of a run. Event observers receive
copies; use transformation hooks to change behavior. Tool hooks must preserve the
call ID and name. Hook failures stop the run, and cleanup attempts every plugin's
`after_run` hook. These extension contracts currently apply to the text agent
loop; voice, batch, and `run_simple()` reject extension-enabled execution rather
than silently bypassing a permission boundary.

Request transformations and persistent compaction are different operations.
`before_model` affects the next request; it does not append its transformed
messages to the transcript. A compaction strategy returns a `CompactionResult`
containing replacement active messages. Those messages are stored atomically and
the active-history boundary advances without deleting older rows. Replacement
histories must keep tool calls and results paired.

The strategy receives active history without the configured system prompt. It
can trim complete turns, call its own summarizer, retain task state, retrieve
memory, or implement another algorithm. Manual compaction uses the same strategy.
The older `Compactor`, compaction prompts, and trigger options remain available
to existing library callers.

See the runnable example:

```bash
poetry run ngn --plugin examples/harness/custom_behavior.py:setup
```

That extension installs a no-LLM recent-turn compactor, adds request-local
verification instructions, and registers a Python tool. It deliberately discards
old active context rather than summarizing it, making the distinction between
algorithm replacement and prompt customization easy to inspect.

A harness extension exports `setup(harness)`, synchronously or asynchronously.
It may configure `harness.agent` and return an `AgentPlugin`. Existing library
features remain accessible from this trusted Python entry point; for example,
an application can compose MCP clients and register their tools without adding
MCP implementation details to the terminal widgets.

## Trust and permissions

- Global configuration and an explicitly selected `--config` file are trusted.
- Project `.ngn/config.toml` is ignored unless `--trust-project` is supplied or
  that file is explicitly selected with `--config`.
  Review it and referenced Python code before enabling it.
- `--plugin path.py:setup` explicitly trusts that Python code. Installed-module
  entry points use the same `module:setup` notation.
- Plugins have the process's full privileges. They are not sandboxed, and may
  use the network or write files during setup. Only load code you trust.
- Built-in file tools restrict paths to the workspace, check symlinks, and guard
  sensitive paths. Changes require a preview and approval, with conflict checking
  to avoid replacing edits made while an approval is open.
- Shell execution is local, not sandboxed. An approved shell command can access
  anything your user account can access, including outside the workspace.
- The reviewer profile denies modifying tools and shell execution. Skills and
  system prompts are not the mechanism enforcing those restrictions.
- Skills provide instructions/resources on demand. Loading a skill does not
  automatically execute scripts in its directory.
- Interruption stops managed work but never rolls back already completed actions.
  An uncertain tool outcome is not automatically replayed on resume.

No automatic Git commit, reset, checkout, push, or rollback is performed. Review
the diff and run appropriate checks before accepting generated changes.

## Customer test loop

1. Start with `ngn --demo`; try streaming, `demo approval`, denial, cancellation,
   a new session, and session resume.
2. Resize to a small terminal and confirm the composer and approval controls
   remain usable. Exercise multiline input and command discovery.
3. Configure a real provider and ask for a read-only explanation of a disposable
   project. Check which tools and context were used.
4. Ask for a small edit. Inspect the exact preview before approving, then review
   the resulting Git diff and run a relevant test.
5. Load `examples/harness/custom_behavior.py:setup` and inspect the active
   extension/compaction behavior. Try your own context transform or compactor.
6. Report the command, terminal size/emulator, provider/model, expected behavior,
   and the observed failure. Do not include API keys or secret file contents.

## Scope

The first version focuses on a local coordinating agent with bounded subagents, Python behavior extensions,
coding tools, sessions, and a terminal/headless interface. It does not yet offer
remote attachment, persistent peer-agent groups, worktree isolation, LSP integration,
extension marketplaces, cross-process exactly-once tool execution, or a secure
sandbox for arbitrary plugins and shell commands. Realtime speech remains a
separate library workflow. Guarded filesystem tools and process-group shell
cleanup currently target POSIX; Windows is not yet a supported coding-tool host.
Skills support single-line `name` and `description` frontmatter, and ignore
matching is a documented subset rather than complete Git wildmatch semantics.

For the reasoning behind these boundaries and the upstream comparison, see
[Harness extensibility research](../development/harness-research.md).
