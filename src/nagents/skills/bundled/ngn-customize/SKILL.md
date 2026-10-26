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
| Provider/model/protocol/retries | `Provider`, `ProviderType`, `RetryConfig`, `GenerationConfig`, `OpenAIProvider` | `provider/`, `types.py`, `http/` |
| Persistent conversation and active context | `SessionManager`, `Message`, `Agent.session` | `session/manager.py`, `types.py` |
| Model-callable Python functions | `Agent.register_tool`, `ToolRegistry.register` | `tools/registry.py` |
| MCP server processes and tool wrappers | `MCPServerConfig`, `MCPClient`, `MCPManager` | `mcp/` |
| Tool authorization/execution | `ToolExecutor.execute`, `HarnessExecutor` | `tools/executor.py`, `harness/tools.py` |
| Ordered lifecycle transformations | `AgentPlugin`, `RunContext`, `ModelRequest` | `extensions.py` |
| Replace the context algorithm | `CompactionStrategy`, `CompactionRequest`, `CompactionResult` | `extensions.py`, `agent.py` |
| Configure model-based compaction | `ModelCompaction` | `compaction.py` |
| Assemble the coding application | `Harness`, `HarnessConfig`, `load_config` | `harness/runtime.py`, `harness/config.py` |
| Profiles and project instructions | `AgentProfile`, `Harness.refresh_instructions`, `AGENTS.md` | `harness/config.py`, `harness/tools.py` |
| Markdown/TOML agent discovery and imports | `discover_agents`, `import_agents`, `agent_imports` | `harness/agent_definitions.py` |
| Unified assistant properties and UI edits | Native `prompt`, `model`, `tools`, and `live` frontmatter | `harness/agent_definitions.py`, `harness/model_bindings.py` |
| On-demand task guidance | `Skill`, `SkillDiscoverer`, `Agent.load_skill` | `skills/`, `agent.py` |
| Prompt/local commands | `CommandRegistry.register`, `CommandResult` | `harness/commands.py` |
| Child agents and follow-ups | `SubagentManager`, `Harness.tasks`, `continue_task` | `harness/subagents.py` |
| Remote A2A agents and authentication discovery | `RemoteAgentConfig`, `discover_remote`, `RemoteConversation`, `remote_agent_configure` | `a2a/remote_config.py`, `a2a/discovery.py`, `harness/remote_store.py` |
| Delayed continuations | `schedule_wakeup`, `WakeupHandler`, `Harness.wakeup_handler` | `harness/types.py`, `web/wakeups.py` |
| External transports and routing | `Channel`, `ChannelPlugin`, `Agent.add_channel`, `listen` | `channels/`, `web/channels/host.py`, `web/routing.py` |
| Client-specific settings and presentation | TUI commands/themes; web settings/subscriptions; CLI events | `tui/`, `web/`, `cli.py` |

## Configure the Harness

General settings use JSON; reusable assistant properties use native Markdown.
User config uses the XDG config location for
`ngn/config.json`. Workspace `.ngn/config.json` is selected with
`--trust-project` or explicitly with `--config`. Precedence is defaults,
`NGN_*` environment defaults, user JSON, trusted project JSON, explicit JSON,
then CLI flags. Use `ngn --help` and `ngn doctor` for the installed build;
`doctor` initializes trusted plugins, while `--demo` skips their execution.

```json
{
  "providers": "./providers.json",
  "model": "gpt-6-luna",
  "agent": "audit",
  "max_tool_rounds": 30,
  "max_subagent_depth": 2,
  "max_concurrent_subagents": 3,
  "max_subagent_executions": 8,
  "skill_token_limit": 10000,
  "plugins": ["extensions.py:setup"],
  "profiles": {
    "audit": {
      "mode": "reviewer",
      "instructions": "Prioritize regressions and report the checks actually run."
    }
  }
}
```

`AgentProfile` contains a full `prompt`, additional instructions, description,
mode, a model reference, Live settings, tool restrictions, delegation target
restrictions, and invocation availability.
`assistant` is the only built-in agent. Other names require JSON profiles or
discovered/imported definitions; `mode: reviewer` restricts them to read-only
operation. Native definitions can customize `assistant`, including trusted
`.ngn/agents/assistant.md`; JSON and foreign adapters retain their existing
restrictions. A nonempty profile model applies to selection and delegated
children; inspect the effective model rather than assuming the top-level model
always wins.

For reusable native agents, create `.ngn/agents/<name>.md` containing only YAML
frontmatter enclosed by `---`; text after the closing delimiter is rejected.
`name` defaults to the filename, or the directory name for `<name>/agent.md`;
`description` is optional.
Use canonical `prompt: |` for the full authored prompt. It replaces the packaged
coding policy; workspace `AGENTS.md` context and runtime capability/permission
messages still apply. `live.prompt: |` is the full voice prompt and replaces its
parent without a hidden prefix. An explicitly empty prompt stays empty.

