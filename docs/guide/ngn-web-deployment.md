# Private ngn Web Deployment

This reusable example is for an explicitly owner-approved, single-user deployment
of the React client in [ngn serve](ngn-web.md), not a new remote mode or a deployment
of the legacy `nagents.server`. It is a narrowly scoped exception to the local
client's normal "do not reverse proxy" guidance.

The standalone client binds **127.0.0.1** by default. This example opts into a
routable bind by passing an explicit `--host 0.0.0.0`, so the in-cluster Service
and Ingress reach the process directly with no same-pod proxy. Only a literal IP
is accepted for that opt-in; hostnames stay rejected.
**Access is password-free.** Tailscale ACLs/grants and trusted cluster networking
are the access boundary. Everyone allowed to reach the Service shares the same
trusted-user workspace, sessions, and per-call approvals. This is not multi-user
isolation or a sandbox.

The public template is [examples/k8s/ngn-web.yaml](https://github.com/abi-jey/nagents/blob/main/examples/k8s/ngn-web.yaml).
The deployment administrator is responsible for secret handling, reviewing the
deployment plan, deployment, and cutover. Passing a dry-run does not establish
live health. Review the rendered configuration and security assumptions before
changing live resources.

## Private Rendering

Keep actual tailnet domains, cluster contexts, node names, account details, and
rendered manifests **outside Git**. This guide and the template contain only public
examples. Do not replace their placeholders with your deployment's private values.
Store the private render at `~/.local/share/ngn/deployments/ngn-web/deployment.yaml`,
with a mode 0700 directory and mode 0600 file. Keep private inputs and secret files
out of repository files, terminal transcripts, and shared build/CI artifacts too.

The template has two required environment variables:

| Variable | Meaning / Public Example |
| --- | --- |
| `NGN_NODE` | The selected node's `kubernetes.io/hostname` label, `<your-node>` |
| `NGN_STATE_PATH` | A new, dedicated absolute directory on that node, e.g. `/var/lib/nagents/ngn-web` |

The `ts-serve` Ingress owns the hostname (its name determines it), so the template
has no host variable and no host allowlist. For a fresh deployment, confirm the
chosen Ingress name produces the private HTTPS hostname you expect.

After setting the two values privately, run this **Bash** rendering step from the
repository root with GNU `envsubst` installed:

```bash
(
  set -eu
  : "${NGN_NODE:?Set the node label privately}"
  : "${NGN_STATE_PATH:?Set a new dedicated state directory}"
  # Restrict inputs before inserting them into YAML.
  [[ "$NGN_NODE" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || exit 1
  [[ "$NGN_STATE_PATH" =~ ^/[a-zA-Z0-9/_-]+$ ]] || exit 1
  export NGN_NODE NGN_STATE_PATH
  umask 077
  deployment_dir="$HOME/.local/share/ngn/deployments/ngn-web"
  install -d -m 0700 "$deployment_dir"
  envsubst '${NGN_NODE} ${NGN_STATE_PATH}' \
    < examples/k8s/ngn-web.yaml > "$deployment_dir/deployment.yaml"
  chmod 0600 "$deployment_dir/deployment.yaml"
)
```

The input checks reject syntax injection, not unsafe storage choices: the administrator
must still verify a valid node label and a new narrow directory, never `/`,
`/home`, `/var`, a checkout, or any existing unrelated data. Always pass the explicit
`${NGN_NODE} ${NGN_STATE_PATH}` list to `envsubst`: the container's startup script
uses shell variables such as `$directory` and `$credential` that must remain
untouched, and a bare `envsubst` would corrupt them. The unrendered template is
**not** an apply-ready manifest. For local tests use only dummy values and a private
temporary directory outside the repository, never a generated file inside it.

## Topology And Boundary

```text
Tailnet browser, HTTPS, access governed by Tailscale ACLs/grants
  -> custom ts-serve Ingress ngn-web (TLS terminates here)
  -> ClusterIP Service ngn-web:8080
  -> same-pod ngn serve bound 0.0.0.0:8765 (single container)
```

- The example new URL is **https://ngn-web.your-tailnet.ts.net**. No Funnel, public Internet
  listener, NodePort, LoadBalancer, or remote bind is configured beyond the explicit
  in-cluster `--host 0.0.0.0`.
- This example requires a custom `ts-serve` operator using `spec.ingressClassName: ts-serve`
  and `spec.defaultBackend.service` name/port, not official Tailscale CRDs or
  annotations. The Ingress name determines the hostname. The operator owns a
  separate `ngn-web-ts` Deployment whose proxy uses host networking; do not manage
  that generated Deployment in this manifest. Confirm your cluster has this
  operator and that the generated hostname matches your expectations; the template
  does not install the operator and is not an official-operator manifest.
- There is no in-pod reverse proxy and no Host/Origin allowlist. Because the bind is
  off loopback, the exact local-authority Host and Origin checks are disabled (there
  is no single trusted authority to compare against). The trust boundary is the
  tailnet plus trusted cluster networking, not a header allowlist. ClusterIP is not
  public Internet exposure, but clients with cluster connectivity can also reach the
  Service, obtain the bootstrap token, and call the API. These checks protect
  browsers, not against malicious authorized network clients. Tailscale ACLs/grants
  control the tailnet entry point; they do not restrict this direct cluster path.
- The application still enforces: a random per-process `X-Ngn-Token` on API reads
  and mutations (except the token-free same-origin `GET /api/bootstrap`),
  `Sec-Fetch-Site` same-origin checks, query-parameter rejection, path-traversal
  rejection, security headers, and a 64 KiB request-body cap. The dictation route
  has its own format, deadline, and byte limits. Access logging stays off because
  URLs may contain accidental secrets.
- Only `/api/events` accepts a WebSocket upgrade. `Sec-WebSocket-Protocol` passes
  through unchanged; ngn validates the process token before accepting a subscription.
- TLS and tailnet admission are the operator's responsibility. Use the tailnet
  HTTPS URL; the hop from ts-serve to the pod is cluster HTTP, not end-to-end TLS.

**Live validation gate:** confirm the custom ts-serve proxy preserves the external
Host, `Sec-Fetch-Site`, and `X-Ngn-Token`, and streams NDJSON without buffering and
supports the `/api/events` WebSocket upgrade. The Ingress does not itself prove TLS,
tailnet-only exposure, or header preservation.

## State And Credentials

The pre-bound, `Retain` hostPath PV `ngn-web-local` uses only `NGN_STATE_PATH` on
`NGN_NODE`. PVC `nagents/ngn-web-state` explicitly selects it with an empty storage
class, avoiding dynamic provisioning, particularly NFS and its poor fit for
SQLite. The declared **5 GiB is a binding size, not a host
filesystem quota**. Monitor free space and arrange protected, consistent backups;
this is not replicated storage. Losing the selected node makes this pod unavailable. Moving
nodes or reusing a released PV requires a separately reviewed recovery plan.

The PVC contains an initially empty workspace at `/state/workspace`, SQLite
session data under `/state/sessions`, private home/config directories, and the
renewable OpenAIAuth store at `/state/data/ngn/auth/openai.json`. It is sensitive
even after the seed Secret is deleted. Deleting the workload/PVC does not erase
the retained PV or node data. Never automatically delete or clear either during
cleanup. Do not mount NFS, the current checkout, a Docker socket, or broad host
directories into this workload.

The template also creates `/state/channel-plugins` and sets
`NGN_CHANNEL_PLUGIN_PATH` to it. Python packages installed there through the
application's approved shell are retained by the PVC and become discoverable with
**Channels → Refresh**. The image's Python/pip installation remains on its
read-only root filesystem; package installation targets the writable plugin
directory explicitly. Channel configuration, secret values, and chat/session
bindings are private application state and belong in protected backups with the
session database. Do not put bot tokens in the public manifest or shell prompts.

Web input and connector notifications use a durable queue. Check queued channel
work as well as active runs before maintenance. A browser disconnect does not
stop that work. Failed/interrupted execution is not automatically replayed after
restart because an external action may already have occurred. This differs from
the process-local scheduled wakeups described below.

There is no separate initializer. The main `ngn` container runs as **root** and, on
startup, creates the fixed `/state/...` directories with mode 0700, installs the
one-time projected seed credential as a regular 0600 file **only if the
destination is absent**, then `exec`s `ngn serve`. The auth directory and store are
kept at 0700/0600; there is deliberately no `fsGroup` that could relax them. A
missing seed and missing destination fail clearly at startup. Existing refreshed
contents are never overwritten by the seed on restart; the seed volume is optional
to allow its later deletion. Running as root is what lets the container own and
mode the hostPath without a privileged init container; keep the container's other
hardening (read-only root filesystem, dropped capabilities, no privilege
escalation, no service-account token, a dedicated `/tmp` `emptyDir`).

`replicas: 1` plus `Recreate` avoids overlapping owners in ordinary rollouts;
ReadWriteOnce is not a single-process lock. Do not autoscale, force-delete/recreate
a running owner, or attach a second application to this PVC.

The web Harness's `schedule_wakeup` timers are **process-local**, unlike saved
conversations, settings, and credentials. A pod restart or image rollout cancels
pending timers and drops retained child continuation handles. Check for active
runs and pending wakeups before maintenance; do not describe a successful PVC
restart test as proof that timers survived. These timers can execute without an
open browser while the server remains running, but are not durable cron jobs.

Config is secret-free: `provider = "openai"`, `auth = "chatgpt"`, `api = "auto"`,
`data_dir = "/state/sessions"`. Omitting `model` lets the Harness replace its normal
default with its Codex default. Chat credentials and routing stay separate from
the optional transcription Secret described below. The pinned amd64 application image includes the React assets and
Codex client; the container `command` selects `serve` with the exact
host/port/workspace/config flags. New images default to `ngn serve` without
development mode. The image is pinned by digest.

The web runtime settings API stores its versioned `ngn_web_settings` row in the
existing workspace SQLite database under `/state/sessions` on this same PVC.
Model, profile, dictation preferences, and bounded tool/runtime limits therefore survive pod/image
replacement while the PVC and resolved workspace path remain unchanged. No writable
ConfigMap mount, new volume, credential copy, or image-layer write is needed.
Startup captures trusted configuration defaults after initial Codex model resolution,
then applies the saved override. Reset deletes the override so future trusted
defaults take effect. Provider/API routing, credentials, plugins, trust, and storage
paths remain administrator-controlled and cannot be edited through this API.
Malformed/unsupported saved settings or a removed saved profile fail web startup
closed. Stop the application and have the administrator repair or remove only the
settings row, never delete the session database or auth store as a workaround.
See [runtime settings](ngn-web.md#runtime-settings) for the API and conflict rules.

### Transcription Credentials

The browser microphone supplies text input through a separately authenticated
transcription request. The template permits dictation with
`dictation_enabled = true` and selects `NGN_TRANSCRIPTION_API_KEY` as its API-key
environment variable. It references only the `api-key` entry in the optional
`ngn-web-transcription` Secret. With no Secret/key, the web app still starts and
Settings reports that transcription is unavailable. Enabling the service never
starts recording automatically.

To use your own OpenAI Platform key, create the runtime Secret from a private file
containing only the key, without a trailing newline:

```bash
kubectl --context <your-context> -n nagents create secret generic ngn-web-transcription \
  --from-file=api-key=/private/path/openai-api-key
```

If an appropriate Secret already exists, update the `secretKeyRef` name/key in
your **private rendered manifest** for `NGN_TRANSCRIPTION_API_KEY` instead. The
application needs only that entry; nothing else in the pod receives it.
Do not put the key in this template, ConfigMap, Docker build arguments, image,
frontend bundle, or public documentation. Environment-variable credentials are
read at process startup, so adding or rotating this Secret requires a safe pod
restart after checking active runs and pending wakeups.

Chat can remain on its existing Codex login. The transcription client does not
forward that OAuth credential to the file-transcription API or fall back to
another provider's key. Its default model is `gpt-4o-mini-transcribe`, with language
auto-detection and a 120-second administrator ceiling. Existing trusted YAML,
`NGN_DICTATION_*`, and CLI configuration select backend defaults; YAML/CLI values
take precedence over environment defaults when explicitly supplied.

Settings exposes the enabled preference, model, language, and recording limit,
which persist in the workspace settings row. The administrator's disabled flag,
maximum duration, endpoint, and key reference remain authoritative. The browser
records locally and uploads bounded 16 kHz mono PCM16 WAV. The application enforces
its own format, deadline, and byte limits for that route; there is no proxy ceiling
to configure. It does not require microphone hardware or audio-device
mounts in the pod. Audio and unsent transcripts are not persisted as workspace
files.

## Operator Preflight

1. Review the public template, its private render, and the live plan. Select
   `<your-context>` and confirm the namespace `nagents` exists. Confirm
   `ngn-web-local`, `ngn-web-state`, and the new workload/config/ingress names are
   unused or belong to this exact reviewed deployment. Inspect metadata only for
   existing Secrets, never their data. Verify `NGN_STATE_PATH` on the selected
   node, not on the machine running the CLI: a local `stat` does not verify a
   remote hostPath. Prefer a new empty directory for first initialization.
   Confirm `<your-node>` has
   capacity and Linux amd64 labels. If the
   namespace or registry pull Secret is absent, provision it separately.
2. For migration, preserve the current `nagents` Deployment, Service, Ingress, and running pod.
   Its `emptyDir` data is lost on pod replacement. Do not apply the old example,
   restart, scale, delete, or prune those resources while preparing the new UI.
3. Use the administrator-managed `nagents/ghcr-pull-secret`. No registry credential
   is included in the template.
4. Stop use of the local TUI/other clients with the authorized source credential
   before copying the **ngn OpenAIAuth stored JSON**, not another client's
   auth format, into `nagents/ngn-web-codex-seed`, key `openai.json`. Do not inspect
   or print the JSON. Use file-based secret creation outside Git, without a
   generated Secret YAML in the repository. Refresh-token rotation makes the
   copied credential a transfer of ownership, not safe simultaneous local/web
   login sharing. Do not restart the old copy; a return to local use requires a
   reviewed transfer of the latest store or a fresh login.
5. Review Tailscale ACLs/grants and which cluster clients can reach the Service;
   all must be trusted with the shared workspace and approvals. Verify private
   HTTPS (no Funnel), header forwarding, streaming, and node storage/backups.
   The one-time seed is administrator-managed and intentionally absent from the
   manifest. Kubernetes Secret storage/access must be protected by the cluster.

Non-persisting API validation, allowed before live approval, uses the **private
render**, not `examples/k8s/ngn-web.yaml`. Replace the context placeholder privately:

```bash
kubectl --context '<your-context>' get pv ngn-web-local --ignore-not-found
kubectl --context '<your-context>' -n nagents get pvc ngn-web-state --ignore-not-found
kubectl --context '<your-context>' apply --dry-run=server \
  -f "$HOME/.local/share/ngn/deployments/ngn-web/deployment.yaml"
```

A dry-run does not create the node directory, bind storage, check image contents,
read/import credentials, run containers, or exercise the Tailscale proxy. Local
proxy/container tests must use dummy data only, no actual model credentials, no host
Docker socket inside a container, and no private-image pull. Run repository checks
in the Python virtual environment, including `.venv/bin/pre-commit run --all-files`; new
untracked files also need explicit `--files` checks before they are staged.

## Validation And Cutover

Only after administrator review and secret preparation, apply the private rendered
`deployment.yaml`, without pruning unrelated resources. It adds only the
PV, PVC, ConfigMap, Deployment, Service, and **new** Ingress. Validate in order:

1. The PVC binds to the dedicated PV on `NGN_NODE`; only the new pod starts. The
   single `ngn` container becomes ready, bound to `0.0.0.0:8765`. Inspect startup
   output for the state-directory and seed result; no Secret dumps, bootstrap-body
   dumps, or credential content in logs/review output. Check file types, ownership,
   and modes without reading the credential.
2. From an allowed tailnet browser, the new HTTPS URL presents a valid certificate
   and opens without a password prompt. Without an `Authorization` header,
   `GET /`, known built assets, and `GET /api/bootstrap` return 200 with no
   `WWW-Authenticate` header. Do not print the bootstrap token.
3. API reads and mutations require a valid `X-Ngn-Token`; requests with a
   missing/invalid token, query parameters, oversized bodies, or
   `Sec-Fetch-Site: cross-site` fail, while `GET /api/bootstrap` remains the
   token-free exception. Because the bind is off loopback, exact Host/Origin
   equality is not enforced; do not rely on it as an access control.
   `/readyz` alone is not evidence that the app works.
4. Validate React assets, bootstrap, session creation/resume, incremental NDJSON,
   cancellation, and denied/allowed per-call approvals. Run one user-approved
   small live Codex request to validate actual auth/import and refresh behavior;
   readiness alone does not prove provider credentials or a model request works.
   Avoid unnecessary workspace mutations, and never replay a request blindly.
5. Once import is validated, delete only the one-time seed Secret. At an
   approved idle window, recreate only the new pod to verify retained sessions,
   workspace, and the renewable auth store work with no seed. Do not restart the
   legacy pod. If this fails, investigate the retained store rather than blindly
   replacing refreshed contents with the original copy.
6. For migration only, after all gates pass, separately repoint the existing
   `nagents` Ingress backend to `ngn-web:8080`. This mutation is **not included in
   the manifest**. Repeat HTTPS/password-free access/token/stream validation
   there. Retire the old UI exposure, but preserve the old Deployment, Service,
   API pod, and its data. Both hostnames expose the same single-user Harness.

If startup rejects an unsafe state path, stop and verify the
selected node, PV path, and directory provenance with the administrator. Inspect
metadata without reading credentials. Do not delete state or fabricate state
markers to bypass a guard. Preserve recognized partial state and resolve the
underlying startup failure before retrying. Recovery of unrecognized
existing data requires a separately reviewed plan.

Before cutover, failure leaves the old endpoint untouched; stop and fix only the
new deployment. After cutover, any decision to restore the old Ingress backend
requires administrator review because that re-exposes the old UI. Never roll back by
deleting legacy pods or retained state. ConfigMap edits require a controlled
restart of only `ngn-web`. Codex credential renewal remains owned by the app, not by
reapplying the original seed.
