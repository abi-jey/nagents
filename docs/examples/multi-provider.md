# Multi-Provider Setup

Use the same `Provider` class with different `ProviderType` values. Routing,
comparison, and cross-provider fallback are application policies, not a built-in
fallback agent or load balancer.

## Provider Configuration

| Backend | Provider type | Credentials passed by the examples |
|---------|---------------|------------------------------------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | `OPENAI_API_KEY` |
| Anthropic | `ProviderType.ANTHROPIC` | `ANTHROPIC_API_KEY` |
| Google Gemini native API | `ProviderType.GEMINI_NATIVE` | `GOOGLE_API_KEY` |

Set `OPENAI_MODEL`, `ANTHROPIC_MODEL`, and `GOOGLE_MODEL` to model IDs available
to your accounts. These variable names are an example convention, not automatic
environment discovery by `Provider`.

!!! note "Capabilities differ"
    A common interface does not imply provider parity. Media, reasoning,
    generation options, batch support, and HTTP contracts vary. Consult the
    [Providers Guide](../guide/providers.md) before moving a workload between
    backends. Model availability and pricing must be checked with the provider.

## Parallel Provider Comparison

This example compares independent, text-only requests. Each task owns an agent
and provider; each `run()` without a session ID creates a separate conversation.

```python title="parallel_comparison.py" linenums="1"
import asyncio
import os
from contextlib import aclosing
from pathlib import Path

from nagents import Agent, DoneEvent, ErrorEvent, Provider, ProviderType, SessionManager


async def get_response(provider_type: ProviderType, key_env: str, model_env: str) -> str:
    agent = Agent(
        provider=Provider(
            provider_type=provider_type,
            api_key=os.environ[key_env],
            model=os.environ[model_env],
        ),
        session_manager=SessionManager(Path("sessions.db")),
        system_prompt="Be concise.",
    )
    answer = ""
    completed = False
    try:
        async with aclosing(agent.run("Explain quantum entanglement in simple terms.")) as events:
            async for event in events:
                if isinstance(event, ErrorEvent):
                    raise RuntimeError(event.message)
                if isinstance(event, DoneEvent):
                    answer = event.final_text
                    completed = True
        if not completed:
            raise RuntimeError("Run ended without a DoneEvent")
        return answer
    finally:
        await agent.close()


async def main() -> None:
    configurations = [
        (ProviderType.OPENAI_COMPATIBLE, "OPENAI_API_KEY", "OPENAI_MODEL"),
        (ProviderType.ANTHROPIC, "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
        (ProviderType.GEMINI_NATIVE, "GOOGLE_API_KEY", "GOOGLE_MODEL"),
    ]
    results = await asyncio.gather(
        *(get_response(*configuration) for configuration in configurations),
        return_exceptions=True,
    )
    for configuration, result in zip(configurations, results, strict=True):
        print(f"\n{configuration[0].value}:")
        if isinstance(result, BaseException):
            print(f"Failed: {result}")
        else:
            print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

The default non-streaming mode is intentional: read `DoneEvent.final_text`
rather than waiting for `TextChunkEvent`. To stream, pass `streaming=True` to
the agent and print `TextChunkEvent.chunk`, as in [Basic Agent](basic-agent.md).

## Fallback Pattern

For a one-off text request without tools, the same `get_response()` function
can be tried sequentially. This fragment replaces `main()` above and requires
both providers' environment variables:

```python
async def main() -> None:
    try:
        answer = await get_response(ProviderType.OPENAI_COMPATIBLE, "OPENAI_API_KEY", "OPENAI_MODEL")
    except (RuntimeError, ValueError) as error:
        print(f"Primary request failed: {error}")
        answer = await get_response(ProviderType.ANTHROPIC, "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")
    print(answer)
```

This is a deliberately limited policy: configuration errors such as a missing
environment variable are not retried, and a failed fallback propagates. Provider
HTTP retries are separate and configured with `RetryConfig`.

!!! warning "Do not blindly replay tools or partial streams"
    A failed request may already have emitted text, stored conversation state,
    or performed side effects. Buffering the final text only avoids displaying
    a partial answer; it does not undo work. Do not extend this example to
    tool-using agents without an explicit idempotency and recovery policy.

## Provider Routing and Sessions

For routing, choose a configured agent before starting the request, for example
`agent = agents[task_type]` where your application maintains the mapping.
Select models based on your own task evaluations and budget rather than assuming
one provider is always cheaper or better.

A `SessionManager` can be shared across agents, but conversation history is
selected by session ID, not by the agent or provider. Use separate IDs for
comparisons, serialize requests that use the same ID, and verify that any
reused history is supported by the destination provider. Do not share a live
provider instance across concurrent runs.

## Next Steps

- Extend agents with [Tool Usage](tool-usage.md)
- Read the [Providers Guide](../guide/providers.md) and [Provider API](../api/provider.md)
- Review [Configuration](../getting-started/configuration.md)
