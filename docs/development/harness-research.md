# Harness extensibility research

Research date: September 6, 2026. This is a source/documentation comparison, not
a performance benchmark or a claim that every upstream feature was runtime-tested.
Upstream default branches and experimental contracts can change.

This page records research and design direction, not a feature-parity promise.
For implemented behavior and current limitations, use the
[ngn guide](../guide/ngn.md); the terminal harness remains a source-only release.

## Findings

| Project | Context and compaction | Extensions | Lesson for nagents |
| --- | --- | --- | --- |
| OpenCode | Per-request message/system transforms; compaction hook can append context or replace the summary prompt | TypeScript plugins, tool/permission hooks, custom tools, agent profiles, on-demand skills, MCP, commands | Separate observer events from hooks that can change execution; expose more than a compaction prompt |
| pi | Non-destructive context transforms; custom compaction checkpoint with retained-history boundary | TypeScript SDK/extensions, tool replacement, custom tools and terminal components, Python-accessible JSONL RPC | Keep agent core, coding harness, and terminal independent; native Python should not require a JS bridge |
| OpenClaw | Replaceable context engine owns ingestion, assembly, and optionally compaction | TypeScript plugins, typed gates/modifiers/observers, skills, per-agent workspaces, local or gateway TUI | Make context ownership explicit; permission checks and run lifecycle need defined semantics |
| Grok Build | Automatic compaction and preservation instructions; pre/post compaction hooks are passive | Shell/HTTP hooks, MCP, skills/plugins, subagent definitions, headless streaming | Hooks are not necessarily algorithm replacement; document authority and failure behavior |
| Community grok-cli | Token reserves, recent-history retention, configurable summary instructions | TypeScript/Bun application with lifecycle hooks, MCP, skills and subagents | Internal exported functions do not constitute a stable compaction-plugin API |
| Grok Bot | Persistent cloud-computer agents; no custom context engine verified | Skills, routines, connectors/MCP, approvals | Persistent agents sharing a computer are not separate security boundaries |
| LangChain middleware | Python before-model and wrapped model/tool calls, request overrides | Typed middleware on LangGraph | Borrow useful function-level contracts without importing graph orchestration |

"Grokbot" is ambiguous. Grok Bot, official Grok Build, and the community grok-cli
are different projects. Clawdbot is an earlier name of OpenClaw, not an additional
current framework. The comparison does not assume which name was intended.

## Important distinctions

- Skills are instructions and resources loaded on demand, not executable hooks
  or a permission system.
- Changing a summary prompt is not the same as replacing a compaction algorithm.
- A model context is a projection of the transcript. Request-time filtering must
  not silently destroy the stored conversation.
- Permission gates must fail closed and approve the arguments that actually
  execute, after any plugin transformation.
- Trusted Python plugins have the privileges of the host process. Path checks on
  built-in tools do not sandbox a plugin, a shell, or an MCP server.
- Cancelling a UI worker is not sufficient to terminate an underlying thread or
  subprocess. Stopping execution and repairing conversation state are separate.
- A crash after a side effect but before its result is stored cannot be repaired
  by blindly retrying the tool. Record an unknown outcome and inspect it instead.

## Direction for ngn

The library exposes ordered Python hooks for incoming messages, model requests,
tool calls/results, event observation, and cleanup. A separate compaction strategy
can select its own trigger and replace the active message context, not just the
summary prompt. Old transcript rows remain available in storage.

The coding harness supplies workspace discovery, instructions/skills, guarded
file tools, managed shell execution, approval requests, and session selection.
The TUI and headless CLI use that same harness. Neither contains the LLM loop.

Executable project configuration is opt-in. Even a project-controlled API endpoint
could expose a user's provider credential, so trust applies to more than `.py`
files. Configuration and diagnostics must make this boundary visible.

The initial implementation deliberately does not attempt feature parity with
these products: no marketplace, remote daemon, agent swarm, worktree orchestrator,
LSP client, or arbitrary terminal-component plugin API is required to validate
Python-extensible harness behavior.

## Primary sources

The Grok Bot design articles distinguish state-driven avatars from orchestration:
idle, thinking, working, waiting, blocked, and done are visual lifecycle states.
Actual collaboration is asynchronous messaging between agents with separate
role context. Some notch/cursor-companion animations are explicitly exploratory
prototypes, not shipped interaction modes. For ngn, a future team view should
render real task events rather than simulate peer collaboration the backend does
not support. Color themes and collaboration views are separate concerns.

- [Grok Bot avatar/presence design](https://x.ai/news/designing-grok-bot)
- [Grok Bot design prototypes](https://x.ai/bot/guides/designing-grok-bot-with-grok-bot)
- [Grok Bot async collaboration](https://docs.x.ai/grok-bot/chat-and-collaboration)
- [OpenCode plugin documentation](https://opencode.ai/docs/plugins/)
- [OpenCode actual plugin types](https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts)
- [OpenCode agents](https://opencode.ai/docs/agents/)
- [OpenCode skills](https://opencode.ai/docs/skills/)
- [OpenCode custom tools](https://opencode.ai/docs/custom-tools/)
- [OpenCode rules](https://opencode.ai/docs/rules/)
- [OpenCode TUI](https://opencode.ai/docs/tui/)
- [pi extension contracts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [pi agent core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)
- [pi RPC and extension UI boundary](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
- [OpenClaw context engine](https://docs.openclaw.ai/concepts/context-engine)
- [OpenClaw plugin hooks](https://docs.openclaw.ai/plugins/hooks)
- [OpenClaw plugin construction](https://docs.openclaw.ai/plugins/building-plugins)
- [OpenClaw policy, sandbox, elevation](https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated)
- [OpenClaw agent loop](https://docs.openclaw.ai/concepts/agent-loop)
- [OpenClaw TUI](https://docs.openclaw.ai/web/tui)
- [OpenClaw history and former names](https://docs.openclaw.ai/start/lore)
- [Official Grok Build](https://docs.x.ai/build/overview)
- [Grok Build hooks](https://docs.x.ai/build/features/hooks)
- [Grok Build sessions](https://docs.x.ai/build/features/sessions)
- [Grok Build permissions](https://docs.x.ai/build/features/permissions)
- [Community grok-cli](https://github.com/superagent-ai/grok-cli)
- [Community grok-cli compaction source](https://github.com/superagent-ai/grok-cli/blob/main/src/agent/compaction.ts)
- [Grok Bot overview](https://docs.x.ai/grok-bot/overview)
- [Grok Bot skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations)
- [Grok Bot shared-computer security](https://docs.x.ai/grok-bot/approvals-security-and-privacy)
- [LangChain custom Python middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)

Chrome was started for browser research. Google returned an automated-traffic
challenge, and the supplied SearXNG hostname did not resolve in this environment.
Official documentation and upstream source were reachable directly and used for
the findings above.
