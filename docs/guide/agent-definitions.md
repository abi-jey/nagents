# Agent definitions

ngn can discover named agents from Markdown and Codex TOML files. They run through
the same `delegate(prompt, agent="name")` lifecycle as JSON profiles: shared task
limits, approval handling, child history, follow-ups, and result delivery.

Create `.ngn/agents/research.md`:

```markdown
---
name: research
description: Find evidence and report it with file references.
tools: [read_file, list_files, find, search]
user_invocable: true
subagent_available: true
---
Investigate the assigned question. Cite the evidence you inspected and identify
anything you could not verify.
```

Start ngn with `--trust-project` to discover workspace definitions. Select the
agent through `/agent research` or the web settings, or delegate to it from
another agent. Descriptions appear in the picker and the model's delegation tool
schema. Web settings show the selected source file and expandable loading
diagnostics. Terminal diagnostics also include parsing failures and collisions.
Trusted definitions refresh before every model request, including requests after
tool calls and requests in children. Creating or renaming a definition updates
the next request's delegation schema. Team also refreshes its catalog, so new
definitions appear as **Not started** without a restart. Already issued tool
batches keep the definitions they were given; a rename cannot redirect an
accepted delegation. Running children retain their identity and history.

## Native format

Use UTF-8 Markdown with a YAML header enclosed by `---`, followed by nonempty
instructions. Multiline YAML strings work; line endings normalize to LF. The file's basename supplies the name
when `name` is omitted; `research/agent.md` uses the directory name.

| Field | Meaning |
| --- | --- |
| `name` | Letters, digits, underscores, or hyphens; at most 128 characters. `assistant` is reserved. |
| `description` | Required, nonempty description for selection and delegation. |
| `mode` | `build` (default) or `reviewer`. Reviewer prohibits writes, shell, and custom tools. |
| `model` | A reference containing `provider` (an existing connection ID) and `id` (an exact model ID). Omitted inherits workspace, then global defaults. Applies to delegated children too. |
| `live` | Optional Live profile: `enabled`, atomic `model: {provider, id}`, `voice`, `instructions`, `context_mode`, plus advanced `backend_mode` and `backend_model`. Missing fields inherit workspace, global, then built-in Live defaults. |
| `tools` | Allowlist of ngn tool names, as a YAML list or comma-separated string. Omitted inherits; `[]` permits no tools. |
| `denied_tools` | Tool names to subtract from the inherited or explicit allowlist. |
| `allowed_agents` | Permitted delegation targets. Omitted inherits; `[]` disables delegation. |
| `user_invocable` | Whether the user can select this agent (default `true`). |
| `subagent_available` | Whether another agent can delegate to it (default `true`). |

Existing JSON `profiles` support the same fields, with `instructions` in the JSON
object and arrays for tool/agent lists. JSON profiles may omit `description`.

The model and its connection form one selection:

```yaml
model:
  provider: openai
  id: gpt-6.1-sol
```

Connections may come from the global or workspace registry. Credentials stay in
the connection; an agent does not define a provider. An unavailable connection
is reported rather than silently using another one. Older scalar `model` and
separate `provider` fields remain readable; do not combine a model reference
with a separate provider field.

In the web Team pane, model settings are available for `assistant` and unused
profiles too. Saves share revisioned Markdown preference files under
`$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/agent-models/`. **Inherit**
uses workspace/global defaults; **Restore definition** removes the override and
uses the original definition again. A save affects the next activation while
accepted work keeps its selected connection and model. Model resets preserve the
file's other preference sections.

A Live profile configures speech separately from the normal agent model:

```yaml
model:
  provider: text-connection
  id: reasoning-model
live:
  enabled: true
  model:
    provider: voice-connection
    id: gpt-live-1
  voice: cedar
  instructions: Keep spoken answers brief.
  context_mode: recent
```

`live.model` is one complete reference to an existing global or workspace provider
connection. It never defines credentials or inherits the normal agent's provider.
Without a Live model selection, voice follows Live defaults and the provider
registry's default connection. `enabled: false` and `instructions: ""` are explicit
values; omit a field to inherit it. `context_mode` accepts `recent`, `summary`, or
`none`. Advanced hosted mode uses `backend_mode: hosted` and `backend_model`.

The agent Live editor writes a `live` section into the same operator preference
file as model, instructions, and tools. A saved Live section replaces the source
Live layer: `live: {}` explicitly uses workspace/global defaults. **Restore
definition** removes only that section and reveals the native or JSON profile.
Saved changes apply when the next voice call starts. An active call retains its
captured Live profile even if the chat later selects another agent.

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

Discovery is bounded to 256 files per scope and 256 KiB per file, with a 32 KiB
YAML header limit. Symlinks, special files, duplicate YAML keys, aliases, explicit
tags, and unsupported fields are rejected. No foreign scripts, hooks, plugins,
or MCP processes run during discovery.

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

The later native import wins when both contain the same name. New files and
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
`read_file/find/search/shell/delegate`; Copilot `read`, `search`, and `edit` map to
the corresponding ngn file-tool groups; OpenCode `bash/task` map to
`shell/delegate`; Gemini `run_shell_command/replace` map to `shell/edit`;
Antigravity `view_file/run_command` map to `read_file/shell`. The parser reports
unmapped names rather than treating them as installed tools.

Every child receives the intersection of its own restrictions, the parent's
permissions at creation, subsequent ancestor restrictions, and workspace tool
selections. This also applies to retained-child follow-ups. Tool schemas and the
executor both enforce the policy; a direct call to a hidden tool is denied.
User selection and delegation eligibility are checked by the backend as well as
the picker. Shell remains subject to ngn's approval policy and is not an OS
sandbox.
