# Agent Designer (Experimental)

Agent Designer is a web canvas for creating general-purpose agents, chatting with
them, and inspecting their execution. Open **Agent Designer** from the `ngn serve`
header. The terminal client can run its YAML definitions; the canvas is web-only.

The Designer is **experimental**. Its editor and definition format are evolving.
Choose **New** to open the creation dialog: enter the design ID and first agent,
configure the provider, API contract and credential reference, then **Create
design**. Creation saves the YAML without making a model request. The design ID
is shown in the status bar rather than edited in the agent sidebar.

Tool selection is configurable; registered tool definitions are read-only.
Expand a selected built-in tool to inspect its canonical description and schema.
MCP discovery shows the server's definitions. Nonempty tool-description overrides
in older YAML must be removed; descriptions and parameters come from the library
or MCP server. Agent instructions and delegation guidance remain configurable.

## Channels (ngn serve only)

Use **Agent Designer → Channels → Configure connections** to create or edit a
Telegram or other installed channel connection using the existing Channels form.
Refresh connections, select the agent for each connection, then choose
**Save & apply channel routes**. This saves the YAML and publishes a resolved
instruction snapshot for new channel conversations.

```yaml
channels:
  telegram: assistant
  support-bot: support
```

Keys are existing connection IDs, not plugin names; values are agent IDs in this
design. Connector settings and write-only credentials remain in the web host's
channel store. YAML contains no bot tokens. Each connection has one active design
route; remove it from its previous design and apply before assigning another.

Channel listeners and routing are supported **only by `ngn serve`**. Loading this
YAML in the TUI or standalone runtime does not open channels or add channel tools.
The web host continues to own one listener per connection and the durable inbox.

Existing conversations retain their current runtime and history. In Telegram,
use `/new` after applying a route to start with the selected designed agent. The
first run pins its resolved definition to that session; subsequent Telegram and
web messages use that snapshot, including after a server restart. Publishing a
new revision or removing a route affects new conversations, not existing ones.

Routed roots receive the normal scoped channel tools. Existing auto-reply,
in-chat approval, chat ownership, queue, and cancellation rules still apply.
Delegated agents retain only their explicit tools and instructions; they do not
inherit channel tools. Channel runs also appear in the Designer execution history
with model context and request traces.

## Designer basics

The right-hand chat panel's **Chat history** lists conversations; selecting one restores its
persisted messages across every turn. Choose **＋ New chat** in that list to start a separate conversation.
Sending continues the selected conversation by default. **Execution history**
selects an individual run for debugging without reducing the visible chat to that
run. Existing conversations keep their pinned agent and provider configuration.

New designs and **Live example** inherit the current web provider settings,
including saved overrides. Open **Agent settings → Provider settings** (or the
provider under **Resources**) to choose **Responses API**, **Chat Completions API**,
**Messages API**, or automatic routing. The same form exposes provider type, model
ID, endpoint prefix, authentication, credential reference, and Azure API version.
Provider resources are shared: edits apply to agents using that resource in new
conversations. **Advanced** exposes temperature, output token limit, top-p, stop
sequences, and model-round limits; blank generation fields use provider defaults.

