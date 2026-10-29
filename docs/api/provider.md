# Provider API

Use `Provider` with a `ProviderType` value and explicit credentials/model.
`OpenAIProvider` adds OpenAI authentication/discovery; `FoundryProvider` accepts
a caller-owned sync or async Azure credential or an API key. See [Providers](../guide/providers.md),
[Foundry authentication](../guide/foundry-authentication.md), and
[Multi-Provider Setup](../examples/multi-provider.md).

`base_url` is an API prefix, not a complete generation endpoint. A shared
interface does not imply equal media, reasoning, retry, or batch capabilities
across providers and HTTP contracts. This reference follows the current source.

The library constructors `Provider`, `OpenAIProvider`, and `FoundryProvider`
accept `timeout=120.0` for the total duration of each model HTTP request, including
its streaming response. Named ngn connections expose this existing parameter as
[`request_timeout`](../guide/ngn-configuration.md#shared-named-provider-connections),
with the same default for API-key and ChatGPT/Codex authentication. Catalog
requests retain their 30-second cap, and shell, whole-run, authentication, and
Live session budgets remain separate. Retry policy is unchanged.

All three constructors also accept `session_id_header=""`. Set it to a custom
HTTP header name, such as `x-opencode-session`, to send the current conversation
ID on text generation requests. `Agent.run(..., session_id=...)` supplies the
same ID across model rounds, tool continuations, and resumed turns. An Agent
that creates a new session uses that new session's durable ID. Unset headers
and direct provider calls without an explicit ID send no correlation header.

Direct callers can use a scoped context or the existing setter:

```python
provider = Provider(
    ProviderType.LITELLM,
    api_key="gateway-key",
    model="gateway/model",
    base_url="https://gateway.example/v1",
    session_id_header="x-opencode-session",
)
with provider.session_context("conversation-123"):
    async for event in provider.generate(messages):
        print(event)
# Or: provider.set_session_id("conversation-123"); clear with None or "".
```

The ID is local to the calling asynchronous context, so tasks sharing a provider
do not overwrite each other's IDs. `session_context` restores the previous ID
on exit; child tasks inherit their parent's context until they set their own ID.
Header names must be ASCII HTTP tokens up to 128 characters; authentication,
routing, and protocol headers are reserved, case insensitively. Session values
must be 1–1024 visible ASCII characters without spaces; use an opaque ASCII
identifier when an application's native IDs contain Unicode. Invalid values
raise before an HTTP request and never appear in validation error text. Model
catalogs and credential requests do not carry this header. It does not configure
batch uploads or Live/Realtime connection headers.

::: nagents.ProviderType

::: nagents.Provider
    options:
      members:
        - __init__
        - generate
        - get_model_list
        - verify_model
        - context_stats
        - resolve_context_window
        - set_session_id
        - session_context
        - close

## Context accounting

`Provider.context_stats(...)` is the library's context-accounting entry point.
It uses the selected connection's cached model limit and normalized usage.
`Agent.context_stats(session_id)` resolves optional model metadata, assembles the
session, and delegates to this method. ngn's main chat and subagent status views
consume that same result, including an unsaved streaming response.

The result distinguishes two quantities:

- `context_tokens` is the last measured response's input and generated output,
  including reasoning once, plus estimated messages added afterward.
  `remaining_tokens` is the model window minus this quantity.
- `total_tokens` and `components` are a heuristic breakdown of the next input.
  They are not provider tokenizer counts or accumulated session billing.

For example, 400 input tokens and 120 output tokens that include 80 reasoning
tokens consume 520 tokens. If a provider instead reports 40 visible output
tokens and 80 separate reasoning tokens, the same context consumption is 520.
The normalized `reasoning_in_output` contract controls this calculation; an
unknown inclusion relationship is never permission to add reasoning twice.

`observed_reasoning_tokens` preserves a provider-reported zero and uses `None`
when no reasoning breakdown was supplied. `context_tokens_source` is `provider`
for complete reported counts, `mixed` for partial counts or estimated additions,
and `estimated` when no matching measurement exists. The UI marks estimates with
`~`. Measurements are invalidated after model, connection, instructions, tool,
or saved-context changes. New user messages and tool results grow the budget
without adding the same assistant response again.

```python
from nagents.events import TokenUsage

window = await provider.resolve_context_window()  # Optional bounded metadata refresh.
stats = provider.context_stats(
    context_window=window,
    observed=TokenUsage(
        prompt_tokens=400,
        completion_tokens=120,
        reasoning_tokens=80,
        reasoning_reported=True,
        reasoning_in_output="included",
    ),
)
assert stats.context_tokens == 520
assert stats.observed_reasoning_tokens == 80

# An Agent supplies matching usage, history and streaming state automatically.
stats = await agent.context_stats(session_id)
```

This is a response-budget measurement, not a promise that all earlier reasoning
will be retained in the next request. Retention depends on the provider, model
and transmitted reasoning items. See OpenAI's
[reasoning context documentation](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-across-calls).
Future reasoning that the model has not generated cannot be measured in advance.

## Foundry credentials

`FoundryProvider` reuses the Provider text/GPT-Live transports. `credential` and
`api_key` are mutually exclusive; `scope` defaults to `https://ai.azure.com/.default`.
It never creates, selects, caches, or closes an Azure credential. Applications
choose a sync or async Azure Identity credential (or a structural equivalent)
themselves. Async is preferred; synchronous token calls are offloaded to a worker
thread to keep the voice event loop responsive.
Azure Identity is a developer-testing dependency only, not a project runtime
dependency or extra.

Foundry API-key GPT-Live WebSockets use the `api-key` header, including attach/fork.
Entra WebSockets and REST requests retain Bearer authentication, as do REST API
keys. OpenAI WebSocket authentication is unchanged. Redirects and query-string
credentials are not allowed.

::: nagents.FoundryProvider
    options:
      members:
        - __init__

::: nagents.provider.AsyncTokenCredential

::: nagents.provider.TokenCredential

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
`LiveConfig(delegation="client", client_request_handler=...)`.

For correlated application work, `client_request_handler` receives a
`ClientDelegationRequest` (from `nagents.live`) contains the original `identifier`,
notice-time `transcript`, optional native `text`, and `offset_ms`. The immutable
snapshot never absorbs later speech while waiting for a backend. A synchronous
`client_request_observer` can report receipt immediately, including when all
workers are busy; keep this callback short and nonblocking.

Independent callbacks run with `client_concurrency=4` (configurable from 1 to 32),
with at most 32 queued requests. A supplied `Agent.delegation_agent` keeps one
lane because it owns one conversation. Cancellation joins all workers before
disconnecting.

The typed handler accepts voice notices only. Route application text or images
directly to your backend; `submit_text` and `submit_image` reject this handler
configuration instead of discarding the supplied content. A delegation agent
continues to accept these multimodal input commands.

A handler may return `""` when the application owns ongoing results. The ID
remains valid until the connection closes: send quiet progress with
`voice.live.append("thinking", text, identifier)` and speakable results with
`voice.live.append("commentary", text, identifier)`. Use an empty identifier for
session-wide work. Keep each public append below 500 tokens; the transport's
automatic results use conservative 480-byte UTF-8 chunks. An append receipt
does not confirm audio playback. See the
[OpenAI delegation guide](https://developers.openai.com/api/docs/guides/live-delegation).

The web ChatGPT login adapter uses its distinct native protocol: quiet progress
maps to `channel: commentary`, spoken results to `channel: speakable`, and an
empty ID uses `session.context.append`. Native instruction appends are explicitly
unsupported; they are not converted into spoken instructions. Native content is
also split below its 500-byte limit.

`LiveAPI` implements WebRTC setup, SIP controls, recording download and stored
forks. `agent.live_configuration(media=True)` supplies the session configuration
and registered hosted tool schemas without a WebSocket audio format. Attach an
Agent to the returned ID with `LiveConfig(attach_to=...)`; it does not start the
session twice. Observers use `handle_delegations=False` to avoid duplicate tool
execution. Live lifecycle/control traffic is `LiveEvent`, separate from Realtime.

`nagents.live.audio` provides paced inputs and application-owned playback gates.
`PacedAudioInput` preserves source chunks and schedules their duration using the
source's audio format. Its clock starts when the first nonempty chunk is available;
waiting for source setup does not give buffered audio a head start. The default
`silence_tail=True` continues with silence after the source ends.
Verified-clip completion requires an output adapter with an actual `drain()`
implementation. Muting a browser or SIP player's output remains the responsibility
of the media path that owns playback.

::: nagents.LiveConfig

::: nagents.live.LiveAPI

## Explicit Model Discovery

`async def Provider.get_model_list(self) -> list[str]` provides explicit model
discovery and is included in v0.15.0. It fetches IDs on each call; there is no
catalog cache, implicit generation, or change to the selected model.

```python
import asyncio
import os

from nagents import ModelListError, Provider, ProviderType


async def main() -> None:
    async with Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key=os.environ["OPENAI_API_KEY"],  # Your own OpenAI Platform API key.
        model="gpt-6-luna",
    ) as provider:
        try:
            model_ids = await provider.get_model_list()
        except (ModelListError, NotImplementedError):
            print("Catalog unavailable; keep or enter your model ID manually.")
        else:
            print(model_ids)
        # provider.model remains "gpt-6-luna" in either case.


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
that those services lack catalog APIs. Offline Harness demo mode rejects discovery without network requests.

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
discovery for the public Live WebSocket API. This path requires an API key and may
use its `OPENAI_API_KEY` environment fallback even when normal text inference uses
saved ChatGPT authentication. OAuth tokens are never sent as Live API keys.
The [web voice interface](../guide/ngn-web.md#gpt-live-voice-conversations) uses
the normal provider registry and defaults, with an optional explicit voice
connection. Its browser exchanges PCM audio only with the ngn server over a
same-origin WebSocket. The server owns the native Codex media/control path for
ChatGPT login, or the provider's Live WebSocket for supported API-key/Entra
connections. The selected provider must support Live.

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
from nagents.harness.providers.auth import OpenAIAuth


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
`https://chatgpt.com/backend-api/codex/models` route. Its `client_version` query
and `version` header use the latest stable Codex release version discovered from
OpenAI's public [`@openai/codex` package metadata](https://registry.npmjs.org/@openai/codex/latest),
not the ngn package version. This matters because upstream catalog visibility
can depend on client version. Requests still identify
the application honestly as `originator: ngn` and `User-Agent: ngn/<package-version>`.

Every explicit catalog lookup revalidates that public release metadata, using
`If-None-Match` when the registry supplies an ETag; overlapping lookups share one
check. There is no positive freshness delay that hides a newly published stable
version. Metadata requests have a separate maximum two-second deadline (or the
connection's shorter catalog deadline), a 64 KiB response limit, no redirects,
cookies, proxy/netrc settings, provider credentials, account headers, or request
tracing. Only the exact package name and a plain stable `major.minor.patch`
version are accepted. ngn does not install or execute Codex to discover models.

If metadata is unavailable, invalid, or older than the known version, ngn uses
the last valid version in this process and waits five minutes before checking
metadata again. A process with no successful metadata lookup uses the verified
`0.162.0` fallback. This is an offline compatibility fallback, not a cached model
list: the authenticated catalog itself is fetched on every request. The cache
contains only public version/ETag data, stays in memory, and writes no settings
or credential files. Logs report the selected version, metadata source, and
fallback status without upstream response data. `ngn serve` routes these bounded
diagnostics through its application logger, so they remain visible in normal
server/container logs without enabling verbose logging for other library or
MCP operations.

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
Codex's required enum rather than guessing a default. Models with
`supported_in_api: false` **remain eligible for OAuth discovery**. IDs retain
upstream order (no priority sorting), with duplicates removed; descriptions,
instructions, account metadata, and other extra fields are not returned.

The authenticated catalog uses the same bounded, redirect-free, cookie-free,
non-logging transport limits described above. There is no model-list cache or pagination.
For ChatGPT authentication, `OpenAIProvider.verify_model()` remains local and does not prove account
entitlement or request a catalog. This is an **official Codex client contract,
not a stable public OpenAI REST API guarantee**. `ModelListError` and manual model
entry remain important if that contract or account access changes.

Verified upstream sources: [catalog route and client-version query](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/codex-api/src/endpoint/models.rs),
[model fields and picker visibility](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/protocol/src/openai_models.rs),
and [Codex's public release metadata checks](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/tui/src/updates.rs).

::: nagents.CodexCredentials

::: nagents.OpenAIProvider
    options:
      members:
        - __init__
        - generate
        - get_model_list
        - close

## Reasoning effort

`GenerationConfig(reasoning_effort="low")` selects an explicit effort. The default
empty string omits the effort parameter, preserving the provider's default. This
is independent of requesting displayable summaries. Effort levels are
model-specific; there is no universal `none` or `max` setting.

| Route | Request field |
|---|---|
| OpenAI Chat Completions | `reasoning_effort` |
| OpenAI Responses and ChatGPT/Codex | `reasoning.effort` |
| Anthropic Messages | `output_config.effort` |
| Gemini 3 native | `generationConfig.thinkingConfig.thinkingLevel` |
| OpenRouter | `reasoning.effort` |

`await provider.resolve_reasoning_efforts()` returns supported choices with
bounded catalog discovery. `provider.cached_reasoning_efforts(model="...")`
reads choices for a draft model without changing the provider's active model or
making a request. Exact Codex `supported_reasoning_levels` and OpenRouter
`reasoning.supported_efforts` take precedence over conservative documented model
choices. Catalog data is bounded and isolated by endpoint and authentication
identity; unsupported or unknown portable choices fail before generation HTTP.
Custom gateways without advertised capabilities retain their defaults.

An explicit portable effort replaces only the corresponding effort control in
authored provider settings. Gemini's conflicting `thinkingBudget` and
OpenRouter's conflicting `max_tokens`/`enabled` reasoning controls are removed
from the outgoing request, without changing the supplied configuration object.
Anthropic effort does **not** turn thinking on. Gemini 2.5 uses token budgets,
so it has no portable effort selector; its explicit `thinking_config` remains
available. Deliberately authored `responses_reasoning`, `thinking_config`, and
OpenRouter `reasoning` settings remain available for custom routes.

Provider references: [OpenAI reasoning effort](https://developers.openai.com/api/docs/guides/reasoning),
[Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[Gemini thinking levels](https://ai.google.dev/gemini-api/docs/generate-content/thinking),
and [OpenRouter model reasoning capabilities](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

## Displayable reasoning summaries and usage

`HarnessConfig.reasoning_summaries` defaults to `true`. Set
`"reasoning_summaries": false` in trusted configuration, or
`NGN_REASONING_SUMMARIES=false`, to stop requesting summaries. Framework callers
opt in with `GenerationConfig(reasoning_summary="auto")`; its default is `"off"`.
This controls requesting summaries, not whether the model reasons or how many
tokens it uses. Providers may return summaries or usage without being asked.

Automatic requests apply to recognized reasoning models on native OpenAI
Responses, Gemini, and Anthropic endpoints, and the fixed ChatGPT/Codex route.
They request Responses `summary: "auto"`, Gemini `includeThoughts: true`, or
Anthropic adaptive `display: "summarized"`. Older/non-thinking models and custom
gateways receive no extra summary parameter. There is no speculative second
generation when a provider rejects a request. For an explicitly compatible
Responses gateway, use
`GenerationConfig(responses_reasoning={"effort": "low", "summary": "concise"})`.
Explicit Responses effort/summary and Gemini thinking settings are preserved.
Anthropic automatic requests only target models whose default is already
adaptive thinking (Claude 5 families and Mythos Preview); this flag does not
turn thinking on for Claude 4 models.

Provider adapters mark displayable summary text with
`ReasoningChunkEvent(displayable=True)`. Custom providers must explicitly opt in;
the default `False` preserves private framework reasoning and excludes it from
web response metadata. Display summaries remain separate from answer text,
ordinary prompt history, and voice delivery.
Responses continuation retains encrypted state with an empty summary; Anthropic
tool continuation must retain its signed thinking blocks unchanged as protocol
metadata. Those required signed blocks are not copied into assistant prose.
Opaque signatures, encrypted content, and redacted thinking are never emitted
as display text. Responses summary reconciliation retains at most 65,536
characters and 1,024 parts before downstream UI retention rules apply.

`Usage.reported` and `Usage.reasoning_reported` distinguish missing counts from
reported zero. Input, output, and total have independent `prompt_reported`,
`completion_reported`, and `total_reported` flags: a reasoning-only usage
snapshot does not imply zero input or output. Fallback totals remain unreported.
`reasoning_in_output` is `"included"` for OpenAI reasoning and
Anthropic `output_tokens_details.thinking_tokens`; Gemini
`thoughtsTokenCount` is `"separate"` from `candidatesTokenCount`. Totals use the
provider's total where supplied. Gemini's fallback includes thought tokens
once; included reasoning is never added again. These counts are distinct from
context-window estimates and the length of a displayed summary.

Provider references: [OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning),
[Gemini thought summaries and usage](https://ai.google.dev/gemini-api/docs/generate-content/thinking),
[Anthropic display and signed blocks](https://platform.claude.com/docs/en/build-with-claude/thinking),
and [Anthropic thinking-token accounting](https://platform.claude.com/docs/en/build-with-claude/extended-thinking).
