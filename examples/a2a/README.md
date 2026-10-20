# A native agent on a remote machine

This example deploys an ordinary framework `Agent` next to a server's resources.
A local process uses A2A to ask for that server's OS and disk capacity. Providers
and tool access stay on the server. The optional ngn Harness adapter is described
in [the A2A guide](../../docs/guide/a2a.md).

From the repository root, install the optional integration in your environment:

```sh
pip install -e '.[a2a]'
```

For a reproducible local IdP with Docker, the disposable helper generates random
credentials in a private temporary directory, starts the pinned Keycloak image
on an OS-assigned loopback port, and registers the explicit example redirects:

```sh
python -m examples.a2a.keycloak_fixture up
python -m examples.a2a.server --config /tmp/ngn-a2a-idp/a2a.json
```

In another terminal, load only that private generated environment and map its
fixed IdP endpoints to the example client:

```sh
. /tmp/ngn-a2a-idp/credentials.sh
export NGN_A2A_TOKEN_URL="$NGN_IDP_TOKEN_URL"
export NGN_A2A_CLIENT_ID="$NGN_IDP_SERVICE_CLIENT_ID"
export NGN_A2A_CLIENT_SECRET="$NGN_IDP_SERVICE_CLIENT_SECRET"
python -m examples.a2a.client http://127.0.0.1:8781 --loopback-http --authentication client-credentials
```

For browser login instead, set `NGN_A2A_CLIENT_ID=nagents-browser` and
`NGN_A2A_AUTHORIZATION_URL="$NGN_IDP_AUTHORIZATION_URL"`, then select
`--authentication browser`. Alice/Bob passwords are in the private fixture
environment; do not paste that file into logs or a chat. The helper neither
prints tokens/passwords nor installs a production IdP.

With the development test dependencies installed, run the real-IdP integration
case and then stop only this helper's labelled container:

```sh
NGN_A2A_KEYCLOAK_E2E=1 python -m pytest tests/test_a2a_oauth.py -k live_keycloak
python -m examples.a2a.keycloak_fixture down
```

`--directory` selects another private fixture directory; point
`NGN_A2A_KEYCLOAK_FIXTURE` there for tests. Stop the example agent before stopping
its IdP. Cleanup removes generated credential/configuration files; it preserves
any `server-state` history the example agent created. The helper requires a
local POSIX Docker host and never mounts its socket into Keycloak.

Copy `examples/a2a/config.example.json` to a private configuration path and set
your service origin, private persistent data directory, trusted IdP issuer and
API audience. The example's `typ: Bearer` access-token marker is Keycloak
specific. Other IdPs should use their signed access-token marker or the default
`at+jwt` JWT type. Register clients and API scopes at the IdP:

- A confidential Client Credentials client for machine calls, with API audience
  `nagents-a2a` and `ngn:access ngn:invoke ngn:read ngn:cancel`.
- A public browser client with PKCE S256, the same API audience and scopes, and
  exact redirect `http://127.0.0.1:8780/callback` for the local example client.
- Optionally a separate reviewer browser client registered with exact
  `<service-public-url>/oauth/callback`, scopes `ngn:access ngn:approve`, and
  `ngn:approve:any` only for an explicitly authorized same-tenant operator.

The server uses `OpenAIProvider`'s normal local configuration discovery
(including a saved Codex login). `NGN_A2A_MODEL` selects its model. These are
server-side provider credentials, separate from callers' A2A access tokens.
You can replace the provider with another ordinary nagents provider.

```sh
python -m examples.a2a.server --config /private/a2a.json --port 8781
```

The example binds loopback. Put a trusted TLS reverse proxy in front of it for
the configured public origin; preserve Host and HTTPS scheme. For a local-only
trial set `public_url` to `http://127.0.0.1:8781`, `loopback_http` to `true`, and
use an explicitly permitted loopback IdP issuer. OAuth remains mandatory.

`ngn-a2a.service` is a systemd deployment template for the optional ngn profile
adapter. Install `nagents[a2a]` in its dedicated venv, create the service account,
and supply its private provider configuration and `/etc/ngn/a2a.env` through your
normal secret manager. The service creates one private state directory and runs
one process. Only its configured workspace is writable. Adjust the named paths
and expose port 8781 through your trusted HTTPS reverse proxy; keep callback
query strings and authorization headers out of proxy logs. The native
`create_app` factory can be hosted by the same single-process ASGI arrangement.

## Machine caller

Set host environment variables for your IdP's fixed endpoints and client:

```sh
export NGN_A2A_TOKEN_URL=https://identity.example.com/realms/agents/protocol/openid-connect/token
export NGN_A2A_CLIENT_ID=machine-caller
export NGN_A2A_SCOPES='ngn:access ngn:invoke ngn:read ngn:cancel'
# Supply NGN_A2A_CLIENT_SECRET through your normal secret manager/environment.
python -m examples.a2a.client https://machine-agent.example.com --authentication client-credentials
```

If your IdP requires the OAuth `resource` parameter, set `NGN_A2A_RESOURCE` to
the configured A2A resource. Audience assignment is IdP configuration; an ID
token or a model API key is not a substitute.

## Human caller with PKCE

Keep the token URL and set the authorization endpoint and public client ID:

```sh
export NGN_A2A_AUTHORIZATION_URL=https://identity.example.com/realms/agents/protocol/openid-connect/auth
export NGN_A2A_CLIENT_ID=local-user-client
python -m examples.a2a.client https://machine-agent.example.com --authentication browser
```

The helper binds only `127.0.0.1:8780`, opens the IdP login, validates a
single-use random state, and exchanges the returned code with its S256 verifier.
It uses the access token in memory and never displays tokens or accepts an ID
token as the API credential. `NGN_A2A_LOGIN_PORT` can change the callback port;
register the new exact redirect URI at your IdP first. It does not save or
refresh tokens; expiry triggers another login. For local HTTP endpoints add
`--loopback-http` explicitly.

To supply a token acquired by an existing IdP client instead, use the default
`environment` authentication with `NGN_A2A_ACCESS_TOKEN`. In an application,
replace the example `Tokens` callback with your audience-specific cache/refresh
logic. `RemoteAgent` requests credentials per HTTP call and does not retry sends.

## Connect this tool to another agent

`local_agent.py` is a runnable native Agent with an `ask_machine` tool:

```sh
python -m examples.a2a.local_agent https://machine-agent.example.com \
  --data-dir /private/local-agent --authentication client-credentials
```

It uses the same IdP environment settings as the client above. Its local model
and remote model each use their own provider credentials; neither receives the
other process's login.

An async tool on the **local** framework Agent can use `RemoteAgent` with the
fixed server origin and host-owned token callback from `client.py`. Create a
user message with `a2a.helpers.new_text_message`, set its role to `ROLE_USER`,
and consume `remote.send(SendMessageRequest(message=message))`. Return only the
remote public text/artifacts to the local model. Endpoint, token audience and
credentials should never be model-controlled tool arguments.

The server's `machine_status` tool runs on the remote machine. It is explicitly
host-declared read-only. Every other native tool needs task-bound human approval
unless the host separately declares it read-only; a valid A2A login does not
approve writes. See the guide for `AUTH_REQUIRED`, additional input, downstream
consent, headless decisions, cancellation and restart semantics.