Install the current checkout with the `web` extra and build the frontend as
described in [web development](../development/contributing.md#web-client-development).
`ngn serve --demo` exercises the editor and inspector without model calls or MCP
processes. Demo replies are scripted and do not execute delegation.

The designer uses the web harness's shared color and control tokens. The compact
canvas supports pan/zoom, automatic arrangement, and click-to-connect delegation
ports on all four sides. Drag a port to another agent, or click the source and
target ports. Dropping on an agent's border attaches at that position. Select an
existing link to expose its source and target handles; drag either handle to
reconnect to another side or another agent. Dropping on empty space or pressing
Escape cancels the change and preserves the original link. **Test & inspect**
opens chat on the right and an execution inspector below the canvas; the default step
list hides per-token noise while **All raw events** retains access to every record.
The canvas follows the panel's dimensions when resized, including when opening
the inspector. In test chat, **Ctrl+Enter** or **Cmd+Enter** sends a message;
plain Enter inserts a newline.

For an online demonstration, launch without `--demo`, open the designer and click
**Live example**. This loads a coordinator and analyst using the host's provider.
Click **Send** to execute a real delegated calculation. Open **API requests** to
inspect the serialized requests, or select each agent's model context. With an
existing `ngn login chatgpt` session, the example uses that login automatically.

## Design, then run

1. Create a design, give it a stable ID, and add agents.
2. Select an agent to set instructions, a provider, tools, and delegation targets.
3. Drag nodes to arrange the canvas. An edge means the source may delegate a task
   to the target; it does not automatically execute the target.
4. Use **Resources** for shared providers, secret references, and MCP servers.
   This panel edits JSON resource objects; **YAML** edits the complete document.
5. **Validate** refreshes the effective instructions and tool-schema preview.
6. **Save YAML** writes `.ngn/designs/<id>.yaml` atomically. Revision conflicts
   require reloading the saved design.
7. Select any agent and send a message in designer chat.

General-purpose agents do not receive the coding assistant's system prompt or
workspace `AGENTS.md` contents. Coding capabilities are individually selected
built-ins: `read_file`, `list_files`, `find`, `search`, `edit`, `write`, and `shell`.
Their existing workspace boundaries and approval behavior still apply.

Visual edits generate canonical YAML and do not preserve comments. Direct YAML
editing preserves the source text when saved without subsequent visual edits.
Duplicate keys, aliases, anchors, unknown fields, and invalid references are rejected.

## Definition example

```yaml
version: 1
id: research-team
entrypoint: coordinator
defaults:
  provider: primary
  max_subagent_depth: 2
secrets:
  primary_key:
    source: env
    name: OPENAI_API_KEY
providers:
  primary:
    type: openai
    model: gpt-4.1
    secret: primary_key
agents:
  coordinator:
    instructions:
      text: Delegate research when useful, then synthesize the findings.
    invokes:
      - agent: researcher
        description: Read project documents and summarize relevant findings.
  researcher:
    instructions:
      text: Research the delegated question. Cite the files you used.
    tools:
      - ref: builtin.read_file
      - ref: builtin.list_files
    generation:
      max_tokens: 4096
    max_tool_rounds: 20
```

An agent's `provider` selects a named provider; otherwise `defaults.provider` is
used. A provider owns `type`, `model`, `base_url`, `api`, `api_version`, and a named
`secret`. Generation settings are `temperature`, `max_tokens`, `top_p`, and `stop`;
omitted settings use provider defaults. Provider-specific support still applies.

`instructions.file` is a bounded workspace-relative UTF-8 file. Its contents are
prepended to `instructions.text` and snapshotted when a conversation starts.

## Secret references and MCP

Secret definitions contain references, not credential values:

- `source: env`, `name: ENVIRONMENT_VARIABLE` resolves a server-side environment variable.
- `source: saved`, `name: openai` uses the matching active `ngn login` API-key
  selection at its default endpoint. Custom endpoints and MCP environment bindings
  must use environment references.

For a saved ChatGPT subscription login, set `auth: chatgpt` on an OpenAI provider,
leave `base_url` unset and `api: auto`, and omit `secret`. It uses the existing
Codex transport and its supported model settings. Credentials remain outside YAML;
the request inspector captures the actual Codex request with authentication redacted.

MCP uses the existing stdio transport:

```yaml
mcp_servers:
  docs:
    transport: stdio
    command: documentation-mcp
    args: []
    env: {}
    secrets:
      DOCS_TOKEN: docs_key
```

Declare `docs_key` in `secrets`. To expose tools to an agent:

```yaml
mcp:
  - server: docs
    tools:
      - ref: search_docs
```

**Start & discover tools** explicitly launches the configured subprocess, shows
its current descriptions and schemas, then closes it. Starting a live agent with
MCP requests approval to launch its subprocess. MCP calls use the existing custom
tool approval mechanism. Each agent invocation owns its MCP connections and closes
them on completion or cancellation. No MCP process starts just by opening YAML.

## Delegation

Only connected target IDs may be passed to `delegate(prompt, agent)`. It returns
immediately; results are delivered through the existing parent notification and
synthesis lifecycle. Children use their own configured provider, instructions,
and tools. They do not inherit the parent's conversation or system instructions.
Existing root-owned concurrency/execution quotas and configured depth limits apply.
There is no automatic pipeline or durable task replay.

Each invocation can also store visual `source_port` / `target_port` (`top`,
`right`, `bottom`, or `left`) and `source_offset` / `target_offset` (0–1 along
the side). These attachment positions round-trip through YAML; they do not alter
the delegation tool contract. Reconnecting to another agent changes the actual
allowed delegation target.

## Inspect execution

The inspector records:

- Agent starts/finishes and delegation events.
- Model context after context preparation and before transport conversion.
- Tool names, model-facing descriptions and parameter schemas.
- HTTP requests, serialized outbound body chunks, response metadata and stream events.
- Tool arguments, results, timings, model events, and errors.

Each record has an ordered sequence and timestamp. Model context has a model-call
ID; transport requests have attempt IDs, allowing retries to be distinguished.
Child records include the agent, task, activation, and conversation identity.
Authentication headers and known resolved credentials are redacted. Individual
payloads above 1 MiB and runs above 16 MiB or 10,000 records are explicitly marked
as truncated. Request body records contain application bytes decoded as UTF-8,
not TCP/TLS packet captures. Gateway error bodies intentionally remain unavailable.

Traces are persisted in a workspace-scoped `designer.db` next to normal harness
storage. **Execution history** reopens traces after refresh; **Delete trace** removes
that run's inspection data. Conversation messages remain in separate designer
session storage. Retention is manual in this version.

**Continue this run's conversation** uses the pinned agent and resolved YAML
snapshot, even if the editor has changed. Otherwise Send starts a new conversation
using the selected agent and current design. Retained child task handles do not
survive between designer turns; persisted child history is inspection evidence.

Designer runs share the web host's execution slot. Live inspection uses incremental
HTTP reads of the durable trace, while host busy status uses its existing event bus.
Closing the designer view does not cancel its run; use **Cancel run** explicitly.

## Terminal and headless execution

```bash
pip install 'nagents[designer,tui]'
ngn --design .ngn/designs/research-team.yaml
ngn run --design .ngn/designs/research-team.yaml --design-agent researcher --json "Summarize README.md"
```

The terminal reuses the same definition-driven harness and approvals. Designer
trace persistence and the graphical inspector are provided by the web host.
Python extension tools, skill configuration, custom compaction strategies,
breakpoints, remote MCP transports, and external secret-manager plugins are not
editable through this first schema version.
