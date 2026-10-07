# ngn container: configuration and authentication

The runtime image in `dockerfiles/Runtime.Dockerfile` starts `ngn serve --host
0.0.0.0` as root inside the container, in `/home/agent/workspace`. It serves the web UI **with
no configuration file or API key**. Starting and passing `/api/bootstrap`
does not verify provider access: live chat still needs an account, a supported
model and credentials. With no credential source, the UI shows setup guidance
and a chat attempt returns a specific, safe error instead of claiming success.
Use an authenticated, trusted network boundary around the container; it is a
single-user workspace, not a multi-user service.

## Root access inside the container

Approved shell tools run with the same container identity as ngn. The runtime
image and web Kubernetes examples use UID/GID 0 and a writable container root
filesystem, so commands such as `apt-get update` and `apt-get install` can install
system dependencies. Tool approval still applies: becoming root does not grant
automatic approval to run a tool.

The Kubernetes examples drop all capabilities, then add `CHOWN`, `DAC_OVERRIDE`,
`FOWNER`, `SETUID`, and `SETGID` for package ownership and the package manager's
unprivileged download user. They retain the runtime's default seccomp profile,
disable privilege escalation and service-account-token mounting, and do not
mount a Docker socket or host system directories. No `privileged: true`,
`SYS_ADMIN`, host networking, or host process namespace is needed.

For the same configuration with Docker, use a published image digest:

```sh
docker run --rm --user 0:0 --cap-drop ALL \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
  --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges \
  -p 127.0.0.1:8765:8765 \
  ghcr.io/abi-jey/nagents/agent-server@sha256:<reviewed-digest>
```

Package installations change the container's writable layer. They disappear
when the container is replaced; put permanent system dependencies in a derived
image. Keep sessions, credentials, and workspace files on the configured data
volume. The retained `agent` account also supports an explicitly non-root
deployment with Docker `--user 1000:1000 --env HOME=/home/agent`, or equivalent
Kubernetes settings and writable volumes owned by that user.

When changing an existing deployment from UID 1000 to UID 0, first stop ngn at
an idle maintenance window and back up its persistent state. Preserve its
`HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and workspace/data paths, and migrate
ownership of only its dedicated private state to the new UID/GID. Update any
initializer that sets ownership too. Keep authentication directories at 0700
and credential files at 0600: the credential store checks ownership against
the running UID and will reject a store still owned by the previous user.
Changing only the image's `USER` does not override a pod's `runAsNonRoot`,
`runAsUser`, or read-only root filesystem settings.

## Two ways to configure an API-key connection

For a mounted file, use **secret-free** JSON. A complete Kubernetes
ConfigMap + Deployment + ClusterIP Service is in
[`examples/k8s/ngn-container-config.yaml`](https://github.com/abi-jey/nagents/blob/main/examples/k8s/ngn-container-config.yaml).
The deployment selects the projected file explicitly:

```text
ngn serve --host 0.0.0.0 --workspace /workspace --config /etc/ngn/config.json
```

The ConfigMap's `providers.json` selects an OpenAI API-key connection and
`config.json` chooses `gpt-6-luna`. The Secret reference sets that **environment
variable in the ngn container**; a browser's environment cannot supply it.
Replace the example image tag with a digest of an image built from the updated
Dockerfile. Change the model to one your account can use; setting a model name
does not grant model access.

For **UI-first** setup, omit `--config` and the `config` volume/mount. The web
client starts without a selected provider; add and activate a named connection
in Provider connections before chatting. Supply its key variable to the container:

```yaml
env:
  - name: NGN_MODEL
    value: gpt-6-luna
  - name: OPENAI_API_KEY
    valueFrom:
      secretKeyRef:
        name: ngn-provider-example
        key: OPENAI_API_KEY
        optional: true
```

Keep the example's `XDG_DATA_HOME`, `XDG_CONFIG_HOME` and writable `/data` and
`/workspace` volumes. A zero-config container shows setup guidance until a
named connection is selected. Enter the **variable name** `OPENAI_API_KEY` in
that connection; never enter its value in configuration. An explicit
`--config` must point to an existing readable file. Environment defaults have
lower precedence than JSON; explicit CLI options have higher precedence. ngn
does not interpret `NGN_CONFIG` or automatically read `.env` files.

## Provision and verify

1. Build/publish the updated image and use its digest in your private render of
   the sample. Provision a Secret **from a private file**, for example
   `kubectl -n <namespace> create secret generic ngn-provider-example
   --from-file=OPENAI_API_KEY=/private/path/openai-api-key` (key only, with no
   trailing newline). Do not place the
    key in a ConfigMap, JSON, image, command argument or logs. The Secret is
   optional only to allow UI-first startup; a missing key cannot run inference.
2. Deploy the ConfigMap example, or the env-only variant, in your namespace.
   Provide a persistent writable data volume when you want settings, sessions
   and credentials to survive replacement; the example's `emptyDir` volumes
   are deliberately ephemeral. Use a single replica with `Recreate` for a
   persistent SQLite database. Grant access to the Service only through your
   approved ingress/proxy. Kubernetes ConfigMap projected files use symlinks
   under `/etc/ngn/..20.../`; a diagnostic naming that resolved path means the
   file **was read**, not that the path is invalid.
3. Open the UI, confirm the selected connection and model in Settings, then
   make one live request. `/api/bootstrap` and a ready pod prove only local UI
   startup. Rotate a Secret with a controlled container restart so its env
   value is refreshed. A saved named provider connection can override flat
   defaults; select the intended connection in Settings if one already exists
   on the persistent data volume.

### ChatGPT/Codex subscription route

`auth: chatgpt` uses the **ngn-owned** renewable login at
`$XDG_DATA_HOME/ngn/auth/openai.json`, not `OPENAI_API_KEY`. If you use this
route, run `ngn login chatgpt` with the **same persistent** `XDG_DATA_HOME` as
the serving container (with only one process refreshing the login), or transfer
an existing ngn OpenAIAuth store as a regular private file owned by the
container user, with private directory/file permissions (0700/0600). It is not
a generic Codex `~/.codex/auth.json` file. `auth: codex` is a separate local
Codex discovery mode and needs explicit `CODEX_HOME`/`~/.codex` files *inside*
the container. A host's login is never implicitly available in a pod.
`auth: auto` may choose a saved ngn/Codex login before an OpenAI API key;
choose `auth: api-key` explicitly when the Secret should power chat. GPT-Live
uses separate model/voice settings and credentials.

An on-disk login status of “saved” only confirms that a file can be parsed;
tokens can be expired/revoked, renewal can fail, and the selected model can be
unavailable. The web error now distinguishes known authentication, permission,
quota and connection failures using bounded internal error codes. Unknown
upstream error bodies and credential contents are not returned or logged by
the web error mapping. A model-catalog failure is also **not** proof of a
specific token/entitlement issue. Check the server's safe error category and
the configured auth route before changing credentials or models.
