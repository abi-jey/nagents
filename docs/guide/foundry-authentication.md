# Foundry authentication and GPT-Live

Use an existing Foundry resource and deployed models. Set `base_url` to the API
**prefix** `https://RESOURCE.openai.azure.com/openai/v1`, and `model` to the
deployment name. This integration is GPT-Live, not Azure Voice Live or the
Realtime API; their routes and session protocols are different.

## Text with Microsoft Entra ID

`FoundryProvider` accepts a caller-created **sync or async credential object**.
Async Azure Identity credentials are preferred for voice applications. Nagents
has no runtime Azure SDK dependency, Azure extra, or mandatory Azure imports.
`azure-identity` is declared only in the repository's development dependency group
for SDK integration tests. Applications using Azure Identity install it themselves
(`pip install nagents azure-identity`) and choose their credential:

```python
import asyncio
import os
from azure.identity.aio import DefaultAzureCredential
from nagents import FoundryProvider
from nagents.types import Message


async def main():
    async with DefaultAzureCredential() as credential:
        async with FoundryProvider(
            base_url=os.environ["FOUNDRY_ENDPOINT"],
            model=os.environ["FOUNDRY_BACKEND_DEPLOYMENT"],
            credential=credential,
            scope=os.getenv("FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default"),
            api="responses",  # or chat_completions for supported deployments
        ) as provider:
            async for event in provider.generate([Message(role="user", content="Hello")]):
                print(event)


asyncio.run(main())
```

The small structural `TokenCredential` and `AsyncTokenCredential` protocols
require `get_token(*scopes: str)` returning a token or an awaitable token,
respectively. A token exposes a read-only `.token: str`. Both
`azure.identity.DefaultAzureCredential` and
`azure.identity.aio.DefaultAzureCredential` satisfy these contracts directly, as
do the corresponding managed/workload/service-principal credentials. Synchronous
`get_token` runs in a worker thread so voice and other event-loop tasks continue;
async methods are awaited normally. Decorated methods returning awaitables are
also supported. No inheritance from an Azure or Nagents class is required.
`FoundryProvider` adapts this object to the shared internal
`bearer_token_provider` seam, reusing text and Live transports rather than
implementing another authentication transport. There is no Codex discovery.

Nagents calls `get_token(scope)` for every HTTP attempt (including retries),
Live REST request, and WebSocket connection/attach/fork. The credential owns token
caching and renewal; a fresh method invocation does not necessarily fetch a new
token. Custom credentials must return a currently usable token. Tokens are never
assigned to `api_key`. An established WebSocket is not
reauthenticated mid-session; reconnect explicitly if the service requires it.

The **caller owns the credential**: keep it alive until all providers, agents,
and Live connections have closed, then `await credential.close()` or exit its
async context. For a sync credential, call its synchronous `close()` yourself.
Cancellation propagates through token acquisition, but cannot stop an already
running synchronous SDK call in its worker thread. Keep that credential alive
until the call finishes, even after cancelling its awaiting task. The following
sync alternative encloses `asyncio.run`, which drains its default executor before
returning, so credential shutdown happens after pending worker calls finish:

```python
from azure.identity import DefaultAzureCredential


async def run_with_credential(credential):
    async with FoundryProvider(
        base_url=os.environ["FOUNDRY_ENDPOINT"],
        model=os.environ["FOUNDRY_BACKEND_DEPLOYMENT"],
        credential=credential,
    ) as provider:
        async for event in provider.generate([Message(role="user", content="Hello")]):
            print(event)


with DefaultAzureCredential() as credential:
    asyncio.run(run_with_credential(credential))
```

Batch and
Realtime reject token-provider auth rather than dropping it; unsupported text
contracts also fail at construction. Authenticated endpoints are validated before
token acquisition and redirects are not followed. Use HTTPS/WSS (loopback is
allowed for offline testing).

## API keys

```python
provider = FoundryProvider(
    base_url=os.environ["FOUNDRY_ENDPOINT"],
    model=os.environ["FOUNDRY_BACKEND_DEPLOYMENT"],
    api_key=os.environ["FOUNDRY_API_KEY"],
)
```

Pass exactly one of `credential` or `api_key`; neither is implicitly discovered.

`FoundryProvider` selects the header by transport and authentication type:

| Transport | API key | Entra credential |
| --- | --- | --- |
| Text REST and Live REST/WebRTC | `Authorization: Bearer <key>` | `Authorization: Bearer <token>` |
| GPT-Live WS, including attach/fork | `api-key: <key>` | `Authorization: Bearer <token>` |

OpenAI WebSockets retain Bearer authentication. The legacy Azure deployment
provider still uses `api-key`. Credentials are never placed in query strings,
and redirects remain blocked. Do not put a renewable Entra token into `api_key`.

## Hosted and client GPT-Live

For repository development, install the SDK's **dev group** with
`poetry install --with dev -E dev -E voice`. For application use, install your own
SDK: `pip install 'nagents[voice]' azure-identity python-dotenv`.
With this repository checked out:

