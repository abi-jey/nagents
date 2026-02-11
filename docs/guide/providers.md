# Providers

nagents supports three LLM providers with a unified interface.

## Provider Types

| Provider | Type | Description |
|----------|------|-------------|
| OpenAI | `ProviderType.OPENAI_COMPATIBLE` | OpenAI API and compatible services |
| Anthropic | `ProviderType.ANTHROPIC` | Anthropic Claude API |
| Google | `ProviderType.GEMINI_NATIVE` | Google Gemini API |

## OpenAI Compatible

Works with OpenAI API and any compatible service (Azure, local models, etc.):

```python
from nagents import Provider, ProviderType

# OpenAI
provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key="sk-...",
    model="gpt-4o-mini",
)

# Azure OpenAI
provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key="your-azure-key",
    model="gpt-4",
    base_url="https://your-resource.openai.azure.com/openai/deployments/gpt-4",
)

# Local model (Ollama, vLLM, etc.)
provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key="not-needed",
    model="llama2",
    base_url="http://localhost:11434/v1",
)
```

## Anthropic Claude

Native Anthropic API support:

```python
provider = Provider(
    provider_type=ProviderType.ANTHROPIC,
    api_key="sk-ant-...",
    model="claude-3-5-sonnet-20241022",
)
```

Available models:

- `claude-3-5-sonnet-20241022` - Best balance of intelligence and speed
- `claude-3-opus-20240229` - Most capable
- `claude-3-sonnet-20240229` - Balanced
- `claude-3-haiku-20240307` - Fastest

## Google Gemini

Native Gemini API support:

```python
provider = Provider(
    provider_type=ProviderType.GEMINI_NATIVE,
    api_key="...",
    model="gemini-2.0-flash",
)
```

Available models:

- `gemini-2.0-flash` - Latest, fastest
- `gemini-1.5-pro` - Most capable
- `gemini-1.5-flash` - Fast and efficient

## Switching Providers

The unified interface makes it easy to switch providers:

```python
import os

# Select provider based on environment
provider_type = os.getenv("LLM_PROVIDER", "openai")

if provider_type == "openai":
    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-mini",
    )
elif provider_type == "anthropic":
    provider = Provider(
        provider_type=ProviderType.ANTHROPIC,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        model="claude-3-5-sonnet-20241022",
    )
elif provider_type == "gemini":
    provider = Provider(
        provider_type=ProviderType.GEMINI_NATIVE,
        api_key=os.getenv("GOOGLE_API_KEY"),
        model="gemini-2.0-flash",
    )

# Same agent code works with any provider
agent = Agent(provider=provider)
```