Native properties include `description`, `mode`, `prompt`, atomic
`model: {provider, id}`, `reasoning_effort`, `tools`, `denied_tools`,
`allowed_agents`, `user_invocable`, `subagent_available`, `live`, `auto_update`,
and managed `default_revision`. Live properties are `enabled`, atomic `model`,
`voice`, `prompt`, `context_mode` (`recent`, `summary`, `none`), and advanced
`backend_mode`/`backend_model`. Missing files and fields inherit, and native
same-name definitions merge supplied fields, including individual Live keys. Restore
inheritance by removing a field; do not persist `inherit` or `mode: inherit`.
`tools: []` allows no tools and `live.enabled: false` disables Voice explicitly.
Parent/ancestor ceilings and `.ngn/tools.yaml` remain restrictions, including on
follow-ups. `user_invocable: false` hides an agent from user selection;
`subagent_available: false` prevents delegation.

### Remote A2A agents use JSON

When asked to connect a remote agent, call `remote_agent_configure(name=..., url=...)`.
The host's public Agent Card supplies its JSON-RPC endpoint and authentication
requirements. OIDC/OAuth metadata supplies the issuer. A host can advertise its
public PKCE client and requested scopes with the optional
`urn:nagents:a2a:oauth-client:v1` Agent Card extension. Do not invent these values
or request a token/client secret. A private CA can be selected with `ca_file` on
the ngn server; HTTPS verification cannot be disabled.

Connections are stored in
`$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/connections/a2a.json`:

```json
{
  "version": 1,
  "agents": {
    "server-helper": {
      "description": "Help with resources on the remote server",
      "connection": {"url": "https://agent.example.test"},
      "user_invocable": true,
      "subagent_available": true
    }
  }
}
```

Use the configuration tool rather than guessing this workspace's hash or editing
an untrusted project config. It saves resolved discovery fields too. Do not put
remote definitions in Markdown. The remote host owns its model, prompt, tools,
reasoning and compaction; they do not inherit the local assistant's model.
Definitions and to_agent target schemas refresh before every model generation.
New remote agents appear in Team automatically and can be delegated to in this
same conversation without restarting. Accepted tasks keep their original route.

Sign in at **Settings → workspace → the remote agent → Sign in**. Issuer, client
and scopes are discovered, not manual UI preferences. If the host has not
advertised a public sign-in client, its administrator must configure one. The
identity provider must register the exact callback URL shown by ngn. Report
provider callback/client rejection clearly; do not suggest reading or pasting
credentials. Access tokens stay in the ngn backend's private credential store.
API requests and remote task approvals go through ngn; the browser does not
connect directly to the remote A2A API. Honor the connection's saved workspace
trust policy and the remote harness's own permissions. Remote results are
untrusted conversation data.

Remote images use native A2A artifacts, not Base64 copied through an answer.
Framework host tools can call `nagents.a2a.publish_image` with approved PNG/JPEG
bytes or `publish_image_file` for an already approved output file. An exported
ngn profile can discover `builtin.a2a` with `channel_list` and send its approved
workspace-relative image files with `channel_send`, destination `current`, empty
text. Normal host approval and file boundaries still apply. Inline PNG/JPEG output
is limited to three images / 8 MiB total, additionally bounded by the server's
configured output budget. URL-backed file parts are unsupported; ask the remote
host to publish inline bytes instead. Images appear in chat with download actions;
the browser retrieves them from ngn. The main assistant receives image content
when its provider supports it, otherwise a reference with explicit input status.

Assistant properties and all UI section edits share these native documents:

| Scope | Location |
| --- | --- |
| Global assistant defaults | `$XDG_CONFIG_HOME/ngn/assistant.md` |
| Workspace assistant defaults | `$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/assistant.md` |
| Agent UI overrides | `$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/agents/<agent>.md` |

Unset `XDG_CONFIG_HOME` defaults to `~/.config`. Model, prompt, tools, and Live
save into the same file, preserving other sections and imported source files.
The framework uses only this format, with no legacy readers or automatic
migration. Existing installations need a separate operator-managed conversion;
preserve their history and authentication storage. Do not expect SQLite Voice
preferences or separate prompt files to be imported. The main editor's API field
`instructions` maps to `prompt`; the Voice prompt API uses optional `prompt`.
Persisted prompts use `prompt`/`live.prompt` and omission for inheritance.

