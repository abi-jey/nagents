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
        - get_model_list
        - verify_model
        - close

## Explicit Model Discovery

`async def Provider.get_model_list(self) -> list[str]` is available in the current
source checkout, not the published `v0.5.0` release. It fetches IDs on each call;
there is no catalog cache, implicit generation, or change to the selected model.

```python
import asyncio
import os

from nagents import ModelListError, Provider, ProviderType


async def main() -> None:
    async with Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=os.environ["OPENAI_API_KEY"],  # Your own OpenAI Platform API key.
        model="gpt-4.1",
    ) as provider:
        try:
            model_ids = await provider.get_model_list()
        except (ModelListError, NotImplementedError):
            print("Catalog unavailable; keep or enter your model ID manually.")
        else:
            print(model_ids)
        # provider.model remains "gpt-4.1" in either case.


asyncio.run(main())
```

For `OPENAI_COMPATIBLE`, `OPENROUTER`, and `LITELLM`, discovery uses
`GET {base_url}/models` with the provider's API key as Bearer authentication.
OpenRouter retains its existing identification header. Custom API prefixes are
preserved; no `/v1` is inserted and no pagination query is invented. LiteLLM
requires an explicit gateway prefix. Only the conventional `{"data": [{"id":
"model-id"}]}` envelope is accepted. A valid `data: []` returns `[]`; malformed
envelopes, error envelopes, and invalid IDs raise `ModelListError` instead.
IDs preserve spelling and response order, with duplicates removed. Blank IDs and
control characters are rejected; provider metadata is not returned.

Requests use a bounded response body (32 MiB), a timeout of at most 30 seconds
(or a shorter positive provider timeout), no redirects, no environment proxies,
no cookies, and no raw HTTP logging. `ModelListError` messages do not include
upstream response bodies, headers, URLs, or credentials. Unsupported discovery
raises `NotImplementedError`; this currently includes native Gemini, Anthropic,
and Azure provider types. This is a library implementation limit, not a claim
that those services lack catalog APIs. `PlaceholderProvider` and offline Harness
demo mode reject discovery without network requests.

`get_model_list()` is independent of `is_model_verified`. Existing
`verify_model(force=False)` retains its cached boolean behavior and `models/`
prefix matching; `force=True` refreshes verification. Native Gemini verification
and the local Anthropic/Azure verification behavior are unchanged. A catalog ID
is not a guarantee of tool/media support, capabilities, or account entitlement.

## Codex Authentication

`CodexProvider` is a separate authenticated transport. For the CLI's login
workflow and limitations, see [ngn](../guide/ngn.md) and
[release availability](../guide/ngn-installation.md#release-availability).

Use your own saved ngn login, never an OAuth token as an OpenAI Platform API key:

```bash
ngn login --device-auth
ngn login --status
```

```python
from nagents import CodexProvider, ModelListError
from nagents.harness.auth import OpenAIAuth


async def codex_models() -> None:
    auth = OpenAIAuth()
    try:
        async with CodexProvider(credentials=auth.credentials) as provider:
            try:
                print(await provider.get_model_list())
            except (NotImplementedError, ModelListError):
                print("Keep or enter your Codex model ID manually.")
    finally:
        await auth.close()
```

Codex catalog discovery is currently explicitly unsupported in this checkout
pending a verified upstream catalog contract. Its override raises
`NotImplementedError` without requesting credentials or making HTTP requests; it
never inherits the API-key `/models` route. Codex generation and login are
unchanged. `CodexProvider.verify_model()` remains local and does not prove account
entitlement or request a catalog. `OpenAIAuth.credentials` remains the per-request
credential/refresh callback for the existing OAuth transport, not a copied token.

::: nagents.CodexCredentials

::: nagents.CodexProvider
    options:
      members:
        - __init__
        - generate
        - get_model_list
        - close
