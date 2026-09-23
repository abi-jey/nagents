# Foundry authentication implementation plan

Research date: 2026-09-23. Based on official guides read in the shared browser,
supplemented by API references and the user's final verified Azure test results
recorded below. Live results apply only to the tested environment; they do not
establish availability or feature parity across all subscriptions and regions.
This documentation update performed no Azure operations.

## Implementation constraints

- Keep `azure-identity` **development-only**, not a runtime/transitive requirement.
- Inject credentials/token providers into `FoundryProvider`; application code
  constructs `DefaultAzureCredential` or another credential. Do not instantiate
  Azure Identity credentials implicitly inside the provider.
- Use narrow typed sync/async credential/token-provider contracts with explicit
  behavior, expiry handling, and ownership. Caller-owned credentials remain
  caller-owned. Preserve API-key authentication as an explicit alternative.
- Share credential plumbing, but keep GPT-Live, Realtime, and Voice Live endpoint
  builders and event protocols separate. GPT-Live validation does not validate
  Realtime or Voice Live.

### Implemented dependency and API decision

`FoundryProvider(base_url=..., model=..., credential=..., scope=...)` now accepts
an async or sync Azure-compatible credential directly, structurally typed without Azure
SDK imports. Its adapter calls `credential.get_token(scope)` through the shared
bearer-token seam for each HTTP attempt or Live connection. It does not implement
an identity chain, token cache, or credential shutdown. Credentials are always
caller-owned; `api_key` is an exclusive alternative. Synchronous token acquisition
is offloaded to a worker thread; async methods are awaited on the calling loop.
Cancellation cannot interrupt an already running synchronous SDK call: caller
shutdown must wait for it to finish. Applications choose `DefaultAzureCredential`
or an explicit sync/async
managed/workload/service-principal credential from their own SDK installation.

`azure-identity` is declared only in `[dependency-groups].dev` for developer
testing, not in project dependencies or any Azure extra. The public implementation
and examples are described in [Foundry authentication](../guide/foundry-authentication.md).
The validation matrix below distinguishes live-service results from offline auth,
routing, redirect, and lifecycle tests.

## Endpoint and authentication boundaries

`{r}` denotes the resource custom subdomain; `{deployment}` is a deployment name.

| Surface | Documented endpoint | Version/model selection |
| --- | --- | --- |
| GPT-Live WebSocket | `wss://{r}.openai.azure.com/openai/v1/live/sessions` | `/v1`; model in `session.start.session`, not Realtime query parameters |
| GPT-Live WebRTC creation | `POST https://{r}.openai.azure.com/openai/v1/live/sessions` | JSON `session` plus `transport: {type: "webrtc", sdp: ...}` |
| GPT-Live sideband | `wss://{r}.openai.azure.com/openai/v1/live/sessions/{session_id}/attach` | Existing session; do not send `session.start` again |
| Realtime GA WebSocket | `wss://{r}.openai.azure.com/openai/v1/realtime?model={deployment}` | No dated version parameter |
| Realtime preview WebSocket | `wss://{r}.openai.azure.com/openai/realtime?api-version=2025-04-01-preview&deployment={deployment}` | Do not mix GA/preview paths or parameters |
| Realtime GA WebRTC | `POST /openai/v1/realtime/client_secrets`, then `POST /openai/v1/realtime/calls` on the OpenAI resource host | Ephemeral-client-secret flow; old preview WebRTC URLs are deprecated |
| Voice Live WebSocket | `wss://{r}.services.ai.azure.com/voice-live/realtime?api-version=2026-04-10&model={model}` | Legacy host: `{r}.cognitiveservices.azure.com`; feature-specific versions also exist |
| Voice Live WebRTC signaling | `wss://{r}.services.ai.azure.com/voice-live/realtime/calls?api-version=2026-01-01-preview&model={model}` | Exact public-preview guide example; SDP exchanged over WebSocket |

Sources: [GPT-Live guide][live], [GPT-Live WebRTC][live-rtc],
[Realtime WebSockets][realtime], [Realtime WebRTC][realtime-rtc],
[Voice Live guide][voice], [Voice Live WebRTC][voice-rtc].

- **Entra:** current Foundry/OpenAI guides request
  `https://ai.azure.com/.default`; send `Authorization: Bearer <access-token>`.
  Voice Live explicitly also accepts legacy
  `https://cognitiveservices.azure.com/.default`. Do not assume that legacy scope
  works for every GPT-Live operation. `/.default` is a requested scope suffix,
  not a literal JWT audience suffix. ARM uses
  `https://management.azure.com/.default`, not the inference audience.
  See [Foundry auth][auth] and the [OpenAI Entra guide][entra].
