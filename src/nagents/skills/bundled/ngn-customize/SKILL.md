---
name: ngn-customize
description: Customize Nagents and the ngn application using real Agent, provider, session, tool, executor, plugin, compaction, Harness, profile, skill, command, subagent, wakeup, channel, and web interfaces. Use when implementing or explaining ngn behavior extensions or configuration.
---

# Customize Nagents and ngn

Use this skill for this application's library and clients. First identify the
installed version and the owning layer below. Inspect the relevant source in a
checkout before changing behavior; the public docs describe the current checkout
and can be newer than an installed release. Prefer a small extension at the
existing registration point, then verify its actual execution path.

This body is self-contained. Source paths below are repository-relative pointers
for verification, not additional files automatically loaded by the skill tool.

## Extension map

| Need | Object or entry point | Source under `src/nagents/` |
| --- | --- | --- |
| Text agent lifecycle and runtime options | `Agent(...)`, `run()`, `close()` | `agent.py` |
| Provider/model/protocol/retries | `Provider`, `ProviderType`, `RetryConfig`, `GenerationConfig`, `CodexProvider` | `provider/`, `types.py`, `http/` |
| Persistent conversation and active context | `SessionManager`, `Message`, `Agent.session` | `session/manager.py`, `types.py` |
| Model-callable Python functions | `Agent.register_tool`, `ToolRegistry.register` | `tools/registry.py` |
| MCP server processes and tool wrappers | `MCPServerConfig`, `MCPClient`, `MCPManager` | `mcp/` |
| Tool authorization/execution | `ToolExecutor.execute`, `HarnessExecutor` | `tools/executor.py`, `harness/tools.py` |
| Ordered lifecycle transformations | `AgentPlugin`, `RunContext`, `ModelRequest` | `extensions.py` |
| Replace the context algorithm | `CompactionStrategy`, `CompactionRequest`, `CompactionResult` | `extensions.py`, `agent.py` |
| Configure model-based compaction | `Compactor`, `Tokens`, `Messages`, `compact_prompt` | `compactor.py`, `compaction.py` |
| Assemble the coding application | `Harness`, `HarnessConfig`, `load_config` | `harness/runtime.py`, `harness/config.py` |
| Profiles and project instructions | `AgentProfile`, `Harness.refresh_instructions`, `AGENTS.md` | `harness/config.py`, `harness/tools.py` |
| On-demand task guidance | `Skill`, `SkillDiscoverer`, `Agent.load_skill` | `skills/`, `agent.py` |
| Prompt/local commands | `CommandRegistry.register`, `CommandResult` | `harness/commands.py` |
| Child agents and follow-ups | `SubagentManager`, `Harness.tasks`, `continue_task` | `harness/subagents.py` |
| Delayed continuations | `schedule_wakeup`, `WakeupHandler`, `Harness.wakeup_handler` | `harness/types.py`, `web/wakeups.py` |
| External transports and routing | `Channel`, `ChannelPlugin`, `Agent.add_channel`, `listen` | `channels/`, `web/channel_host.py`, `web/routing.py` |
| Client-specific settings and presentation | TUI commands/themes; web settings/subscriptions; CLI events | `tui/`, `web/`, `cli.py` |

## Configure the Harness

Settings are flat YAML. User config uses the XDG config location for
`ngn/config.yaml`. Workspace `.ngn/config.yaml` is selected with
`--trust-project` or explicitly with `--config`. Precedence is defaults,
`NGN_*` environment defaults, user YAML, trusted project YAML, explicit YAML,
then CLI flags. Use `ngn --help` and `ngn doctor` for the installed build;
`doctor` initializes trusted plugins, while `--demo` skips their execution.

```yaml
provider: openai
model: gpt-4.1
api: auto
auth: api-key
api_key_env: OPENAI_API_KEY
agent: audit
max_tool_rounds: 30
max_subagent_depth: 2
skill_token_limit: 10000
plugins: ["extensions.py:setup"]

profiles:
  audit:
    mode: reviewer
    instructions: Prioritize regressions and report the checks actually run.
```

`AgentProfile` has `mode`, `instructions`, and `model`. `assistant` is the only
built-in agent and uses build mode. Other names require explicit profiles; use
`mode: reviewer` for read-only behavior. Custom profiles cannot replace
`assistant`. A profile's nonempty model is selected on activation; inspect the
effective model rather than assuming the top-level model always wins.

YAML plugin paths and `data_dir` resolve relative to the config file. CLI
`--plugin` paths resolve relative to the invoking directory. YAML plugin lists
replace earlier lists; CLI entries append. Profile entries merge by name, with a
later same-name entry replacing that profile. Provider secrets come from the
named environment variable, never literal YAML values.

