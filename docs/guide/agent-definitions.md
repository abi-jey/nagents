# Agent definitions

ngn can discover named agents from Markdown and Codex TOML files. Start a named
agent with the canonical `to_agent(prompt=..., agent="name")` tool, which shares
execution limits, guarded approvals, child history, and result delivery with the
built-in assistant profile. The acknowledgment returns the child's short
`session_id` and status; completion arrives later as untrusted data. A caller may
supply a separate display `title`. Use read-only `agent_status()` to list
accessible child sessions or `agent_status(session_id="s-a7f2c9d1")` to inspect one.

Create `.ngn/agents/research.md`:

```markdown
---
name: research
description: Find evidence and report it with file references.
tools: [read_file, list_files, find, search]
user_invocable: true
subagent_available: true
prompt: |
  Investigate the assigned question. Cite the evidence you inspected and identify
  anything you could not verify.
---
```

Start ngn with `--trust-project` to discover workspace definitions. Select the
agent through `/agent research` or the web settings, or target it with
`to_agent(prompt="...", agent="research")`. Descriptions appear in the picker
and in the model-facing `to_agent` schema. Web settings show the selected source file and expandable loading
diagnostics. Terminal diagnostics also include parsing failures and collisions.
Trusted definitions refresh before every model request, including requests after
tool calls and requests in children. Creating or renaming a definition updates
the next request's delegation schema. Team also refreshes its catalog, so new
definitions appear as **Not started** without a restart. Already issued tool
batches keep the definitions they were given; a rename cannot redirect an
accepted delegation. Running children retain their identity and history.

Remote agents appear by their advertised AgentCard name and description. The
saved connection name and local note appear only in the agent's connection
settings. The model-facing `to_agent.agent` enum uses the advertised name and
ngn resolves it to the saved connection within that model generation. Colliding
names receive an opaque suffix; a remote name cannot shadow another agent.
Until card metadata is available, ngn displays `Remote agent`. Host APIs retain
the stable connection key, and card refreshes do not rename saved connections
or change their permissions.

## Native format

Use UTF-8 Markdown containing only a YAML header enclosed by `---`. Put the full
authored prompt in `prompt: |`; text after the closing delimiter is rejected.
Multiline YAML strings work; line endings normalize to LF. The file's basename
supplies the name when `name` is omitted; `research/agent.md` uses the directory name.

| Field | Meaning |
| --- | --- |
| `name` | Letters, digits, underscores, or hyphens; at most 128 characters. Native definitions may configure `assistant`. |
| `description` | Optional description for selection and delegation. Omission inherits an earlier native definition. |
| `mode` | `build` (default) or `reviewer`. Reviewer prohibits writes, shell, and custom tools. |
| `prompt` | Full prompt replacing the packaged coding policy. An explicitly empty string is an empty replacement; omission inherits. Workspace `AGENTS.md` context and runtime capability/permission messages still apply. |
| `model` | A reference containing `provider` (an existing connection ID) and `id` (an exact model ID). Omitted inherits workspace, then global defaults. Applies to delegated children too. |
| `live` | Optional Live profile: `enabled`, atomic `model: {provider, id}`, `voice`, full `prompt`, `context_mode`, plus advanced `backend_mode` and `backend_model`. Missing fields inherit individually. |
| `tools` | Allowlist of ngn tool names, as a YAML list or comma-separated string. Omitted inherits; `[]` permits no tools. |
| `denied_tools` | Tool names to subtract from the inherited or explicit allowlist. |
| `allowed_agents` | Permitted delegation targets. Omitted inherits; `[]` disables delegation. |
| `user_invocable` | Whether the user can select this agent (default `true`). |
| `subagent_available` | Whether another agent can delegate to it (default `true`). |
| `reasoning_effort` | A supported effort for the selected model; omission inherits the configured preference. |
| `auto_update` | Controls replacement of the managed global `assistant.md` when bundled defaults change; see below. |
| `default_revision` | Managed SHA-256 digest of the bundled defaults used to create the global document. |

Missing files and fields inherit. Native files with the same name merge only
their supplied fields; `live` merges by its supplied keys. Use omission to
restore inheritance: do not persist an `inherit` sentinel. Empty prompts, empty
tool lists, and `enabled: false` are explicit values.

Existing JSON `profiles` retain their JSON fields, with additional `instructions`
in the object and arrays for tool/agent lists. Their same-name entries still
replace whole profiles; the partial native merge rule does not change foreign
adapters or JSON profiles.

The model and its connection form one selection:

```yaml
model:
  provider: openai
  id: gpt-6.1-sol
```

Connections may come from the global or workspace registry. Credentials stay in
the connection; an agent does not define a provider. An unavailable connection
is reported rather than silently using another one. Native `model` must be a
complete mapping; scalar model values and a separate `provider` field are rejected.