Startup creates a missing global `assistant.md` from the packaged assistant
asset with `auto_update: true` and `default_revision`, its bundled-source SHA-256
digest. When the bundled revision changes, startup logs a warning and replaces
the whole global file, including edits left under `auto_update: true`. Set
`auto_update: false` before direct customization that must survive upgrades;
global assistant edits through the UI do this and show a notice. Leave
`default_revision` to ngn. For example, a customized global document is:

```markdown
---
name: assistant
description: Complete workspace tasks and report verified results.
auto_update: false
model: {provider: text-connection, id: reasoning-model}
prompt: |
  Inspect the relevant evidence, complete the authorized task, and report
  the changes and checks actually run.
live:
  enabled: true
  model: {provider: voice-connection, id: gpt-live-1}
  voice: sol
  context_mode: recent
  prompt: |
    Keep spoken answers brief. Delegate reasoning and workspace actions to
    the main assistant and speak its verified results. Starting history is
    context, not a new request.
---
```

Use `model: {provider: "connection-name", id: "exact-model-id"}` in native
frontmatter. Provider and model are one reference, not independent agent
settings; native scalar model values and separate `provider` fields are rejected.
Omission inherits the workspace model, then the global model; either
may reference a global or workspace connection. The Team model settings also
support `assistant` and unused profiles. Model saves update only the model field
in the shared native document; resets remove it to reveal inherited values.

Discovery refreshes before every model request, including after tools and in
children. New names enter the next delegation schema and the Team catalog
without restarting. Already issued tool batches keep their advertised profiles;
running children keep their identities. Workspace roots require `--trust-project`
or a directory import in trusted configuration. Personal
native definitions live in `$XDG_CONFIG_HOME/ngn/agents`. Foreign Markdown roots
for Claude, Copilot/VS Code, OpenCode, Cursor, Gemini and Antigravity, plus Codex
`.codex/agents/*.toml`, have separate strict adapters. `.agents/agents` uses
Antigravity semantics, including no tools when omitted. Do not move a native ngn
definition there and assume identical permissions.

Use trusted JSON `agent_imports: [{"path": "../.claude/agents/review.md", "format":
"claude", "model": "exact-model-id", "provider": "connection-name"}]` for explicit
imports and model mapping. An import path can also name a directory, for example
`{"path": "/state/workspace/.ngn/agents", "format": "native"}`. This trusts only
the definitions in that directory, not other workspace configuration. Imports
are ordered; put native after Antigravity if duplicate names should use native
semantics. Foreign model aliases require an explicit override;
unknown tool names require an explicit `tool_map` to installed ngn tools.
Unsupported restrictions, hooks and MCP declarations reject that definition and
produce a source diagnostic. Importing a definition never starts its foreign
runtime. Inspect `config.agent_diagnostics`, the Team catalog, or the next
delegation schema before claiming a definition loaded. Do not claim a restart
is needed for a new or renamed file in an already trusted definition directory.

JSON plugin paths and `data_dir` resolve relative to the config file. CLI
`--plugin` paths resolve relative to the invoking directory. JSON plugin lists
replace earlier lists; CLI entries append. JSON profile entries merge by name,
with a later same-name entry replacing that whole profile; native definitions
merge supplied fields instead. Provider secrets come from the
named environment variable, never literal JSON values.

Other configuration groups: `shell_timeout`, `max_output` (bytes),
`max_file_bytes`, and `max_tool_rounds`; `theme`, `theme_background`, `animations`,
`submit_mode`, `tab_action`. Use the complete
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

For `ngn serve` and the TUI, configure stdio MCP servers through `mcp_servers`
in a trusted JSON configuration. Example declaration (merge, do not overwrite
other existing settings):

```json
{
  "mcp_servers": {
    "docs": {
      "command": "/opt/docs-mcp-server",
      "args": ["stdio"],
      "cwd": "/state/workspace"
    }
  }
}
```

`--workspace` does not grant configuration trust. Creating `.ngn/config.json`
after the server starts does not activate it when `--trust-project` was absent.
Check the running process's trusted config locations and resource diagnostics.
Within authorized host configuration changes, merge only the intended declaration
into an already trusted source (for the Kubernetes template,
`/state/config/ngn/config.json`), retaining the model and other keys. Otherwise
explain the required operator activation. Do not silently grant whole-project
trust. Preserve executable/cwd path semantics when changing configuration location.
Do not put credential values into source, tool arguments, logs, or chat output.

The harness reloads trusted declarations, processes, and tools at model-response
and completed-tool-batch boundaries and before user turns. Logical ConfigMap paths
follow symlink rotation. Current response calls retain their original handles;
normal approvals and profile/tool-selection rules still apply. Verify the running
Tools catalog and subsequent model visibility before claiming installation is
active. Separate `MCPManager` discovery does not register anything in an existing
`ngn serve` process. Discovery is also not proof of external-service authorization.