Other configuration groups: `shell_timeout`, `max_output` (bytes),
`max_file_bytes`, and `max_tool_rounds`; `theme`, `theme_background`, `animations`,
`submit_mode`, `tab_action`; separate `dictation_*` preferences. Use the complete
configuration guide for accepted types and bounds rather than inventing keys.

`AGENTS.md` supplies project task context, including nested instructions as
guarded file access reaches their subtree. Trusted profile instructions are
assembled by `Harness.refresh_instructions()`. A plugin that needs request-local
changes should use `before_model` rather than repeatedly appending text to
`agent.system_prompt`, which the Harness regenerates.

## Register behavior, tools, and commands

A trusted Python extension exports sync or async `setup(harness)`, loaded with
`ngn --plugin extensions.py:setup` or `installed.module:setup`. It can configure
the Harness and return one `AgentPlugin`, or return `None`. The loader appends
the returned plugin to `harness.agent.plugins`; do not also append that instance
inside setup.

```python
from dataclasses import replace
from datetime import UTC, datetime

from nagents import AgentPlugin, Message, ModelRequest, RunContext
from nagents.harness import CommandResult, Harness


class VerificationContext(AgentPlugin):
    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        instruction = Message(
            role="system",
            content="Report checks actually run separately from recommendations.",
        )
        return replace(request, messages=[instruction, *request.messages])


def utc_time() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()


async def local_status(harness: Harness, arguments: str) -> CommandResult:
    return CommandResult(message=f"Current model: {harness.agent.provider.model}")


def setup(harness: Harness) -> AgentPlugin:
    harness.agent.register_tool(utc_time)
    harness.commands.register(
        "review-code",
        "Review a selected file",
        prompt="Review this file without editing it: $ARGUMENTS",
        argument_hint="<file>",
        requires_arguments=True,
    )
    harness.commands.register("local-status", "Show local status", handler=local_status)
    return VerificationContext()
```

The system-role instruction above is authored by trusted Python code. Do not
copy user text, skill bodies, or tool results into that role.

Registration belongs to the actual object: `harness.agent.register_tool(...)`,
`harness.agent.tool_registry.register(...)`, and
`harness.commands.register(...)`. There is no separate `harness.add_tool()` API.
For explicit JSON Schema, use
`tool_registry.register(function, name=..., description=..., parameters=...)`.
The convenience `Agent.register_tool` accepts function, name, and description.
The registry also provides `get`, `get_all`, `names`, and `unregister`; registering
an existing tool name replaces its definition, so check names first.

Commands register either a prompt or an async handler. Only literal
`$ARGUMENTS` is substituted; braces are ordinary text. `CommandResult` contains
either a local `message` or a `prompt` for `Harness.run`. Built-in names and
the `skill:` namespace are reserved. Command metadata does not execute handlers.
Handlers are trusted Python, not automatically tool-mediated actions.

For MCP, `nagents.mcp.MCPServerConfig` describes a stdio server's name, command,
args, optional environment, and working directory. `MCPManager(configs)` owns
connections: await `connect_all()`, then register the callable wrappers from
`await manager.get_tools()` through `harness.agent.register_tool`. To add a
server dynamically, `await manager.add_server(config)` returns its new wrappers.
The embedding application owns `disconnect_all()` during shutdown; keep it alive
for the intended tool lifetime rather than closing it after setup. These remain
custom tools subject to the Harness executor. There is no YAML MCP-server table
or automatic child inheritance; trusted Python composition supplies the wiring.

### Hooks and executor boundary

| Hook | Contract |
| --- | --- |
| `before_run(context, message) -> Message` | Transform incoming message. |
| `before_model(context, request) -> ModelRequest` | Transform next-request messages, tools, and generation config; does not rewrite history. |
| `before_tool(context, call) -> ToolCall` | Transform arguments before executor authorization; preserve name and call ID. |
| `after_tool(context, result) -> ToolResultEvent` | Transform result; preserve name and call ID. |
| `on_event(context, event) -> None` | Observe a detached event copy. |
| `after_run(context) -> None` | Lifecycle cleanup, including failure and stream closure. |

`RunContext` contains `agent`, `session_id`, `user_id`, and zero-based
`round_number`. Plugins run in registration order; the chain is snapshotted at
run entry. Hook failures abort the run. Cleanup attempts every plugin.