```bash
export FOUNDRY_ENDPOINT=https://RESOURCE.openai.azure.com/openai/v1
export FOUNDRY_LIVE_DEPLOYMENT=YOUR_LIVE_DEPLOYMENT
export FOUNDRY_BACKEND_DEPLOYMENT=YOUR_RESPONSES_DEPLOYMENT
python examples/live/foundry.py --mode hosted --auth entra
python examples/live/foundry.py --mode client --auth entra
python examples/live/foundry.py --mode client --identity workload
python examples/live/foundry.py --mode hosted --identity managed
# Set FOUNDRY_API_KEY to use --auth key instead.
```

The example accepts `--identity`, `--endpoint`, `--deployment`, `--backend`, `--scope`, and
`--duration`. Hosted mode sets `LiveConfig(delegation="responses",
backend_model=...)`: Foundry invokes the configured Responses deployment.
Client mode supplies a separate Nagents `delegation_agent`, whose text requests
acquire tokens independently. Its provider can be replaced with another backend.
The credential context encloses both agents and their shutdown.

`LiveAPI(provider)` derives REST URLs from that same prefix. WebRTC setup is a
trusted-backend call to `create_webrtc(sdp, session)`; relay only the SDP answer
to the browser. Do not send the resource key or Entra token to the browser.
GPT-Live does not issue ephemeral client keys. WS uses the corresponding
`wss://RESOURCE.openai.azure.com/openai/v1/live/sessions` route, including
`/{id}/attach` and `/{id}/fork` suffixes.

## Credential selection and lifecycle

Use `--identity default` for Azure's `DefaultAzureCredential` chain. The SDK
tries configured environment credentials, workload identity, managed identity,
then its enabled developer credential sources (for example Azure CLI). The exact
chain depends on SDK version/platform/options. Nagents neither recreates that
chain nor chooses an identity itself. Use SDK exclusion options to constrain
`DefaultAzureCredential`, or the explicit choices below to avoid fallback:

```python
credential = DefaultAzureCredential(
    exclude_environment_credential=True,
    exclude_workload_identity_credential=True,
    exclude_managed_identity_credential=True,
)  # Developer-source chain; still caller-owned, close after providers.
```

For deployment, configure the intended identity and grant
it the appropriate data-plane role/access on the existing resource:

| Identity | Configuration / async credential option |
| --- | --- |
| Workload identity | `--identity workload`: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_FEDERATED_TOKEN_FILE`; `WorkloadIdentityCredential()` |
| Managed identity | `--identity managed`: `ManagedIdentityCredential()` for system-assigned; set `AZURE_CLIENT_ID` for user-assigned |
| Service principal | `--identity service-principal`: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`; `ClientSecretCredential(tenant_id, client_id, client_secret)` |

Import these alternatives from `azure.identity.aio`, wrap the selected credential
in `async with`, and pass it as `FoundryProvider(credential=credential, ...)`.
Do not commit secrets. Scope defaults in the example to
`https://ai.azure.com/.default`; override `FOUNDRY_TOKEN_SCOPE`/`--scope` for the
audience required by your cloud/resource.

For existing generic providers, the lower-level async `bearer_token_provider`
callable remains available. Applications may use Azure's official
`azure.identity.aio.get_bearer_token_provider(credential, scope)` with that seam;
`FoundryProvider` accepts the credential directly and does not require this helper.

## Service evidence and remaining assumptions

Offline tests verify routing, authentication freshness, shutdown and redirect
blocking. Final Azure validation additionally passed actual native hosted and
client GPT-Live inference with audio using API keys, DefaultAzureCredential with
local login, ClientSecretCredential for a service principal,
ManagedIdentityCredential on ACI, and WorkloadIdentityCredential on AKS. These
results apply to the tested environment, not all regions or subscriptions.

SP/MI/WI tokens were app-only (`idtyp=app`, no `scp`); the tested flows do not
require delegated-user tokens. MI/WI principal claims matched the created
identities with no fallback. An initial SP 401 resolved after RBAC propagation.

Microsoft's [WebRTC guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/gpt-live-webrtc)
explicitly shows **Bearer API-key** authentication. Its
[WebSocket guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/gpt-live)
shows Bearer access-token authentication. A separate live Azure test found that
Bearer API-key WS authentication returned a redirect, while the **`api-key`
header successfully completed hosted and client inference**. `FoundryProvider` therefore
uses `api-key` for API-key WebSocket connections, including attach/fork; it does
not follow that redirect. REST key behavior remains as documented. The
[delegation guide](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/gpt-live-delegation)
documents hosted Responses and client modes. Token/session expiry, WebRTC and
sideband, optional fork/recording/SIP operations, other tool/operation variants,
and certificate credentials remain untested against Azure. Confirm those features
and alternate scopes against your resource; endpoint derivation alone does not
establish Azure feature parity.

All temporary test resources, identities, and role assignments were deleted and
cleanup verified; existing resources were retained. The temporary
`gpt-live-1-test` deployment was also deleted. Recreate a suitable deployment or
configure an existing deployment before running these examples. See the
[validation matrix and cleanup record](../development/foundry-auth-plan.md).
