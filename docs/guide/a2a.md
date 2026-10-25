# Remote agents over A2A

Nagents can expose a **native framework `Agent`** as a separate authenticated
A2A service. The `ngn a2a serve` command is an optional adapter for an ngn agent
profile using the same service boundary. Neither requires the local browser UI.

A useful deployment is a small agent running on a server with tools for that
server's resources. A local agent calls it through A2A instead of receiving SSH
credentials or exposing the server's filesystem directly. Providers, tools,
workspace and exported profile remain server configuration.

Install only where needed:

```sh
pip install 'nagents[a2a]'
```

The integration uses official `a2a-sdk>=1.2.3` (locked to 1.2.3), protocol **1.0**, JSON-RPC at
`/rpc`, and SSE streaming. Its public minimal Agent Card is at
`/.well-known/agent-card.json`. The card declares one configured profile,
`text/plain` input, `text/plain`/PNG/JPEG output and streaming. Push callbacks, gRPC, REST bindings,
extended cards, file fetching and protocol 0.3 compatibility are not supported.
Their capabilities are not advertised. Unsupported operations use SDK errors.

## Image artifacts

Native framework agents can publish complete image bytes from a host tool using
`await nagents.a2a.publish_image(data, media_type="image/png", filename="thumbnail.png", caption="...")`.
The publisher belongs to the current authenticated A2A execution. It returns an
artifact ID; keep that small receipt in the tool result. Image bytes travel in
protocol `Part.raw`, without copying Base64 into a model answer.

For a file that the host has already produced and authorized:

```python
from pathlib import Path
from nagents.a2a import publish_image_file


async def share_prepared_thumbnail() -> dict[str, str]:
    """Publish the prepared thumbnail under the host's normal tool permissions."""
    artifact_id = await publish_image_file(
        Path("/srv/approved-output/thumbnail.jpg"),
        media_type="image/jpeg",
        caption="Prepared thumbnail",
    )
    return {"artifact_id": artifact_id}
```

Register this function on the native `Agent` with its usual tool executor.
Neither publication helper grants file access or bypasses approval. The file
helper uses a bounded read. Calls outside an active execution fail explicitly.
PNG and JPEG containers are checked; the receiving browser performs final pixel
decoding and reports decoding failures with a download action.

An exported ngn profile also discovers `builtin.a2a` through `channel_list`.
Use its existing `channel_send` with destination `current`, empty text, and
workspace-relative image attachments. This uses the existing file boundaries,
tool policy and approval flow. Ordinary assistant text supplies the caption.

Each remote response supports at most three images and 8 MiB of image bytes in
total. A server's configured `max_output_bytes` budget also applies to text and
image bytes together; its default is smaller than this image ceiling. A batch
that fails validation or budget checks publishes no image from that batch.
URL-backed parts are rejected with an explicit error: neither host nor browser
automatically downloads them or forwards credentials to a file URL.

`RemoteConversation` exposes complete images through `RemoteUpdate.images`,
preserving task-scoped artifact ID, part position, MIME type, filename, caption
and bytes. Artifact updates append protocol parts; final snapshots replace the
same artifact instead of duplicating it. A raw image part must contain a complete
file, rather than byte fragments distributed across unrelated parts.

The harness stores image content in the owning conversation's SQLite history.
The Web UI receives small authenticated asset references and renders the existing
image card, viewer and download action in the chat and remote child conversation.
Parent notifications attach owned child images when the selected provider accepts
the format and image budget; otherwise the notification retains the reference
with an explicit model-input status. Results still reach only the immediate
parent. Reloads preserve the original bytes and asset identity, and root deletion
removes their history. Every browser image request goes through ngn.

## Native framework application

```python
from nagents import Agent, OpenAIProvider, SessionManager
from nagents.a2a import A2AConfig, AgentContext, create_app


def factory(context: AgentContext) -> Agent:
    return Agent(
        provider=OpenAIProvider(),
        session_manager=SessionManager(context.data_dir / "sessions.db"),
        system_prompt="Help with this server's approved maintenance tasks.",
        tools=[],  # Only host-configured functions, plugins and executors.
        streaming=True,
    )


app = create_app(config=service_config, agent_factory=factory)
```

`service_config` is an `A2AConfig` loaded from your private deployment file.
The factory returns a fresh ordinary `Agent` for each execution; the adapter
uses its existing `run` loop, history, provider and tools, then closes it.
`AgentContext` contains the selected profile, validated principal, public
task/context IDs, internal session ID and private data directory. It never
contains the incoming bearer token. Use the supplied session and principal
identity for routing; message metadata cannot choose another user's session.

