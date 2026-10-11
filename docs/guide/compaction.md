# Context compaction

Compaction replaces a session's active message history with a shorter context.
The built-in [`ModelCompaction`][nagents.ModelCompaction] asks a provider to
summarize the history. It is enabled by default; pass
`compaction_strategy=None` to disable it.

```python
from nagents import Agent, ModelCompaction

# Trigger at 80,000 observed input tokens, or at 100 messages.
agent = Agent(provider, session_manager, compaction_strategy=ModelCompaction(token_threshold=80_000))
agent_by_messages = Agent(provider, session_manager, compaction_strategy=ModelCompaction(message_threshold=100))
```

`token_threshold` and `message_threshold` are optional positive integers and
cannot be set together. With no threshold, `ModelCompaction` triggers at 70%
of the provider model's estimated context limit. It uses observed input tokens
when available and falls back to an estimate. The prompt defaults to a concise
continuity summary; supply `prompt` to customize it. Supply `provider` to use a
dedicated provider for summaries. Otherwise the agent's provider is used.

```python
strategy = ModelCompaction(
    message_threshold=80,
    prompt="Summarize decisions, constraints, and open questions.",
    provider=summary_provider,
)
agent = Agent(provider, session_manager, compaction_strategy=strategy)
```

Implement [`CompactionStrategy`][nagents.CompactionStrategy] to use a custom
algorithm. `should_compact(request)` decides whether to run; forced requests
skip that check. `compact(request)` returns a nonempty replacement message list
and an optional summary. The request contains detached active history,
`estimated_tokens`, `actual_input_tokens` (zero when unavailable), the session
and provider. The replacement is validated and committed atomically, so a
strategy error, cancellation, empty result, or invalid tool exchange leaves
the persisted history intact. Compaction does not include `Agent.system_prompt`
in the history it replaces.

```python
from nagents import CompactionRequest, CompactionResult


class KeepRecent:
    async def should_compact(self, request: CompactionRequest) -> bool:
        return len(request.messages) > 40

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        start = next(
            (
                i
                for i in range(max(0, len(request.messages) - 20), len(request.messages))
                if request.messages[i].role == "user"
            ),
            0,
        )
        return CompactionResult(messages=request.messages[start:], summary="Kept recent complete turns")
```

Set `compaction_strategy=None` to disable compaction. To compact only on demand,
use a strategy whose `should_compact` returns false; a manual call bypasses that
check:

```python
class ManualCompaction(ModelCompaction):
    async def should_compact(self, request: CompactionRequest) -> bool:
        return False


agent = Agent(provider, session_manager, compaction_strategy=ManualCompaction())
result = await agent.compact("session-id")
```

Call `agent.trigger_compaction()` during a run to request compaction before the
next model call. The configured strategy still performs the replacement. A
`CompactionDoneEvent` reports the old and new message counts and summary.

See [ModelCompaction in the API reference](../api/agent.md#nagents.ModelCompaction)
for the complete built-in strategy signature.

::: nagents.Agent.compact
    options:
      show_source: true

::: nagents.Agent.trigger_compaction
    options:
      show_source: true