For a standalone Agent, subclass `ToolExecutor` and implement
`async execute(tool_call: ToolCall) -> ToolResultEvent` when authorization or
execution policy changes. Initialize it with the Agent's own registry and assign
`agent.tool_executor`; retain result correlation and cancellation. The default
executor awaits async functions and sends sync functions through
`asyncio.to_thread`. Disable `save_tool_outputs` when the executor must own all
filesystem access, including the reserved `_save_to` convention.

The Harness already installs `HarnessExecutor`: guarded built-ins, validated
arguments, per-action approvals, reviewer restrictions, and child permission
ceilings. Keep this executor for Harness extensions. Adding a tool does not make
it a privileged builtin; custom tools require approval and are denied to
reviewers and children. Python setup/hooks themselves retain full process access.

## Choose a compaction layer

`CompactionStrategy` is a structural async protocol:
`should_compact(request) -> bool`, `compact(request) -> CompactionResult`.
Assign `harness.agent.compaction_strategy` in setup to replace the algorithm.
It takes precedence over the legacy compactor. `CompactionRequest` carries
detached active messages, session, provider, estimated token count, and `force`;
it excludes the configured system prompt. Manual compaction bypasses
`should_compact`.

This deliberately lossy example retains complete recent user turns:

```python
from nagents import CompactionRequest, CompactionResult


class RecentTurns:
    async def should_compact(self, request: CompactionRequest) -> bool:
        return sum(message.role == "user" for message in request.messages) >= 10

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        starts = [i for i, message in enumerate(request.messages) if message.role == "user"]
        start = starts[-4] if len(starts) >= 4 else 0
        return CompactionResult(
            messages=request.messages[start:],
            summary="Retained four recent turns; earlier details were not summarized.",
        )


# In setup(harness):
# harness.agent.compaction_strategy = RecentTurns()
```

Return nonempty valid history with assistant calls and tool results paired.
Replacement is atomic: the active-history boundary advances while older SQLite
rows remain. Do not confuse `before_model` transformations with persisted
context replacement. Skill tool results remain subject to ordinary compaction;
load a skill again when its detailed guidance is needed.

For prompt/trigger customization, keep the legacy layer: `compactor="self"`
uses the main provider, a `Compactor` uses a separate agent, `compactor=None`
disables that compactor, and an omitted value uses an agent-local default.
`compact_on=Tokens(input=..., output=...)` or `Messages(length=...)` controls the
trigger; `compact_prompt` customizes self-compaction. `trigger_compaction()`
requests it during a run; `await agent.compact(session_id)` invokes it manually.

## Providers, sessions, and owned execution

Construct `Agent(provider, session_manager, ...)` with `Provider` and
`SessionManager(Path("agent.db"))`, register tools, and consume `agent.run(...)`.
`GenerationConfig` supplies request generation options. Provider types include
OpenAI-compatible, Anthropic, native Gemini, Azure, OpenRouter, and LiteLLM.
`api` selects a supported HTTP contract; `base_url` is an API prefix, not a full
generation endpoint. LiteLLM needs an explicit gateway URL and API-key variable.
Text-only legacy Completions cannot drive the coding Harness's tools.

`CodexProvider` uses its credential-provider interface; the Harness owns device
login, refresh, and credential storage. `auth="chatgpt"` requires the default
OpenAI route with `api="auto"`; custom endpoints use API-key authentication.
Dictation has separate transcription credentials and is not authorized by
ChatGPT device login. Provider model discovery is explicit; a listed model ID
does not prove capability or account entitlement. Custom provider subclasses
must honor verification, generation events, retries, and cleanup, not just return
strings from `generate`.

`Agent.session` exposes `SessionManager` operations such as `get_history`,
`add_message`, and `replace_context`. The Harness owns workspace-scoped SQLite
storage and exposes `new_session`, `resume`, `list_sessions`, `history`,
`set_agent`, `set_model`, and `compact`. Serialize mutations with execution.
Consume async event generators to completion or close them with
`contextlib.aclosing`; always await the owning Agent/Harness `close()`.

Hooks, custom executors, compaction strategies, and dynamic skills belong to the
text loop. Extension-enabled batch, realtime voice, and `run_simple()` reject
execution instead of silently bypassing that lifecycle. Realtime/audio and STT
have their own library interfaces; browser/TUI dictation produces a draft for a
subsequent text run.

## Skills, children, and timers

Workspace `.ngn/skills/` and `.agents/skills/` contain
`<name>/SKILL.md` with single-line `name` and `description` frontmatter. The
Harness also includes `ngn-customize` and `ngn-channels` from package resources.
Metadata is advertised first; `skill(name)` loads instructions on demand.
The escaped catalog is descriptive tool-selection data in a separate system-level
message, preserving existing user/tool order. Loaded bodies remain user/task
context or tool results. `MAX_SKILL_MANIFEST_TOKENS = 10_000` caps only the model
preview: complete name-sorted entries plus an omission notice. `Agent.skills`,
named loads, and explicit references retain access to the full catalog.
`$ngn-customize` in incoming user text explicitly activates it; the TUI also
supports `/skill:ngn-customize [task]`. These syntaxes are host choices, not the
Agent Skills standard.

