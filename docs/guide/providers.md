# Providers

nagents supports multiple LLM providers with a unified interface, making it easy to switch between providers without changing your application logic.

## Provider Types

| Provider | Type | Description |
|----------|------|-------------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | OpenAI API and compatible services |
| Anthropic | `ProviderType.ANTHROPIC` | Anthropic Claude API |
| Google | `ProviderType.GEMINI_NATIVE` | Google Gemini API |

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
from nagents import Agent, Provider, ProviderType, SessionManager


def get_provider(provider_name: str) -> Provider:
    """Create a provider based on name."""
    match provider_name:
        case "openai":
            return Provider(
                provider_type=ProviderType.OPENAI_COMPATIBLE,
                api_key=os.getenv("OPENAI_API_KEY"),
                model="gpt-4o-mini",
            )
        case "anthropic":
            return Provider(
                provider_type=ProviderType.ANTHROPIC,
                api_key=os.getenv("ANTHROPIC_API_KEY"),
                model="claude-3-5-sonnet-20241022",
            )
        case "gemini":
            return Provider(
                provider_type=ProviderType.GEMINI_NATIVE,
                api_key=os.getenv("GOOGLE_API_KEY"),
                model="gemini-2.0-flash",
            )
        case _:
            raise ValueError(f"Unknown provider: {provider_name}")


async def main():
    # Select provider from environment or config
    provider_name = os.getenv("LLM_PROVIDER", "openai")
    provider = get_provider(provider_name)

    session_manager = SessionManager(Path("sessions.db"))

    # Same agent code works with any provider!
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
    )

    async for event in agent.run("Hello!"):
        if hasattr(event, "chunk"):
            print(event.chunk, end="")

    await agent.close()
```

!!! success "Provider Agnostic"
    Your application logic remains the same regardless of which provider you use. This makes it easy to:

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
