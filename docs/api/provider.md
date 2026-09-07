# Provider API

Use `Provider` with a `ProviderType` value and explicit credentials/model.
There are no separate `OpenAIProvider`, `AnthropicProvider`, or `GoogleProvider`
classes. See [Providers](../guide/providers.md) and
[Multi-Provider Setup](../examples/multi-provider.md).

`base_url` is an API prefix, not a complete generation endpoint. A shared
interface does not imply equal media, reasoning, retry, or batch capabilities
across providers and HTTP contracts. This reference follows the current source.

::: nagents.ProviderType

::: nagents.Provider
    options:
      members:
        - __init__
        - generate
        - verify_model
        - close

## Codex Authentication

`CodexProvider` is a separate authenticated transport. For the source-only
CLI's login workflow and limitations, see [ngn](../guide/ngn.md).

::: nagents.CodexCredentials

::: nagents.CodexProvider
    options:
      members:
        - __init__
        - generate
        - close
