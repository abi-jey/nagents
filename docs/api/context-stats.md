# Context Statistics API

Context statistics report an **estimated** token breakdown of the request a
session would send: the system prompt, request-local instructions, tool
definitions, skill text, conversation history, and media. They are meant for
budgeting and for understanding which part of a request dominates.

!!! warning "These are estimates, not tokenizer counts"

    Values use the same heuristic as
    [context compaction](../guide/compaction.md): roughly four characters per
    token for text, plus a size-based approximation for base64 media. Cached
    tokens, provider-specific framing, and attachment tokenization are
    approximate. Do not treat them as billed usage.

`estimate_context_stats` is a pure function over already-assembled inputs. The
`Agent.context_stats(session_id)` method assembles the same system prompt, tool
schemas, skill catalog, and persisted history the next text request would use,
without calling the provider or changing session state. Explicit `$skill`
activation text is request-local and never persisted, so an idle estimate does
not include it.

## Components

Components always appear in a stable order with stable keys and labels, even
when their estimate is zero. Component estimates sum exactly to
`total_tokens`.

| Key | Label |
| --- | --- |
| `system_prompt` | System prompt |
| `instructions` | Request instructions |
| `tools` | Tool definitions |
| `skills` | Skills |
| `history_user` | Conversation history (user) |
| `history_assistant` | Conversation history (assistant) |
| `history_tool` | Conversation history (tool) |
| `history_other` | Conversation history (other) |
| `media` | Media and attachments |

Loaded skill bodies returned through the `skill(name)` tool are reported under
`skills`; their media (if any) remains under `media`.

## Python

```python title="context_stats.py"
from pathlib import Path

from nagents import Agent, Provider, ProviderType, SessionManager

agent = Agent(
    provider=Provider(ProviderType.OPENAI_COMPATIBLE, "sk-...", "gpt-4o"),
    session_manager=SessionManager(Path("sessions.db")),
    system_prompt="You are a helpful assistant.",
)

# ...after some runs...
stats = await agent.context_stats("session-123")
for component in stats.components:
    print(f"{component.label}: {component.tokens}")
print(f"Total: {stats.total_tokens}")
if stats.context_window is not None:
    print(f"Remaining: {stats.remaining_tokens} of {stats.context_window}")
if stats.observed_prompt_tokens is not None:
    print(f"Last provider-reported input: {stats.observed_prompt_tokens}")
```

The model context window comes from
[`get_model_context_limit`](agent.md); it is `None` when the window is not
known. The window is reported only for reference and never changes model
behavior.

::: nagents.context_stats.ContextStats

::: nagents.context_stats.ContextComponent

::: nagents.estimate_context_stats

::: nagents.estimate_tool_tokens