Native tools require a task-bound approval by default. A host can explicitly
declare known read-only tool names with
`create_app(..., read_only_tools=frozenset({"machine_status"}))`. This extra gate
wraps the original `ToolExecutor`; it does not replace the embedding
application's permission checks. Tool function, schema, description and exact
arguments are checked again after approval.

The runnable [machine-agent example](https://github.com/abi-jey/nagents/tree/main/examples/a2a)
includes a server, client, Client Credentials and browser PKCE login helpers,
and deployment configuration. The server reads OS/filesystem information where
it runs; no ngn Harness is involved.

## Export an ngn profile

```sh
ngn a2a serve \
  --workspace /srv/maintenance \
  --config /etc/ngn/config.json \
  --a2a-config /etc/ngn/a2a.json \
  --host 127.0.0.1 --port 8781
```

The `agent` in `a2a.json` is the only exported profile. An optional `--agent`
must match it. The adapter preserves normal ngn provider selection, tools,
subagents and approval policies. Remote callers cannot enable profiles or
select another workspace. Local UI/admin routes are not mounted.

## Authentication and permissions

Nagents is an OAuth protected resource, **not an identity provider**. Configure
an existing OAuth/OIDC issuer and its API audience. Machine callers obtain
Client Credentials access tokens. Human callers use Authorization Code with
PKCE. ID tokens are not API access tokens.

JWT validation checks the trusted issuer, allowed asymmetric algorithms, JWKS
signature/key rotation, audience, validity window and access-token class.
The default token class is JWT `typ=at+jwt`; an IdP with another signed access
token marker needs explicit `token_kind_claim` and `token_kind_value`.
For example, Keycloak access tokens use the signed `typ: Bearer` claim; its ID
tokens use `typ: ID`. Opaque tokens can use configured trusted introspection
with `validation: introspection`, an `introspection_endpoint`, `client_id` and
`client_secret_env`. Tokens cannot supply a different issuer or JWKS endpoint.

Every protected RPC requires `ngn:access` plus its operation scope:

| Scope | Permission |
| --- | --- |
| `ngn:invoke` | Send work to the configured agent |
| `ngn:read` | Get/list owned tasks, artifacts and streams |
| `ngn:cancel` | Cancel owned work |
| `ngn:approve` | Review and decide the caller's task-bound requests |
| `ngn:approve:any` | Additionally review another principal's requests in the same tenant; grant only to a dedicated operator |

Scope/tenant/role claim names and explicit role-to-scope mappings are host
configuration. Ownership combines trusted issuer, subject and optional tenant.
A task ID, context ID, login cookie or approval URL alone grants no access.
Task reads, listing, continuation, subscription and cancellation all enforce
ownership. Authentication failures return HTTP 401/403 with a Bearer challenge.

These are separate permissions: calling A2A, authorizing a local tool, and
obtaining a downstream service token. An incoming access token never becomes a
provider credential or an automatic shell approval.

## Approval, additional input and downstream consent

When an action requires approval, the task enters `AUTH_REQUIRED`. Its safe
status includes an opaque `/authorize/...` link. That page requires the
configured IdP login and displays the exact operation. Allow/deny is single-use
and bound to the task, owner, operation digest, nonce and expiry. Denial rejects
the task; expiry fails it. Cancellation invalidates pending requests. Earlier
completed work is retained, so inspect it before retrying.

For browser review configure `browser_client_id` (and optionally a
`browser_client_secret_env`) with the **exact** redirect URI
`<public_url>/oauth/callback`. `browser_scopes` default to
`ngn:access ngn:approve`; grant `ngn:approve:any` separately if a human operator
must approve a machine principal's tasks in their tenant. A regular user's
login cannot approve a service account's work.

Headless clients can GET the authorization URL with an appropriate Bearer token
and `Accept: application/json`. The result supplies the exact operation,
`nonce` and `digest`. POST `{ "approved": true, "nonce": "…", "digest": "…" }`
to its `/decision` URL with the same authentication and JSON content type.
This is an ngn out-of-band HTTP flow, not a new standard A2A RPC.

A host tool can explicitly request more input:

```python
from nagents.a2a import request_input


async def choose_target() -> str:
    return await request_input("Which approved target should I inspect?")
```

The task enters `INPUT_REQUIRED`. Send a fresh user message with its task and
context IDs to resume the same in-memory Agent/tool call. A retained message ID
does not supply new input. There is one pending input per execution.

For downstream OAuth, configure a named `services` entry with trusted issuer,
client ID, fixed scopes, mandatory `api_origin`, optional OAuth resource and
secret environment-variable name.
For example, the service-specific part of the configuration can be:

```json
{
  "services": {
    "calendar": {
      "issuer": "https://identity.example.com",
      "client_id": "calendar-consent-client",
      "scopes": ["calendar:read"],
      "api_origin": "https://calendar.example.com",
      "resource": "https://calendar.example.com"
    }
  }
}
```

Register the same exact callback URI with that downstream IdP. A host tool calls
`await request_credentials("service-name")`; the task pauses for a separate
PKCE/consent flow and receives an opaque `AccessGrant`. Use
`httpx.AsyncClient(auth=grant.auth(), trust_env=False, follow_redirects=False)`
in host code. Its auth guard rejects a request to any origin other than the
configured API target before sending its bearer token. OAuth resource identity
and the API origin are separate settings. Keep redirects disabled and return
only public service results; never return the grant, headers, tokens or token
responses to the model. Incoming A2A tokens are never forwarded.

Downstream grants are task/owner/service-bound, private on disk and expire.
This version requests fresh consent for each acquisition; it does not refresh
or silently reuse grants. Private credential records remain in the service's
data directory until operator retention/cleanup, independently of token expiry.

## Client, retries and task lifetime

### Connect from ngn

Ask the assistant to add a named A2A agent using its server URL, or open
**Settings → Connect remote agent**. ngn discovers the host's Agent Card,
same-origin JSON-RPC endpoint and sign-in requirements, then saves the
connection in the workspace's trusted `connections/a2a.json` settings. Remote
agents appear alongside local definitions in Team and in the conversation
picker. The next model round can delegate to a newly added agent without a
restart. Updates affect new work; active and retained work keeps its accepted
connection. Remote definitions are JSON, separate from local agent Markdown.
The configured name is a stable local routing alias. Team and connection
settings show the remote Agent Card's advertised name and description separately;
those labels refresh on a successful probe or outbound connection, or on the
next Team/task refresh when the last check is older than 60 seconds. One card
fetch updates every current local alias for that same connection. Each metadata
fetch has a 12-second total deadline; demo and reviewer contexts never schedule
it. While a fetch is pending the UI reports “Checking”; after a failure it keeps
the last known labels, reports “Refresh failed”, and retries on a later poll
(after a 60-second backoff). A saved local description remains an override, and
card metadata never changes the endpoint, authentication binding or tool trust.
The remote labels are untrusted descriptive data, not instructions.

Issuer and requested scopes come from the host's advertised authentication
metadata. A public PKCE client ID can be advertised using the optional
`urn:nagents:a2a:oauth-client:v1` Agent Card extension. Native servers advertise
their `browser_client_id` and `browser_scopes` when the client has no
`browser_client_secret_env`; confidential credentials are never advertised.
The extension is an ngn convention, not a standard A2A client-registration
field. Identity-provider registration remains necessary.

The UI offers **Sign in**, without issuer/client/scope input fields. Missing
public client metadata produces a host-setup error. Register the exact ngn
callback URL, `/remote-agent-login/callback`, with the host's public client in
its identity provider. Provider rejections returned to ngn identify the
client/callback setup and show that exact URL. A provider may reject an invalid
callback without returning to ngn; the same registration guidance is shown
beside sign-in. The UI shows credential status and expiry from the protected
local record; checking it does not contact the remote host. When the issuer
returns a refresh token, ngn stores the access and rotating refresh credentials
in the user's protected credential store and refreshes them on use. The default
location is `~/.local/share/ngn/auth/a2a/<connection-fingerprint>.json`, or
`$XDG_DATA_HOME/ngn/auth/a2a/` when `XDG_DATA_HOME` is set. Sign-in survives
stopping and restarting `ngn serve`; workspaces using the exact same connection
share that user's grant and refresh lock. Connection definitions and remote
tool trust remain workspace settings. Files are plaintext protected by private
filesystem ownership and permissions (directories `0700`, files `0600`), not
application-level encryption. Containers must persist the user data directory.
After upgrading from workspace credential storage, sign in once in the new
store. An operator preserving an existing grant must stop the old ngn process
before moving it; do not keep independently refreshable copies in both stores.
If the issuer does not
provide a refresh token, ngn asks for sign-in again after access-token expiry.
An uncertain refresh is never replayed with the old refresh token; sign in again
before sending another task.

Tokens and PKCE verifiers stay on the ngn backend. The browser's API traffic
remains same-origin; the deliberate identity-provider sign-in navigation is
separate. The backend also proxies native ngn remote action reviews into the
normal approval UI. Reviewer/demo permission ceilings cannot execute remote
tasks. The remote server controls its model, instructions and tools, so local
model/effort/compaction overrides do not apply. Remote context usage is shown
as unavailable rather than estimated with the local model's window.

Workspace owners can enable **Trust remote tool operations** for a configured
connection. The choice is stored with the workspace's protected
`connections/a2a.json` catalog and applies to remote conversations from that
workspace. It is not limited to one chat. Trust follows the exact connection
fingerprint and is cleared when its endpoint or authentication binding changes.
For a currently accepted and trusted connection, ngn can automatically approve
the validated remote operation while build-mode delegation is enabled. Local
reviewer, demo, read-only and inherited tool restrictions still apply. Task,
origin, operation-size, nonce and digest checks remain in force; this trust
choice does not approve local tools or ordinary task dispatches. Without trust,
remote operations continue to require the normal mandatory approval.

Receiving a delegation handle confirms local acceptance, not a connection to
the remote server. A failed remote child returns a safe, specific reason to
its immediate parent and the UI, including missing/expired sign-in, rejected
credentials, denied access, TLS/network failures, timeouts, and remote task
failure/rejection/cancellation. Credential preflight failures explicitly say
that no task was sent. An HTTP 401/403/429 before any task update is recorded
as a rejected request, with no accepted task. Other failures after dispatch
retain the message/task identity and warn about the uncertain outcome;
ngn never automatically sends the request again. Backend diagnostics retain the failure
code, phase, local correlation IDs, and outcome without tokens, upstream
response bodies, or exception text. Framework callers can inspect
`RemoteAgentError.code` instead of parsing an exception message.

### Use the library directly

`RemoteAgentConfig` is a data-only JSON schema and can be imported without the
optional SDK. `discover_remote` and `RemoteConversation` are framework APIs;
they do not depend on the ngn harness or pretend to be model providers.

```python
import os
import uuid

from nagents.a2a import RemoteAgent, RemoteAgentConfig, RemoteConversation, discover_remote


async def access_token() -> str:
    # A resource-specific token supplied by the embedding's private auth layer.
    return os.environ["PEER_ACCESS_TOKEN"]


async def ask_peer() -> str:
    discovered = await discover_remote(RemoteAgentConfig(url="https://agent.example.test"))
    async with RemoteAgent.from_config(discovered.connection, access_token) as peer:
        conversation = RemoteConversation(peer)
        answer = ""
        async for update in conversation.exchange("Inspect the server's available tools", message_id=str(uuid.uuid4())):
            answer = update.text
        return answer
```

Embeddings own credential acquisition and durable send admission. Connection
JSON contains public metadata and optional `token_env` secret references, never
tokens. `ca_file` adds a trusted private CA without disabling HTTPS verification.
An explicit HTTPX transport can be supplied for embedding/testing. No remote
token is sent during public discovery, to redirects or to a different origin.

`RemoteAgent` uses the official SDK underneath. Supply a fixed origin and a
host-owned async access-token callback; the callback runs for each HTTP request
and can use your IdP library's resource-specific token cache. Discovery is
restricted to the exact configured JSON-RPC URL. It cannot move credentials to
another card-advertised endpoint or a redirect target.

Use `send`, `get_task`, `list_tasks`, `subscribe` and `cancel`. A closed browser
or stream does not cancel remote work. Poll/re-subscribe with the same owner;
explicit cancellation is separate. Public output is text/status/artifacts,
not tool arguments, private reasoning or system prompts.

Keep the client-generated message ID and exact payload for an uncertain send.
Duplicate identical sends observe the existing owned task without rerunning
actions. Reusing an ID with different content is rejected. This is a bounded
deduplication contract, not an end-to-end exactly-once guarantee. Finished tasks
do not accept new messages; a new task can use the same owned context.

The service persists tasks, ownership, messages/artifacts and bounded events.
After restart, interrupted tasks become inspectable failures. Agent workers,
approval Futures and input waits are not restored; tools are never silently
replayed. Cancellation stops async execution and nested ngn work. A synchronous
tool already dispatched to a thread, or a downstream side effect already
accepted, cannot be rolled back by cancelling the task. Use cooperative async
tools or managed subprocesses for cancellable operations.

## Deployment and limits

Run **one POSIX process/worker** per private persistent data directory. A process
lock enforces ownership. Use a dedicated OS user, private directories/files and
backups with equivalent access controls. Retained-task, active-task, event,
request/output, approval-timeout and request-rate limits are configurable;
admission fails when a retained-task quota is full. No automatic deletion is
implied.

Production requires HTTPS. Terminate TLS at a trusted reverse proxy, preserve
the configured public Host, and forward the HTTPS scheme only from explicitly
trusted proxy addresses (`--trusted-proxies`; never a wildcard on an exposed
listener). Alternatively supply `--tls-certfile` and `--tls-keyfile`. Keep OAuth
callback query strings and authorization headers out of proxy access logs.
The CLI disables ordinary access logs; sanitized audits use request IDs,
operation, status, duration and exception type/frame locations without bodies,
tokens, exception messages or frame locals.

`loopback_http: true` is explicit development transport permission. It still
requires OAuth and rejects non-loopback callers/binds. It is not an auth bypass.
Keep the local ngn web service and its security settings separate.