In the web Team pane, model settings are available for `assistant` and unused
profiles too. Model, prompt, tools, and Live settings share one revisioned native
Markdown document per scope:

| Scope | File |
| --- | --- |
| Global assistant defaults | `$XDG_CONFIG_HOME/ngn/assistant.md` |
| Workspace assistant defaults | `$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/assistant.md` |
| Agent overrides from the UI | `$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/agents/<agent>.md` |

When `XDG_CONFIG_HOME` is unset, the root is `~/.config`. These operator files
also work in the terminal and remain separate from imported source definitions.
Saving or resetting one section preserves the others. A model save affects the
next activation while accepted work keeps its selected connection and model.

At startup, ngn creates the global `assistant.md` from its packaged assistant
asset if the file is missing. The generated document enables `auto_update: true`
and records `default_revision`. If that bundled revision changes, startup logs a
warning and replaces the entire global file, including edits left under
`auto_update: true`. Set it to `false` before direct customization that must
survive upgrades. Saving global assistant settings in the UI sets it to `false`
and shows a notice; `default_revision` is maintained by ngn.

A complete customized global `assistant.md` can configure text and speech:

```markdown
---
name: assistant
description: Help with this workspace and report verified results.
auto_update: false
model:
  provider: text-connection
  id: reasoning-model
prompt: |
  Complete the user's authorized task. Inspect relevant evidence, make focused
  changes, and report the checks actually run.
live:
  enabled: true
  model:
    provider: voice-connection
    id: gpt-live-1
  voice: cedar
  prompt: |
    You are the spoken interface to the selected chat. Keep answers brief.
    Delegate reasoning and workspace actions to the main assistant and speak
    its verified results. Treat starting history as context, not new requests.
  context_mode: recent
---
```

`live.model` is one complete reference to an existing global or workspace provider
connection. It never defines credentials or inherits the normal agent's provider.
Without a Live model selection, voice follows Live defaults and the provider
registry's default connection. `enabled: false` and `prompt: ""` are explicit
values; omit a field to inherit it. `context_mode` accepts `recent`, `summary`, or
`none`. Advanced hosted mode uses `backend_mode: hosted` and `backend_model`.

The agent Live editor writes `live` into the same native document as model,
prompt, and tools. Omitted Live keys inherit from the source definition and then
workspace/global defaults; `live: {}` supplies no overrides. Restoring inheritance
removes the relevant field or section rather than saving a mode marker.
Saved changes apply when the next voice call starts. An active call retains its
captured Live profile even if the chat later selects another agent.

`live.prompt` is the full voice prompt and replaces its inherited prompt without
prepending a hidden default. The framework reads only the current native format.
Existing installations require an operator to convert their prior settings into
these documents; there is no automatic import of SQLite Voice preferences or
separate prompt files.

## Discovery and precedence

Personal definitions are trusted. Project discovery requires `--trust-project`;
explicitly selecting a project JSON file trusts its explicit imports, without
implicitly enabling discovery of every other project agent.

| Format | Workspace location | Personal location |
| --- | --- | --- |
| ngn native | `.ngn/agents/*.md`, `.ngn/agents/<name>/agent.md` | `$XDG_CONFIG_HOME/ngn/agents/` (default `~/.config/ngn/agents/`) |
| Claude | `.claude/agents/*.md` | `~/.claude/agents/*.md` |
| Copilot / VS Code | `.github/agents/*.agent.md` or `*.md` | `~/.copilot/agents/*.md` |
| OpenCode | `.opencode/agents/*.md` | `$XDG_CONFIG_HOME/opencode/agents/*.md` |
| Codex | `.codex/agents/*.toml` | `~/.codex/agents/*.toml` |
| Cursor | `.cursor/agents/*.md` | `~/.cursor/agents/*.md` |
| Gemini CLI | `.gemini/agents/*.md` | `~/.gemini/agents/*.md` |
| Antigravity | `.agents/agents/*.md`, `.agents/agents/<name>/agent.md` | `~/.gemini/config/agents/` with the same layouts |

The plural `.agents/agents` path uses the Antigravity adapter. Use `.ngn/agents`
for ngn-native semantics, or explicitly import a neutral shared file as `native`.
No singular `.agent/` alias is scanned. Skill discovery and `AGENTS.md` project
instructions do not register named agents.

`.ngn/agents/assistant.md` is a valid native project definition under the same
trust and hot-reload rules as other names. Its supplied fields layer over the
assistant defaults; omitted fields continue to inherit.