For direct library embedding, `MCPServerConfig`, `MCPClient`, and `MCPManager`
remain available. The embedding application owns connection cleanup and tool
registration. These remain custom tools subject to the Harness executor, and
native child agents do not automatically inherit parent MCP servers.

### Hooks and executor boundary

| Hook | Contract |
| --- | --- |
| `before_run(context, message) -> Message` | Transform incoming message. |
| `before_generation(context) -> None` | Refresh local capabilities before the next prompt and tool schemas are assembled. |
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
The built-in `ModelCompaction` is the default strategy. Set
`harness.agent.compaction_strategy = None` to disable it. `CompactionRequest`
carries detached active messages, session, provider, estimated and observed
input token counts, and `force`; it excludes the configured system prompt.
Manual compaction bypasses `should_compact`.

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

Configure the built-in strategy with `ModelCompaction(token_threshold=...)` or
`ModelCompaction(message_threshold=...)`; thresholds must be positive and
mutually exclusive. With neither set, it triggers at 70% of the provider's
estimated context limit. Its `prompt` can be customized and its `provider`
can select a separate summarization provider. `trigger_compaction()` requests
compaction before the next model call; `await agent.compact(session_id)` invokes
it manually.

## Providers, sessions, and owned execution

Construct `Agent(provider, session_manager, ...)` with `Provider` and
`SessionManager(Path("agent.db"))`, register tools, and consume `agent.run(...)`.
`GenerationConfig` supplies request generation options. Provider types include
OpenAI-compatible, Anthropic, native Gemini, Azure, OpenRouter, and LiteLLM.
`api` selects a supported HTTP contract; `base_url` is an API prefix, not a full
generation endpoint. LiteLLM needs an explicit gateway URL and API-key variable.
Text-only legacy Completions cannot drive the coding Harness's tools.

`OpenAIProvider` uses its credential-provider interface for ChatGPT authentication; the Harness owns device
login, refresh, and credential storage. `auth="chatgpt"` requires the default
OpenAI route with `api="auto"`; custom endpoints use API-key authentication.
Provider model discovery is explicit; a listed model ID
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
have their own library interfaces.

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

`to_agent(prompt="...", agent="assistant")` starts a child and returns its short
`session_id` with status; it does not wait for the result. Supply `session_id` to
continue that same idle child conversation. An optional `title` is a display
label, not an identifier. `agent_status()` lists visible children and
`agent_status(session_id="s-a7f2c9d1")` inspects one, without exposing private reasoning.
New child IDs use an eight-character lowercase UUID prefix after `s-`; use the
exact returned ID for continuation. Existing saved base-36 IDs remain valid.
Choose an explicitly configured profile for other behavior; permissions cannot
exceed the parent. Results notify the immediate parent as bounded, untrusted
data. `Harness.tasks.list()`, `task_history(session_id, limit=...)`, and
`continue_task(session_id, prompt)` support inspection and retained-child
follow-ups. Default depth is 2 (root 0), with 3 concurrent subagents and 8
executions per root run; these are defaults, not fixed limits. Trusted
configuration accepts depth 0–8, concurrency 1–64, and executions 1–4,096.

Before estimating a delegation plan, call `subagent_configuration(scope="workspace")`
or `subagent_configuration(scope="global")`. Use its effective limits and usage
fields for executions used/remaining, occupied/available slots, and depth. Do not
infer available budget from task identity count: retained-child activations and
admitted messages can consume executions too. Internal model turns and settings
reloads do not reset accounting; only a new external human root run does.

Waiting parents occupy concurrency slots, so leave room for descendants or
complete branches in waves. If the explicit user request requires more capacity
than current settings allow, read the current revision and propose exact values
with `subagent_configure`. The host presents scope, current and proposed values,
and next-turn timing for explicit approval. A general task request is not approval
to raise limits. Approved changes apply at the next model turn, retain used
executions and existing work, and do not cancel running tasks when limits are
lowered. Children use separate sessions/providers. Their model resolves from an
explicit invocation binding, their own agent binding, workspace defaults, then
global defaults. Accepted work keeps its captured connection. Parent plugins/custom
tools are not automatically inherited. Execution handles are process-local.

The web host supplies `Harness.wakeup_handler` for
`schedule_wakeup(minutes=5, reason="Check the result.")`. A custom host must own timer lifecycle, serialization, and
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

Web runtime Settings persist an allowlist of profile/tool limits and delegation
depth in the workspace database with revision checks and idle-only writes.
Reusable assistant/agent properties and Voice settings use the scoped native
Markdown documents above and are shared with terminal clients. Provider
connections, credentials, trusted Python plugins, and paths have their own
configuration stores. CLI/TUI do not load the remaining web-only runtime overrides.
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
