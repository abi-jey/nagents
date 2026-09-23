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

## GPT-Live Voice

Set `live_config=LiveConfig(...)` on the voice provider, configure the existing
`AudioDuplex` on the agent, and consume `agent.run()` as with Realtime. The library
owns the connection, audio streaming and delegation protocol.

- `LiveConfig(delegation="responses", backend_model=..., web_search=True)` uses
  OpenAI-hosted backend inference.
- `LiveConfig(delegation="client")` uses the ordinary Nagents `Agent` passed as
  `delegation_agent=...` to the voice agent. That backend owns its model, tools,
  permissions and task history. Audio continues while it works.

The voice provider selects `model="gpt-live-1"`; backend model selection is
independent. The runnable example is
[`examples/live/`](https://github.com/abi-jey/nagents/tree/main/examples/live),
with separate hosted, client-agent, and optional commentary examples.

The client backend is an ordinary `Agent`. Its emitted events are forwarded
immediately with their original types and `extra["delegation_id"]` plus
`extra["source"] == "live_backend"`. A tagged backend `DoneEvent` does not end
the voice conversation.

`await voice.add_comment(text)`, `add_thinking(text)`, and `add_instructions(text)`
send native Live updates after startup. They are callable from application code
or other tools, and can optionally be registered as model-callable backend tools.
Delegated calls automatically carry their original task ID; application calls
outside delegated work use a null ID. The returned event ID correlates with the
server acknowledgment, rather than certifying speech playback.

`voice.live` exposes concrete commands and retained session status:

- `wait(event_id)` correlates acknowledgments and command errors.
- `mute_input()` / `unmute_input()` affect model input, independently of playback.
- `update_backend(...)` sends a sparse supported Responses configuration update.
- `submit_text(...)` / `submit_image(...)` route application input to the backend.
- `invalidate_tasks()` marks an application intent change so obsolete results
  are not delivered; business tools must still enforce their own revisions.
- `close()` requests graceful finalization; `status` retains duration, terminal
  reason and whether final usage was confirmed.

In hosted mode, register functions on the voice agent. The native runtime collects
complete hosted function batches and uses its tool executor, submitting every
result before continuing. A plain async service can instead be configured with
`LiveConfig(delegation="client", client_handler=...)`.

`LiveAPI` implements WebRTC setup, SIP controls, recording download and stored
forks. `agent.live_configuration(media=True)` supplies the session configuration
and registered hosted tool schemas without a WebSocket audio format. Attach an
Agent to the returned ID with `LiveConfig(attach_to=...)`; it does not start the
session twice. Observers use `handle_delegations=False` to avoid duplicate tool
execution. Live lifecycle/control traffic is `LiveEvent`, separate from Realtime.

`nagents.live.audio` provides paced inputs and application-owned playback gates.
Verified-clip completion requires an output adapter with an actual `drain()`
implementation. Muting a browser or SIP player's output remains the responsibility
of the media path that owns playback.

::: nagents.LiveConfig

::: nagents.live.LiveAPI

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

## OpenAI Provider

`OpenAIProvider` supports both API-key requests and ChatGPT subscription
authentication. An explicit `api_key` bypasses local discovery:

```python
from nagents import OpenAIProvider

provider = OpenAIProvider(api_key="your-api-key", model="gpt-5.6-terra", api="responses")
```

API-key requests default to Responses; select `api="chat_completions"` for
Chat Completions. A custom `base_url` requires an explicit key.
`uses_chatgpt_auth` reports the selected authentication mode, independently of
the provider class. `retry_config` applies to API-key requests; the subscription
transport does not automatically retry or replay requests.

### Local configuration and ChatGPT authentication

`OpenAIProvider()` discovers local Codex configuration and file credentials from
`CODEX_HOME` or `~/.codex`, including the selected model, profile and wire API.
Pass `home=`, `profile=`, or `model=` only to override discovery. API-key settings
use the configured compatible API; a saved ChatGPT login uses the dedicated Codex
OAuth transport. See [local discovery](../guide/providers.md#local-codex-configuration).

`OpenAIProvider(model="gpt-live-1", live_config=LiveConfig(...))` uses the same
discovery for voice. As in the Codex client, voice requires an API key and may
use its `OPENAI_API_KEY` environment fallback even when normal text inference uses
saved ChatGPT authentication. OAuth tokens are never sent as Live API keys.

For the CLI's own login
workflow and limitations, see [ngn](../guide/ngn.md) and
[release availability](../guide/ngn-installation.md#release-availability).

An explicit credential callback can instead use your saved ngn login:

```bash
ngn login --device-auth
ngn login --status
```

```python
import asyncio

from nagents import OpenAIProvider, ModelListError
from nagents.harness.auth import OpenAIAuth


async def codex_models() -> None:
    auth = OpenAIAuth()
    try:
        async with OpenAIProvider(credentials=auth.credentials) as provider:
            try:
                print(await provider.get_model_list())
            except (NotImplementedError, ModelListError):
                print("Keep or enter your Codex model ID manually.")
    finally:
        await auth.close()


asyncio.run(codex_models())
```

For ChatGPT/OAuth authentication, Codex discovery uses the fixed
`https://chatgpt.com/backend-api/codex/models?client_version=0.153.4` route.
The query and `version` header both use `0.153.4` as a **catalog protocol
compatibility version**, pinned to the official Codex `rust-v0.153.4` client, not
the ngn package version. Requests still identify
the application honestly as `originator: ngn` and `User-Agent: ngn/<package-version>`.

Each fetch obtains one current `OpenAIAuth.credentials` snapshot and uses its
access token, optional account ID, and optional residency together. It never
copies the OAuth token into `Provider.api_key`, derives a catalog route from a
custom URL, or falls back to the OpenAI API-key service. Changing an OAuth
provider's `base_url` rejects discovery before requesting credentials. Login,
refresh, and generation behavior are unchanged; no additional account-routing
features are inferred from geolocation or other metadata.

The response must contain a `models` array of objects with valid `slug` and
`visibility` fields. Only `visibility: "list"` contributes an ID; `"hide"` and
`"none"` are excluded. Missing, null, or unknown visibility is an error, matching
the pinned struct's required enum rather than guessing a default. Models with
`supported_in_api: false` **remain eligible for OAuth discovery**. IDs retain
upstream order (no priority sorting), with duplicates removed; descriptions,
instructions, account metadata, and other extra fields are not returned.

The catalog uses the same bounded, redirect-free, cookie-free, non-logging
transport limits described above. There is no catalog cache or pagination.
For ChatGPT authentication, `OpenAIProvider.verify_model()` remains local and does not prove account
entitlement or request a catalog. This is an **official Codex client contract,
not a stable public OpenAI REST API guarantee**. `ModelListError` and manual model
entry remain important if that contract or account access changes.

Pinned upstream sources: [catalog route and query](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/codex-api/src/endpoint/models.rs#L31-L78),
[required model fields](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/protocol/src/openai_models.rs#L390-L402),
and [picker visibility](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/protocol/src/openai_models.rs#L880-L883).

::: nagents.CodexCredentials

::: nagents.OpenAIProvider
    options:
      members:
        - __init__
        - generate
        - get_model_list
        - close