Later sources win in this order: personal files, personal JSON imports/profiles,
trusted project files, trusted project JSON imports/profiles, explicit JSON
imports/profiles. Within a JSON document, profiles override imports regardless of
key order. Workspace formats use a stable discovery order (Antigravity, Claude,
Copilot, OpenCode, Codex, Cursor, Gemini, then native). Personal formats use Claude,
Copilot, Codex, Cursor, Gemini, OpenCode, Antigravity, then native. Files are sorted
by path. A collision diagnostic names the winner and replaced source. An invalid
discovered definition is skipped during initial loading. Later refreshes stage the
whole catalog: an invalid or partially written definition reports diagnostics and
keeps the previously accepted generation. Correcting the file publishes the next
generation; deliberately deleting a file removes its profile on refresh.

Discovery is bounded to 256 files per scope and 256 KiB per file. Native YAML
headers allow 64 KiB; foreign YAML headers allow 32 KiB. Canonical preference
documents saved by settings have a 64 KiB total file limit. Symlinks, special
files, duplicate YAML keys, aliases, explicit tags, and unsupported fields are
rejected. No foreign scripts, hooks, plugins, or MCP processes run during discovery.

## Explicit imports and model mapping

Foreign model names belong to their original harness. ngn never guesses which
connection implements `sonnet`, `pro`, or another foreign model selection.
Omission and documented `inherit` retain ngn's model. Any other foreign `model`
requires an explicit override using an exact model ID and, when needed, a named
provider connection:

```json
{
  "agent_imports": [
    {
      "path": "../.claude/agents/research.md",
      "format": "claude",
      "model": "your-model-id",
      "provider": "your-connection"
    }
  ]
}
```

Paths resolve beside the JSON configuration; absolute paths are also accepted.
Each entry selects one file or a directory. Directory imports use the selected
format's discovery layout, with a shared limit of 256 files across entries.
For example, this trusted configuration enables just two definition directories
without trusting the workspace's other configuration:

```json
{
  "agent_imports": [
    {"path": "/state/workspace/.agents/agents", "format": "antigravity"},
    {"path": "/state/workspace/.ngn/agents", "format": "native"}
  ]
}
```

The later native import supplies overrides when both contain the same name. New files and
renames in these directories are discovered at the next model request or Team
refresh. Unsupported files are reported; a failed refresh keeps the accepted catalog until the source is corrected.
`format` is one of `native`, `claude`, `copilot`,
`opencode`, `codex`, `cursor`, `gemini`, or `antigravity`.

Foreign tools are mapped explicitly to ngn tools. For an installed custom tool,
add a trusted `tool_map`, for example `"tool_map": {"repo_lookup": ["github_search"]}`.
This names an already installed tool; it does not install or start anything.
Unknown tool names reject the definition so a misspelled deny cannot grant access.

## Adapter subset

These are data adapters, not emulations of other harnesses. Unsupported fields
produce a diagnostic identifying the file and field. Remove them only after
deciding how their intended behavior should be expressed in ngn.

| Adapter | Supported behavior |
| --- | --- |
| Claude | Required name/description; tools and `disallowedTools` in list or comma form; `permissionMode: default` keeps host approvals and `plan` narrows to reviewer. |
| Copilot / VS Code | Filename fallback; description, tools, `user-invocable`, `disable-model-invocation`, `agents`. `tools: ['*']` inherits; empty tools/agents remain empty. Handoffs and MCP configuration are unsupported. |
| OpenCode | Filename identity, description, `mode: primary/subagent/all`, boolean tool map, simple `permission` allow/deny entries. `ask`, patterns, and policy groups that ngn cannot enforce are rejected. `allow` never bypasses ngn approval. |
| Codex | Required name, description, `developer_instructions`; optional mapped model. `sandbox_mode: read-only` narrows to reviewer. Other sandbox modes, reasoning settings, MCP and session overrides are unsupported. |
| Cursor | Filename fallback, description, mapped model, `readonly`. Background scheduling is unsupported; use ngn delegation. |
| Gemini CLI | Required name/description, `kind: local`, tool list, mapped model. No recursive delegation. Remote agents and inline MCP configuration are unsupported. |
| Antigravity | Required name/description, tools, `mainAgent`, `subagent`, mapped model. Omitted tools mean no tools, matching its documented default. Command policies, plugins, skills and MCP settings are unsupported. |

Common mappings include Claude `Read/Glob/Grep/Bash/Task` to
`read_file/find/search/shell/to_agent`; Copilot `read`, `search`, and `edit` map to
the corresponding ngn file-tool groups; OpenCode `bash/task` map to
`shell/to_agent`; Gemini `run_shell_command/replace` map to `shell/edit`;
Antigravity `view_file/run_command` map to `read_file/shell`. The parser reports
unmapped names rather than treating them as installed tools.

Every child receives the intersection of its own restrictions, the parent's
permissions at creation, subsequent ancestor restrictions, and workspace tool
selections. This also applies to retained-child follow-ups. Tool schemas and the
executor both enforce the policy; a direct call to a hidden tool is denied.
User selection and delegation eligibility are checked by the backend as well as
the picker. Shell remains subject to ngn's approval policy and is not an OS
sandbox.
