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

The integration uses official `a2a-sdk==1.2.3`, protocol **1.0**, JSON-RPC at
`/rpc`, and SSE streaming. Its public minimal Agent Card is at
`/.well-known/agent-card.json`. The card declares one configured profile,
`text/plain` input/output and streaming. Push callbacks, gRPC, REST bindings,
extended cards, file fetching and protocol 0.3 compatibility are not supported.
Their capabilities are not advertised. Unsupported operations use SDK errors.

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
