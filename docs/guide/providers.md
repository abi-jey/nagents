# Providers

nagents supports multiple LLM providers with a unified interface, making it easy to switch between providers without changing your application logic.

## Provider Types

| Provider | Type | Description |
|----------|------|-------------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | OpenAI API and compatible services |
| Anthropic | `ProviderType.ANTHROPIC` | Anthropic Claude API |
| Google | `ProviderType.GEMINI_NATIVE` | Google Gemini API |
| OpenRouter | `ProviderType.OPENROUTER` | OpenAI-compatible API with OpenRouter identification |
| LiteLLM | `ProviderType.LITELLM` | API-key gateway with an explicit API prefix |

## Discover Model IDs

The current source checkout adds `await provider.get_model_list() -> list[str]`;
the published `v0.5.0` release does not include this method. See the
[Provider API example](../api/provider.md#explicit-model-discovery) for a complete
async example using your own `OPENAI_API_KEY` environment variable.

Discovery is explicit and fresh. It does not select a model, alter configuration,
or generate a response. OpenAI-compatible, OpenRouter, and LiteLLM connections
query their configured API prefix plus `/models`, using their own API key. A
custom service must implement the conventional `data` array with string `id`
fields. Keep a manual model-ID input: unsupported discovery raises
`NotImplementedError`, and missing credentials, upstream failures, or malformed
catalogs raise `ModelListError`. An empty list means a valid empty catalog, not a
fallback after a failure. Native Gemini, Anthropic, and Azure discovery is not
implemented by this method; their existing verification behavior is unchanged.

Catalog IDs are not capability or entitlement guarantees. The generation service
can reject a listed model or accept an unlisted one. API-key catalogs and
ChatGPT/Codex OAuth catalogs are separate connections, not interchangeable ways
to use a subscription. Never pass a Codex OAuth token as `Provider.api_key`, or
send it to an OpenAI-compatible endpoint. Use the separate `CodexProvider` and
the [existing ngn login flow](ngn.md#openai-device-login) for subscription access.

---

## OpenAI Compatible

Works with OpenAI API and any compatible service (Azure, local models, etc.):

=== "OpenAI"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="sk-...",
        model="gpt-4o-mini",
    )
    ```

    **Available models:**

    - `gpt-4o` - Most capable
    - `gpt-4o-mini` - Fast and affordable
    - `gpt-4-turbo` - Previous generation
    - `gpt-3.5-turbo` - Legacy, fastest

=== "Azure OpenAI"

    ```python
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="your-azure-key",
        model="gpt-4",
        base_url="https://your-resource.openai.azure.com/openai/deployments/gpt-4",
    )
    ```

    !!! note "Azure Configuration"
        Set `base_url` to your Azure OpenAI deployment endpoint.

=== "Local Models"

    ```python
    # Ollama
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="not-needed",  # (1)!
        model="llama2",
        base_url="http://localhost:11434/v1",
    )

    # vLLM
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="not-needed",
        model="meta-llama/Llama-2-7b-chat-hf",
        base_url="http://localhost:8000/v1",
    )
    ```

    1. Local servers typically don't require an API key, but the parameter is still required.

---

## Anthropic Claude

Native Anthropic API support with all Claude models:

```python
from nagents import Provider, ProviderType

provider = Provider(
    provider_type=ProviderType.ANTHROPIC,
    api_key="sk-ant-...",
    model="claude-3-5-sonnet-20241022",
)
```

**Available models:**

| Model | Description |
|-------|-------------|
| `claude-3-5-sonnet-20241022` | Best balance of intelligence and speed |
| `claude-3-opus-20240229` | Most capable, best for complex tasks |
| `claude-3-sonnet-20240229` | Balanced performance |
| `claude-3-haiku-20240307` | Fastest, best for simple tasks |

!!! tip "Model Selection"
    Start with `claude-3-5-sonnet` for most use cases. Use `opus` for complex reasoning and `haiku` for high-throughput, simple tasks.

---

## Google Gemini

Native Gemini API support:

```python
from nagents import Provider, ProviderType

provider = Provider(
    provider_type=ProviderType.GEMINI_NATIVE,
    api_key="...",
    model="gemini-2.0-flash",
)
```

**Available models:**

| Model | Description |
|-------|-------------|
| `gemini-2.0-flash` | Latest, fastest multimodal model |
| `gemini-1.5-pro` | Most capable, large context window |
| `gemini-1.5-flash` | Fast and efficient |

---

## Switching Providers

The unified interface makes it easy to switch providers dynamically:

```python title="multi_provider.py"
import os
from pathlib import Path
from nagents import Agent, ErrorEvent, Provider, ProviderType, SessionManager, TextChunkEvent


def get_provider(provider_name: str) -> Provider:
    """Create a provider based on name."""
    match provider_name:
        case "openai":
            return Provider(
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                api_key=os.environ["OPENAI_API_KEY"],
                model=os.environ["OPENAI_MODEL"],
            )
        case "anthropic":
            return Provider(
                provider_type=ProviderType.ANTHROPIC,
                api_key=os.environ["ANTHROPIC_API_KEY"],
                model=os.environ["ANTHROPIC_MODEL"],
            )
        case "gemini":
            return Provider(
                provider_type=ProviderType.GEMINI_NATIVE,
                api_key=os.environ["GOOGLE_API_KEY"],
                model=os.environ["GOOGLE_MODEL"],
            )
        case _:
            raise ValueError(f"Unknown provider: {provider_name}")


async def main():
    # Select provider from environment or config
    provider_name = os.getenv("LLM_PROVIDER", "openai")
    provider = get_provider(provider_name)

    session_manager = SessionManager(Path("sessions.db"))

    # The same text-only agent interface can use any of these providers.
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        streaming=True,
    )

    try:
        async for event in agent.run("Hello!"):
            if isinstance(event, TextChunkEvent):
                print(event.chunk, end="", flush=True)
            elif isinstance(event, ErrorEvent):
                print(f"\nError: {event.message}")
    finally:
        await agent.close()
```

Set the selected provider's API key and `*_MODEL` environment variables before
calling `main()`. Choose a model available to your account.

!!! success "Provider Agnostic"
    The text-generation interface is shared, but media, reasoning, batch, and
    generation-option support depend on the backend and HTTP contract. Your
    application can use this interface to:

    - Switch providers for cost optimization
    - Use different providers for different tasks
    - Fail over to backup providers
    - Test with local models, deploy with cloud providers

---

## Provider Comparison

??? info "Feature Comparison"

    | Feature | OpenAI | Anthropic | Gemini |
    |---------|--------|-----------|--------|
    | Streaming | :material-check: | :material-check: | :material-check: |
    | Tool Calling | :material-check: | :material-check: | :material-check: |
    | Vision | :material-check: | :material-check: | :material-check: |
    | Max Context | 128K | 200K | 2M |
    | Custom Base URL | :material-check: | :material-close: | :material-close: |

---

## Best Practices

!!! tip "Recommendations"

    1. **Use environment variables** for API keys
    2. **Start with smaller models** (gpt-4o-mini, claude-3-haiku, gemini-flash) for development
    3. **Implement fallback logic** for production systems
    4. **Monitor token usage** via the `usage` field in events
