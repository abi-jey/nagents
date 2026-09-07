# Legacy API Server

`python -m nagents.server` runs the retained HTTP/SSE API application. It is also
the Docker image's default command. The current source has **no bundled chat
UI**: `/` and `/ui*` return JSON 404 responses after access checks. The separate
[React web client](ngn-web.md) uses `ngn serve` and the ngn harness, not this API.

!!! warning "Use the current hardened source"
    This guide describes the current checkout. The published `v0.5.0` wheel
    predates the server's token/loopback hardening and UI removal; its runner
    binds to `0.0.0.0` without this authentication boundary. Do not expose that
    older server or assume an image tag contains the fixes. Build from reviewed
    current source, or verify a later release/image before deployment. See
    [release availability](ngn-installation.md#release-availability).

## Local Setup

From the repository root, install into your existing virtual environment:

```bash
.venv/bin/python -m pip install -e '.[server]'
```

Generate a token into your shell environment, not a repository file or literal
in command history. Do not print it, enable shell tracing, or include it in logs.
Use a secret manager for long-lived deployments.

```bash
export NAGENTS_SERVER_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export NAGENTS_SERVER_HOST=127.0.0.1
export PORT=8080
```

Configure a model and provider key separately. This OpenAI example assumes
`OPENAI_API_KEY` and `OPENAI_MODEL` are already set outside the repository:

```bash
export NAGENTS_LLM_PROVIDER=openai
export NAGENTS_LLM_API_KEY="$OPENAI_API_KEY"
export NAGENTS_LLM_MODEL="$OPENAI_MODEL"
export NAGENTS_LLM_BASE_URL=https://api.openai.com/v1
export NAGENTS_SESSIONS_DB="$HOME/.local/share/nagents-server/sessions.db"
export NAGENTS_TOOLS_DIR="$HOME/.local/share/nagents-server/tools"
export NAGENTS_CONFIGS_PATH="$HOME/.local/share/nagents-server/configs.json"
export NAGENTS_MCP_CONFIG="$HOME/.local/share/nagents-server/mcp.json"
export NAGENTS_MCP_ENABLED=false
.venv/bin/python -m nagents.server
```

Choose a writable, private state directory. Session, config, tool, and MCP paths
otherwise default under `/data`, which may not be writable on a local host.
Server configuration uses `NAGENTS_*` environment variables, not ngn's TOML or
`NGN_*` settings. The server may load the checkout's `.env` if `python-dotenv` is
installed; do not depend on that optional behavior for secret provisioning.

## Access Rules

| Setting or route | Current behavior |
| --- | --- |
| `NAGENTS_SERVER_HOST` | Defaults to `127.0.0.1`. A non-loopback bind, including `0.0.0.0`, requires a configured token. |
| `PORT` | Defaults to `8080`. |
| `NAGENTS_SERVER_TOKEN` | Unset permits only loopback clients with loopback Host headers. An explicitly empty or invalid token fails startup. |
| `GET /health` | Public health check, including with authentication configured. |
| Every other route | Requires authentication when a token is configured, including unknown routes, SSE streams, API docs, and attachments. |

Use a high-entropy RFC 6750-compatible token, such as the generated value above.
The server reads it at startup; changing or rotating it requires a restart.
`Authorization: Bearer <token>` is supported. HTTP Basic is also supported with
username `nagents` and the token as password. Query parameters and cookies do
not authenticate requests.

Host headers are validated, cross-origin browser requests are rejected, and no
wildcard CORS policy is enabled. Attachment responses are downloads with
`X-Content-Type-Options: nosniff`, not inline HTML previews.

## API Requests

In a client shell with the same token supplied from your secret store:

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail --header "Authorization: Bearer $NAGENTS_SERVER_TOKEN" \
  http://127.0.0.1:8080/sessions
curl --fail --no-buffer \
  --header "Authorization: Bearer $NAGENTS_SERVER_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"message":"Hello","session_id":"api-example"}' \
  http://127.0.0.1:8080/chat/stream
```

`POST /chat` returns JSON; `POST /chat/stream` returns SSE. Session operations are
under `/sessions`, tool inspection/reload under `/tools`, configuration under
`/configs`, and MCP status at `/mcp/status`. This API is not the `ngn serve`
`/api` contract, and its SSE records are not the headless CLI's JSONL schema.

To use Basic without putting the password in the command, `curl --user nagents
http://127.0.0.1:8080/sessions` prompts for it. Keep credentials out of shared
command output and process diagnostics.

## Docker

The current Dockerfile still launches `python -m nagents.server`, not `ngn serve`.
Build from the repository root after the frontend source is present at
`src/nagents/web-ui/`; the Dockerfile builds its assets in a separate stage:

```bash
docker build -f dockerfiles/Runtime.Dockerfile -t nagents-server:local .
docker run --rm --name nagents-api \
  -p 127.0.0.1:8080:8080 \
  -e NAGENTS_SERVER_HOST=0.0.0.0 \
  -e NAGENTS_SERVER_TOKEN \
  -e NAGENTS_LLM_PROVIDER -e NAGENTS_LLM_API_KEY \
  -e NAGENTS_LLM_MODEL -e NAGENTS_LLM_BASE_URL \
  -e NAGENTS_SESSIONS_DB=/tmp/nagents-server/sessions.db \
  -e NAGENTS_TOOLS_DIR=/tmp/nagents-server/tools \
  -e NAGENTS_CONFIGS_PATH=/tmp/nagents-server/configs.json \
  -e NAGENTS_MCP_CONFIG=/tmp/nagents-server/mcp.json \
  -e NAGENTS_MCP_ENABLED=false \
  nagents-server:local
```

This uses the exported secrets by name. Binding inside the container to
`0.0.0.0` lets the port mapping reach the server; publishing only on host
`127.0.0.1` keeps that mapping local. The image health check uses public
`GET /health`. Example state is ephemeral; provision a private writable volume
for persistence. The example deliberately does not mount the host Docker socket,
so Docker-backed shell tools need separate, explicitly authorized provisioning.

## Kubernetes and Remote Access

The repository's `examples/k8s/deployment.yaml` is a cluster-specific legacy API
deployment, not a web-client deployment. It binds on the pod network and reads
`NAGENTS_SERVER_TOKEN` from the `nagents-server-auth` Secret. Create the `nagents`
namespace first if absent, then provision a separate high-entropy token:

```bash
token_file="$(mktemp /tmp/nagents-server-token.XXXXXX)"
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > "$token_file"
kubectl create secret generic nagents-server-auth --namespace nagents \
  --from-file=NAGENTS_SERVER_TOKEN="$token_file"
rm -- "$token_file"
```

The temporary file is outside the repository and is created with private
permissions. Provision the manifest's other API-key/registry secrets separately.
After rotating a Secret, restart the deployment so the process sees the new
token. The liveness/readiness probes remain unauthenticated at `/health`.

Non-local access requires TLS and a configured token, including behind a
loopback reverse proxy. The module runner deliberately ignores proxy headers.
For TLS termination, use an ASGI runner configured to trust only the actual
proxy IPs and preserve the original scheme and Host for same-origin checks.
Do not enable arbitrary forwarded-header trust. The example ingress, node
selection, storage, and Docker socket assumptions require deployment-specific
review; this guide does not configure those trust relationships for you.

!!! warning "Single trusted operator, not a sandbox"
    The shared token grants full API authority, not per-user/session isolation.
    File tools, custom Python, and MCP execute with their host privileges.
    Access to the host Docker socket can grant host-level control; a read-only
    socket mount does not make Docker API operations read-only. Do not expose
    this server to untrusted users or rely on prompts to enforce permissions.