- **GPT-Live keys:** native hosted and client inference verified the required
  `api-key: <api-key>` WebSocket header in the tested environment. The provider
  now uses this header for key-authenticated WS connections. REST key auth remains
  `Authorization: Bearer <api-key>`; Entra REST/WS remains
  `Authorization: Bearer <access-token>`. The WebRTC guide shows the REST bearer-key
  form; do not transfer it to key-authenticated WebSockets.
- **Realtime keys:** `api-key` handshake header or query parameter.
- **Voice Live:** `api-key` header/query; Entra bearer in `Authorization` header
  or URL-encoded `Authorization=Bearer <token>` query value. Browser WebSockets
  cannot set arbitrary handshake headers. See [Voice Live reference][voice-ref].
- **Browser split:** GPT-Live explicitly has no ephemeral client keys; a trusted
  backend authenticates session creation and relays SDP. Realtime has ephemeral
  client secrets. Do not reuse that flow for GPT-Live or Voice Live.

## GPT-Live hosted/client delegation

Task delegation is not OAuth delegated-user authentication. [Delegation guide][delegate]:

- **Client** (`delegation.type = "client"`, also default): authenticate the live
  connection; application authenticates downstream models/tools independently.
  Return correlated results through `session.thinking.append` or
  `session.commentary.append` with `delegation_id`.
- **Hosted Responses** (`delegation.type = "responses"`): service calls the
  configured Responses deployment; hosted tools run server-side and incur
  Responses charges. Configuration exposes no separate backend credential field.
  Private function calls still run in the application; return results with
  `response.item.create`, then explicitly continue with `response.create`.
- Native hosted and client GPT-Live inference both passed with audio for API keys,
  local-login DefaultAzureCredential, SP, MI, and WI (matrix below). SP/MI/WI tokens
  were app-only (`idtyp=app`, no `scp`); there is **no delegated-only requirement**
  in the tested environment. Internal identity propagation, arbitrary cross-resource
  authorization, and the minimum per-mode RBAC matrix remain unspecified.
- Prefer client delegation for independently controlled downstream identities.
  Treat mode as startup-only: the guide says switching requires a new session,
  despite later ambiguous wording about resetting delegation to null.

## DefaultAzureCredential identity tree

Python's documented chain begins as follows ([credential-chain guide][chain]):

```text
Application constructs credential -> injects into FoundryProvider
  DefaultAzureCredential
    1. EnvironmentCredential: service principal
       secret: AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET
       certificate: tenant/client IDs + AZURE_CLIENT_CERTIFICATE_PATH
    2. WorkloadIdentityCredential: configured federation + projected token file
       AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_FEDERATED_TOKEN_FILE
    3. ManagedIdentityCredential: supported Azure host
       system-assigned, or user-assigned selected by client ID
    4. Developer sources: platform cache/tools, Azure CLI, etc.
```

Sources: [service-principal guide][sp], [certificate reference][environment],
[AKS workload identity guide][workload]. Python workload identity requires
`azure-identity >= 1.13.0`. Direct federation assertion audience
`api://AzureADTokenExchange` is separate from the resulting Foundry access-token
audience. Prefer an explicit credential/constrained chain in production.

Entra needs a custom resource subdomain and RBAC on the target resource. OpenAI's
guide specifies Cognitive Services OpenAI User/Contributor for inference. Voice
Live's guide specifies Cognitive Services User plus Foundry User (formerly Azure
AI User). General Foundry Agent Service is Entra-only; do not transfer that
restriction to GPT-Live Responses delegation.

Live validation used DefaultAzureCredential with local login, explicit
ClientSecretCredential for the SP, ManagedIdentityCredential on ACI, and
WorkloadIdentityCredential on AKS. MI/WI token principal claims matched the
created identities, with no credential fallback. The SP's initial 401 resolved
after RBAC propagation; it was not evidence of a delegated-user-token requirement.

## Refresh and validation matrix

Acquire a current token for every HTTP request/WS connection, reconnect, and
sideband attachment. Reuse the injected provider and respect token expiry.
Refreshing a cached token does not reauthenticate an established socket.
No in-band refresh event or universal socket-at-token-expiry behavior was found
in the reviewed guides/references. Voice Live's SDK claims token refresh, but that
does not establish a portable wire-level refresh protocol. Track session expiry
separately; do not promise seamless resumption. See [OpenAI Entra][entra],
[GPT-Live events][live-ref], and [Voice Live SDK][voice-sdk].

