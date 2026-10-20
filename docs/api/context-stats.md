# Context Statistics API

Context statistics distinguish a provider-backed **context budget** from a
heuristic breakdown of the **next input**. The library provider owns the
calculation; `Agent.context_stats(session_id)` assembles the session and delegates
to it. ngn's context indicator, agent status pane and token-based compaction use
the same budget.

## Context budget and reasoning

`context_tokens` uses the last matching provider measurement: input plus
generated output, including reasoning exactly once. Estimated user/tool messages
added afterward and an unsaved streaming reply become `pending_context_tokens`.
Without a matching measurement it falls back to the estimated next input.

Reasoning already included in a provider's output is not added again. Separately
reported reasoning is added once. An unknown inclusion relationship does not
justify double counting; the source reflects incomplete information.
`observed_reasoning_tokens` is `None` when absent and preserves a reported zero.

`context_tokens_source` is `provider`, `mixed` or `estimated`.
`remaining_tokens` subtracts `context_tokens` from the model window, and can be
negative. These numbers describe a response budget, not cumulative session
billing or a promise that every earlier hidden reasoning item will be retained
in the next request. See [provider context accounting](provider.md#context-accounting).

`Provider.context_stats(...)` is synchronous and makes no HTTP request.
`Agent.context_stats(...)` may resolve bounded model metadata, but never asks a
model to generate a response. It accounts for current instructions, tool schemas,
skills, history and streamed text. Explicit `$skill` activation is request-local
and is not part of an idle session's saved history.

## Estimated next-input components

`total_tokens` and `components` estimate the assembled next input. Components
appear in stable order and sum exactly to `total_tokens`; they do not partition
the provider-measured `context_tokens` total.

The heuristic uses roughly four characters per text token and a size-based
approximation for base64 media. Provider framing and attachment tokenization can
differ. These component estimates are not tokenizer counts or billed usage.

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

Loaded skill bodies returned by `skill(name)` appear under `skills`; their media
remains under `media`. `estimate_context_stats` is the pure function for callers
that already have assembled inputs; a library Agent supplies matching provider
usage, history and streaming state automatically.

## Python

```python title="context_stats.py"
# After running an ordinary Agent:
stats = await agent.context_stats("session-123")
print(f"Context: {stats.context_tokens} ({stats.context_tokens_source})")
print(f"Estimated next input: {stats.total_tokens}")
for component in stats.components:
    print(f"{component.label}: {component.tokens}")
if stats.context_window is not None:
    print(f"Remaining: {stats.remaining_tokens} of {stats.context_window}")
if stats.observed_reasoning_tokens is not None:
    print(f"Last reported reasoning: {stats.observed_reasoning_tokens}")
```

The provider resolves the model window from explicit configuration, matching
provider metadata or a heuristic fallback. `context_window_source` identifies
which one was used. The pure estimator can leave the window as `None` when no
limit is supplied. A model, connection, instructions, tool or saved-context
change invalidates a previous measurement rather than applying it to unrelated
input.

::: nagents.context_stats.ContextStats

::: nagents.context_stats.ContextComponent

::: nagents.estimate_context_stats

::: nagents.estimate_tool_tokens
