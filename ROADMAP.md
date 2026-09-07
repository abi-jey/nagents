# Roadmap

This roadmap describes the current source checkout, not a guarantee of what is
included in a published package. Planned work has no assigned release versions or
delivery dates. See the [README](README.md) for installation and usage.

## Implemented Core

- Native Anthropic Messages and Gemini REST providers, alongside OpenAI-compatible
  APIs, Azure OpenAI, and OpenRouter. Streaming, non-streaming generation, and
  function calling are implemented; provider-specific capabilities still differ.
- Generic OpenAI Responses support through `api="responses"`, including text,
  user images, custom function tools, and reasoning-item replay. This is distinct
  from the ChatGPT/Codex authentication route and is not full Responses parity.
- LiteLLM gateway access with an explicit endpoint and API key, with selectable
  HTTP contracts. This does not implement a local router, fallback policy, or
  load balancer; those can be configured at the gateway.
- Python tool registration and execution, invalid-tool handling, MCP stdio clients,
  SQLite session persistence and migrations, context compaction, and batch APIs
  for supported providers. Trusted Python lifecycle hooks, context transforms,
  and replaceable compaction strategies extend the text agent loop.
- Usage events, retry/backoff handling, and HTTP/SSE debug logging. These are not
  an integrated tracing or cost-monitoring system.
- OpenAI Realtime WebSocket sessions with audio input/output, transcription events,
  and function calling. This is not Gemini Live support.

The optional server also provides HTTP/SSE chat, session management, tool/MCP
reload, and a web UI. These implementations do not imply identical capabilities
across the core library, server, and terminal client.

## ngn: Source Checkout Only

The optional `ngn` coding harness and terminal/headless clients are implemented
here but **not yet a published CLI release**. Do not assume `pip install nagents`
includes them. Use the [source installation guide](docs/guide/ngn-installation.md)
and [ngn usage guide](docs/guide/ngn.md).

- Interactive TUI and headless JSON events, local sessions, coding tools, approval
  policies, trusted project configuration, Python plugins, and an offline demo.
- Local `SKILL.md` discovery and text loading, including skill slash commands.
  Loading a skill does not automatically execute its scripts.
- Process-local asynchronous subagent trees, bounded delegation, task inspection,
  and explicit follow-ups with inherited permission ceilings. This is not a
  distributed orchestration system or A2A protocol implementation.
- API-key providers and separate ChatGPT/Codex subscription authentication, plus
  opt-in microphone dictation that produces an editable draft, not a sent prompt.

## Remaining Directions

These are planned or proposed areas for discussion, not shipped features or
commitments to complete every provider's API surface:

- **Provider coverage:** Vertex AI and service-account authentication; Gemini Live
  bidirectional audio/video; broader native built-in tools such as Gemini
  grounding/code execution, Anthropic computer use, and Responses web search,
  file search, and code interpreter. Reasoning controls and model-specific
  behavior need continued coverage rather than a blanket parity claim.
- **Skills and multi-agent interoperability:** A reusable skills library,
  composition patterns, shared-context coordination beyond local delegation,
  and A2A discovery/communication remain proposals.
- **Observability:** Langfuse/OpenTelemetry tracing, cost monitoring, and prompt
  management remain planned/research only. Existing logs, usage events, and
  extension hooks are not automatic instrumentation or an exporter integration.
- **Documentation and validation:** Expand provider-specific examples, migration
  and best-practice guides, performance guidance, and coverage of supported API
  combinations and their limitations.

## Contributing

Check the [issues](https://github.com/abi-jey/nagents/issues) for existing
discussions, or open one to agree on scope and limitations before implementing a
proposal. Include tests and documentation, and update this roadmap when an
implementation lands; release availability should be documented separately.