Library clients supply `skill_discoverer` with async `discover() -> Sequence[Skill]`
and `load(skill) -> str`. `Skill(name, description, location)` is immutable.
Directory, builtin, and composite discoverers implement that contract. First
source wins across a composite; workspace skills override bundled names in the
Harness. Refresh replaces the catalog at incoming text, before model requests,
and before/after every tool invocation, including sequential calls in one tool
batch. There is no restart or background polling requirement.

Each load defaults to 10,000 estimated tokens: `ceil(len(text.encode("utf-8"))/4)`
with UTF-8-safe truncation, not a provider tokenizer or 1,000-line limit. Explicit
multi-skill activation shares that aggregate budget. Trusted Harness YAML
`skill_token_limit` or `NGN_SKILL_TOKEN_LIMIT` sets the same allowance (integer
1–100,000, default 10,000), independently of the manifest cap. All sources share
the top-level single-line metadata parser; extra and nested fields are ignored.
Skill context grants
no permissions and executes no scripts. Supporting resources require a separate
permitted loading mechanism; package locations are not general workspace paths.

`delegate(prompt, agent="assistant")` starts a child and returns its task ID.
Choose an explicitly configured profile for other behavior; permissions cannot exceed the parent.
Results notify the immediate parent as user-role data. `Harness.tasks.list()`,
`task_history(task_id, limit=...)`, and `continue_task(task_id, prompt)` support
inspection and retained-child follow-ups. Default depth is 2 (root 0), with
3 concurrent and 8 total child executions per root run. Children use separate
sessions/providers and the parent's active model; parent plugins/custom tools
are not automatically inherited. Execution handles are process-local.

The web host supplies `Harness.wakeup_handler` for
`schedule_wakeup(minutes=5, reason="Check the result.")`; `wake_up_in` is the
legacy tool alias. A custom host must own timer lifecycle, serialization, and
cleanup before acknowledging timers. Ordinary CLI/TUI reject unavailable timers.
Wakeups target the original root or retained child, wait for an idle boundary,
and disappear on restart. Durable channel admission is a different mechanism.

## Channels and web customization

Use `skill(name="ngn-channels")` for connector implementation, Telegram setup,
delivery, and routing. `Agent.add_channel(channel)` attaches without I/O;
`listen(session_id)` owns one shared identity across all attached sources.
The web host instead defaults each connection/chat pair to its own persisted
session. Installing a `nagents.channels` entry point and enabling a connection
in Channels is the web integration path; attaching a standalone Agent does not
configure an existing web server.

Web Settings persist an allowlist of model/profile/tool limits, delegation depth,
and dictation preferences in the workspace database with revision checks and
idle-only writes. Provider routing, credentials, trusted Python plugins, and
paths remain startup configuration. CLI/TUI do not load the web-only overrides.
The Channels panel owns connector configuration and refresh; it is distinct from
automatic skill discovery. Command registry entries are available to clients
that implement command dispatch; registration alone does not add browser routes
or a universal slash-command UI.

Web rows move to Trash in one click; Undo/Restore keeps the same ID/history.
Drafts stay browser-local. Trash retention is separate from runtime Settings:
30 days by default, configurable 1–365, with frozen per-deletion deadlines.
Expired items cannot restore even if idle cleanup lags. Delete forever requires
confirmation; Trash restore/purge uses the current `deletion_id`. Existing work
and routing guards apply. See the web guide for the preferences API and recovery.

## Verification sources

- Runnable behavior example: `examples/harness/custom_behavior.py`.
- [Configuration guide](https://abi-jey.github.io/nagents/guide/ngn-configuration/).
- [Harness guide](https://abi-jey.github.io/nagents/guide/ngn/).
- [Skills guide](https://abi-jey.github.io/nagents/guide/skills/).
- [Channel guide](https://abi-jey.github.io/nagents/guide/channels/) and
  [web guide](https://abi-jey.github.io/nagents/guide/ngn-web/).
- [Agent Skills specification](https://agentskills.io/specification) and
  [integration guidance](https://agentskills.io/client-implementation/adding-skills-support).

After implementing a change, exercise its owning text/client path and report the
actual files, behavior, and checks. In a source checkout run tooling inside its
virtual environment, including `pre-commit run --all-files`.
