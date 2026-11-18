# Providers

nagents supports multiple LLM providers with a unified interface, making it easy to switch between providers without changing your application logic.

This page describes the **Python library**; its `ngn` settings/login sections
refer to the existing **Python client**, not the [native Rust configuration](native-ngn.md#configure-the-bundled-agent-runtime).
Model identifiers in older snippets are illustrative only, not an exhaustive or
current upstream catalog. Choose a model/deployment your endpoint and account
support; use explicit discovery where implemented.

## Provider Types

| Provider | Type | Description |
|----------|------|-------------|
| OpenAI | `ProviderType.OPENAI` | Native OpenAI API and ChatGPT subscription authentication |
| OpenAI-compatible | `ProviderType.OPENAI_COMPATIBLE` | Third-party endpoints using OpenAI-style protocols |
| Anthropic | `ProviderType.ANTHROPIC` | Anthropic Claude API |
| Google | `ProviderType.GEMINI_NATIVE` | Google Gemini API |
| OpenRouter | `ProviderType.OPENROUTER` | OpenAI-compatible API with OpenRouter identification |
| LiteLLM | `ProviderType.LITELLM` | API-key gateway with an explicit API prefix |
| Azure v1 | `ProviderType.AZURE_OPENAI_COMPATIBLE_V1` | Explicit Azure API prefix; API key or caller-owned bearer credential |
| Versioned Azure | `ProviderType.AZURE_OPENAI_COMPATIBLE` | Azure deployment name, API prefix and explicit API version |
| Azure AI Foundry | `FoundryProvider` | API key or caller-owned Azure credential; see [authentication](foundry-authentication.md) |

Provider identity is separate from authentication and the wire protocol.
`OpenAIProvider` (also importable from `nagents.provider.openai`) identifies native
API-key and ChatGPT subscription connections as `ProviderType.OPENAI`. Codex is
an internal subscription transport and optional local configuration source, not a
second provider. Third-party connections use `ProviderType.OPENAI_COMPATIBLE`
with their own `base_url`. For compatibility, an existing `OpenAIProvider` with
an explicit custom endpoint or discovered custom-provider configuration retains
`OPENAI_COMPATIBLE` identity. Existing imports from `nagents.provider.codex.openai`
continue to work. Older generic `OPENAI_COMPATIBLE` calls targeting OpenAI still
work; new native integrations should use `OPENAI` or `OpenAIProvider`.

## Sign in from ngn

Use `/provider` in the TUI or **Settings → Global/Workspace → Provider connections**
in `ngn serve` to manage named connections shared across clients. Global ones
are saved to `$XDG_CONFIG_HOME/ngn/providers.json`; workspace ones are saved to
workspace-specific JSON under the same config root. Both use environment-variable **references**,
not key values. OpenAI can select ngn ChatGPT login, local Codex discovery, or
an API-key variable. Azure AI Foundry supports API-key variables and Microsoft
Entra ID through the optional `azure-identity` package. Voice settings for a
named connection use the same endpoint and credential reference. See
[named provider configuration](ngn-configuration.md#shared-named-provider-connections).

Chat model selection is independent: use `/model` in the TUI, `--model` for a
single CLI invocation, or the global/workspace chat-model selector in web
Settings. Connections display their credential source and effective endpoint;
their JSON entries do not contain a chat model. Voice preferences are stored
separately from provider connections.

`ngn login chatgpt` stores the device credential used by a named OpenAI
connection with `"auth": "chatgpt"`. Login alone does not activate a provider;
select a named connection in the registry or with `--provider-id`. API-key
connections use the environment-variable name specified in `providers.json`.
`ngn login --status` and `ngn logout` manage saved login credentials. Library
users pass credentials explicitly as shown below.

## Discover Model IDs

### Local Codex configuration

```python
from nagents import OpenAIProvider

provider = OpenAIProvider()
```

With no explicit credentials, `OpenAIProvider` discovers Codex configuration under `CODEX_HOME`
or `~/.codex`. It reads `config.toml` and file-based `auth.json` to select the
model, API contract and authentication method. Saved ChatGPT login uses the
existing Codex OAuth transport; API-key/custom-provider configuration uses the
selected Responses or legacy Chat Completions API. An unrelated `OPENAI_API_KEY`
does not replace a saved ChatGPT login.

Optional overrides are `OpenAIProvider(home=..., profile=..., model=...)`.

An explicit API key bypasses local discovery, including saved ChatGPT login:

```python
provider = OpenAIProvider(api_key="your-api-key", model="gpt-5.6-terra", api="responses")
```

Use `api="chat_completions"` to select that protocol. A custom `base_url`
requires an explicit `api_key`. `provider.uses_chatgpt_auth` distinguishes
subscription authentication from API-key authentication.

Selected `NAME.config.toml` profile files and legacy `[profiles.NAME]` tables are
supported. Custom providers can specify `base_url`, `wire_api`, `env_key`, and
`requires_openai_auth`. Project-local configuration is not used for authentication
or endpoint discovery. No credentials are copied or written. OAuth tokens are
reread before requests, picking up refreshes performed by Codex; Nagents does not
rotate the external application's refresh tokens.

This discovery path currently requires file credentials. Keyring-only/ephemeral
logins and provider command-auth/custom-header/query settings report an explicit
configuration error instead of silently selecting another route. Supplying the
existing credential callback continues to work for application-owned OAuth.

### Explicit catalog discovery

`await provider.get_model_list() -> list[str]` is available in the current API
(v0.15.0 is historical introduction evidence, not a latest-version recommendation). See the
[Provider API example](../api/provider.md#explicit-model-discovery) for a complete
async example using your own `OPENAI_API_KEY` environment variable.

Discovery is explicit and fresh. It does not select a model, alter configuration,
or generate a response. OpenAI-compatible, OpenRouter, LiteLLM, and Azure v1 connections
query their configured API prefix plus `/models`, using their own API key. A
custom service must implement the conventional `data` array with string `id`
fields. Keep a manual model-ID input: unsupported discovery raises
`NotImplementedError`, and missing credentials, upstream failures, or malformed
catalogs raise `ModelListError`. An empty list means a valid empty catalog, not a
fallback after a failure. Native Gemini, Anthropic, and versioned Azure discovery is not
implemented by this method; their existing verification behavior is unchanged.

Catalog IDs are not capability or entitlement guarantees. The generation service
can reject a listed model or accept an unlisted one. API-key catalogs and
ChatGPT/Codex OAuth catalogs are separate connections, not interchangeable ways
to use a subscription. Never pass a Codex OAuth token as `Provider.api_key`, or
send it to an OpenAI-compatible endpoint. Use `OpenAIProvider` with ChatGPT authentication and
the [existing ngn login flow](ngn.md#openai-device-login) for subscription access.
Its `get_model_list()` fetches picker-visible `slug` IDs from the fixed Codex
catalog using a fresh credential snapshot. `supported_in_api: false` does not
exclude an OAuth model. See the [ChatGPT API example and pinned contract](../api/provider.md#local-configuration-and-chatgpt-authentication)
for authentication, visibility validation, and compatibility limits. This follows
the official Codex client, not a stable public API guarantee; manual entry remains
available without changing login or billing mode.

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

    Set `model="YOUR_MODEL_ID"` to an API-key model available to your account.
    Use the [explicit catalog](#explicit-catalog-discovery) when supported;
    this guide does not maintain OpenAI's changing model catalog or pricing.

=== "Azure OpenAI"

    ```python
    provider = Provider(
        provider_type=ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
        api_key="your-azure-key",
        model="YOUR_DEPLOYMENT_NAME",
        base_url="https://your-resource.openai.azure.com/openai/v1",
    )
    ```

    !!! note "Azure Configuration"
        Use the explicit Azure route and its API prefix, not a complete
        generation endpoint. The versioned Azure route additionally requires
        `api_version`; see [Provider API](../api/provider.md).

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

Native Anthropic Messages support; individual models and capabilities depend
on your account and endpoint:

```python
from nagents import Provider, ProviderType

provider = Provider(
    provider_type=ProviderType.ANTHROPIC,
    api_key="sk-ant-...",
    model="YOUR_ANTHROPIC_MODEL_ID",
)
```

Replace the placeholder with a supported model ID. The generic library
`get_model_list()` does not implement Anthropic discovery; do not infer that
from the Python client's separate provider metadata integrations.

---

## Google Gemini

Native Gemini API support:

```python
from nagents import Provider, ProviderType

provider = Provider(
    provider_type=ProviderType.GEMINI_NATIVE,
    api_key="...",
    model="YOUR_GEMINI_MODEL_ID",
)
```

Replace the placeholder with a supported model ID. Native Gemini verification
uses its own API; generic `get_model_list()` discovery is not implemented for
this route. Context and media support are model-specific, not fixed values for
all Gemini models.

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
    | Context limit | Model/endpoint-specific | Model/endpoint-specific | Model/endpoint-specific |
    | Custom API prefix | `base_url` | `base_url` with Messages-compatible service | `base_url` with native-compatible service |

    These are implementation routes, not guarantees that every model supports
    every feature. Authentication, payload shape and capability checks remain
    provider-specific; a custom prefix is not permission to forward subscription credentials.

---

## Best Practices

!!! tip "Recommendations"

    1. **Use environment variables** for API keys
    2. **Choose an account-supported model** with the capabilities/cost profile your application needs
    3. **Implement fallback logic** for production systems
    4. **Monitor token usage** via the `usage` field in events