| Validation | Evidence / scope | Status |
| --- | --- | --- |
| Runtime without azure-identity | Package import/key path blocks Azure imports; package metadata has no Azure requirement or extra | Offline tested |
| API key and fake injected sync/async credentials | Headers, scope, per-attempt acquisition, ownership, event-loop responsiveness, and no credential leakage | Offline tested |
| Expiring token/reconnect | Fresh token acquired for new requests/connections; no invented refresh event | Untested |
| GPT-Live hosted + client: API key | Actual native inference with audio; WS `api-key` header | Live passed |
| GPT-Live hosted + client: DefaultAzureCredential | Actual native inference with audio using local login | Live passed |
| GPT-Live hosted + client: ClientSecretCredential SP | Actual native inference with audio; app-only token; passed after RBAC propagation | Live passed |
| GPT-Live hosted + client: ManagedIdentityCredential on ACI | Actual native inference with audio; app-only principal matched created identity; no fallback | Live passed |
| GPT-Live hosted + client: WorkloadIdentityCredential on AKS | Actual native inference with audio; app-only principal matched created identity; no fallback | Live passed |
| GPT-Live WebRTC + sideband | Backend SDP exchange; attach without restarting session | Untested |
| Other credential variants | Certificate SP, unexercised MI assignments/hosts and credential-chain configurations | Untested |
| Negative auth | SP initial 401 resolved after RBAC propagation; broader wrong-scope/expired-token/invalid-key matrix not exercised | Partially observed |
| Other operations | SIP, fork, recording, hosted-tool/function variants, and operations outside the inference runs | Untested |
| Realtime/Voice Live comparison | Product-specific URL/header/version builders; no GPT-Live protocol reuse | Untested |

The canonical test response was **4**. The earlier **44** was a result-aggregation
artifact, not the canonical model response. Live passed means native inference
with audio in these runs, not coverage of every lifecycle/tool operation.

## Availability and unresolved questions

- GPT-Live deployment and inference were verified using `gpt-live-1-test` in the
  tested environment. This does not establish the broader region/version matrix,
  GA/preview designation, or subscription access gates. `gpt-live-transcribe` is a
  separate Realtime transcription model, not evidence of `gpt-live-1` availability.
- The test deployment was deleted during cleanup. Recreate a suitable deployment
  or configure an existing one before running later examples; do not assume
  `gpt-live-1-test` still exists.
- GPT-Live [concurrent-session quotas][quotas]: default 10; tiers 1–5:
  25/50/200/300/500 per subscription. Actual account entitlement is unverified.
- Native WS key authentication and hosted/client app-only inference are resolved
  for the tested environment. Alternate audiences, live attach/fork behavior,
  cross-resource hosted authorization/minimum RBAC, mode-update semantics, and
  socket/token expiry behavior still require validation.
- Realtime guides list global deployments in East US 2 and Sweden Central;
  these regions cannot be assumed for GPT-Live.
- Voice Live native models are managed; BYOM requires deployment. Speech-only
  resources lack BYOM/Foundry Agent integration. Check the [regional matrix][regions].
  Voice Live WebRTC is public preview without SLA and uses Global Standard routing.

## Verified cleanup and future requirements

Final test results confirm deletion and verification of the temporary
`gpt-live-1-test` deployment, `rg-foundry-auth-test` (AKS, ACI, managed identities,
federation, and storage), the SP/app, and test-created role assignments. Existing
resources were retained. No secrets or identity/resource IDs are needed here.

Future live tests require separately authorized Azure work. In private test
records, track exact IDs and pre-existing state for any test-created resource groups,
resources/deployments, app registrations/service principals, identities, federated
credentials, secrets/certificates, and role assignments. Close sessions, delete
only test-owned artifacts, revoke test-created credentials and RBAC assignments,
and restore any explicitly modified settings. Preserve pre-existing resources and
assignments. Verify cleanup and record residual resources/cost exposure; never
store keys or bearer tokens in this document, logs, or git.

[live]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/gpt-live
[live-rtc]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/gpt-live-webrtc
[live-ref]: https://learn.microsoft.com/en-us/azure/foundry/openai/gpt-live-reference
[delegate]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/gpt-live-delegation
[realtime]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/realtime-audio-websockets
[realtime-rtc]: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/realtime-audio-webrtc
[voice]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to
[voice-rtc]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-webrtc
[voice-ref]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-api-reference-2026-04-10
[voice-sdk]: https://learn.microsoft.com/en-us/dotnet/api/overview/azure/ai.voicelive-readme
[auth]: https://learn.microsoft.com/en-us/azure/foundry/concepts/authentication-authorization-foundry
[entra]: https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/managed-identity
[chain]: https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/credential-chains
[sp]: https://learn.microsoft.com/en-us/azure/developer/python/sdk/authentication/local-development-service-principal
[environment]: https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.environmentcredential
[workload]: https://learn.microsoft.com/en-us/azure/aks/workload-identity-overview
[quotas]: https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits#gpt-live-concurrent-session-limits
[regions]: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/regions?tabs=voice-live#regions
