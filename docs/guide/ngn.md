# ngn: the terminal harness

`ngn` puts a terminal interface on top of the nagents library. The same coding
harness powers interactive and headless use. Python extensions change the agent's
behavior, not just its appearance.

This is an initial customer-testing version, not an OpenCode feature-parity
release. Read the limitations and trust model below before using it on important
workspaces.

## Install and launch

From a checkout:

```bash
poetry install -E dev -E tui
poetry run ngn --demo
```

For an installed build, include the `tui` extra. Textual is optional: importing
`nagents`, using `Agent`, or running the headless client does not import Textual.

The offline demo needs no API key and does not execute shell commands, change
workspace files, or contact a model. It does save demo conversations locally.
Send `demo approval` to inspect an approval dialog with a sample change preview.
Demo actions are explicitly labeled as previews, not completed real work.

For a real model, set the provider key in your shell environment and launch:

```bash
poetry run ngn --provider openai --model gpt-4.1
poetry run ngn --provider anthropic --model YOUR_MODEL_ID --api-key-env ANTHROPIC_API_KEY
poetry run ngn --workspace /path/to/project
poetry run ngn --continue
```

Use a model ID your provider account actually supports. `--base-url` and
`--api-key-env` support custom endpoints without putting credentials in command
arguments or project configuration. Credentials are not copied from another
coding assistant's configuration.

## Headless use

```bash
poetry run ngn run "Explain how tests are organized"
poetry run ngn run --json "Review the current implementation"
poetry run ngn run --demo --json "Show the interface event stream"
poetry run ngn sessions
poetry run ngn doctor
```

`ngn run -` reads a prompt from standard input. JSON mode emits one event per
line with `schema_version: 1` and an `event` discriminator. It uses the same
tools, hooks, and permission checks as the TUI. Non-interactive approvals are
denied, never silently granted. An interactive `ngn run` asks for approval on
standard error. Control sequences in human-readable tool output are sanitized.

Exit codes are `0` for a completed command, `1` for a run that emits an agent
error, `2` for a configuration/usage failure, and `130` for interruption. A
completed agent run can contain a denied or failed tool that the model handled;
inspect tool-result events when automating a workflow.

## Python behavior extensions

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
- Project `.ngn/config.toml` is ignored unless `--trust-project` is supplied.
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

The first version focuses on a single local agent, Python behavior extensions,
coding tools, sessions, and a terminal/headless interface. It does not yet offer
remote attachment, parallel subagents, worktree isolation, LSP integration,
extension marketplaces, cross-process exactly-once tool execution, or a secure
sandbox for arbitrary plugins and shell commands. Realtime speech remains a
separate library workflow.

For the reasoning behind these boundaries and the upstream comparison, see
[Harness extensibility research](../development/harness-research.md).
