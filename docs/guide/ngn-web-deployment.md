# Private ngn Web Deployment

This reusable example is for an explicitly owner-approved, single-user deployment
of the React client in [ngn serve](ngn-web.md), not a new remote mode or a deployment
of the legacy `nagents.server`. It is a narrowly scoped exception to the local
client's normal "do not reverse proxy" guidance. The unchanged LocalOnly backend
still binds **127.0.0.1:8765** behind a same-pod nginx sidecar.
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

The template has four required environment variables:

| Variable | Meaning / Public Example |
| --- | --- |
| `NGN_WEB_HOST` | Primary hostname only, e.g. `ngn-web.your-tailnet.ts.net`; no scheme, port, slash, or trailing dot |
| `NGN_LEGACY_HOST` | Existing UI migration alias, e.g. `nagents.your-tailnet.ts.net`; otherwise set equal to `NGN_WEB_HOST` |
| `NGN_NODE` | The selected node's `kubernetes.io/hostname` label, `<your-node>` |
| `NGN_STATE_PATH` | A new, dedicated absolute directory on that node, e.g. `/var/lib/nagents/ngn-web` |

For migration, set `NGN_LEGACY_HOST` to the existing Ingress's hostname, but do not
change that Ingress until validation passes. For a fresh deployment, set
`NGN_LEGACY_HOST="$NGN_WEB_HOST"` and omit the legacy preservation/cutover steps;
do not invent an extra hostname or leave the variable empty. Each nginx map uses
one regex, so equal host values produce no duplicate map keys or server names and
allow only the primary host. Removing a migration alias later uses this same
setting plus a reviewed restart of only the new workload, not deletion of state.

After setting the four values privately, run this **Bash**
rendering step from the repository root with GNU `envsubst` installed:

```bash
(
  set -eu
  : "${NGN_WEB_HOST:?Set the primary hostname privately}"
  : "${NGN_LEGACY_HOST:?Set the alias or the same primary hostname}"
  : "${NGN_NODE:?Set the node label privately}"
  : "${NGN_STATE_PATH:?Set a new dedicated state directory}"
  # Restrict inputs before inserting them into YAML and nginx syntax.
  for host in "$NGN_WEB_HOST" "$NGN_LEGACY_HOST"; do
    [[ "$host" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || exit 1
  done
  [[ "$NGN_NODE" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || exit 1
  [[ "$NGN_STATE_PATH" =~ ^/[a-zA-Z0-9/_-]+$ ]] || exit 1
  export NGN_WEB_HOST NGN_LEGACY_HOST NGN_NODE NGN_STATE_PATH
  umask 077
  deployment_dir="$HOME/.local/share/ngn/deployments/ngn-web"
  install -d -m 0700 "$deployment_dir"
  envsubst '${NGN_WEB_HOST} ${NGN_LEGACY_HOST} ${NGN_NODE} ${NGN_STATE_PATH}' \
    < examples/k8s/ngn-web.yaml > "$deployment_dir/deployment.yaml"
  chmod 0600 "$deployment_dir/deployment.yaml"
)
```

The input checks reject syntax injection, not unsafe storage choices: the administrator
must still verify valid DNS/node labels and a new narrow directory, never `/`,
`/home`, `/var`, a checkout, or any existing unrelated data. Host patterns use
case-sensitive, anchored PCRE `\Q...\E` literal quoting, so dots cannot become
wildcards. Never use bare `envsubst` or expand the allowlist: nginx's `$http_host`,
`$http_origin`, other runtime variables, and the init script's shell variables
must remain untouched. The unrendered template is **not** an apply-ready manifest.
For local tests use only dummy values and a private temporary directory outside
the repository, never a generated file inside it.

## Topology And Boundary

```text
Tailnet browser, HTTPS, access governed by Tailscale ACLs/grants
  -> custom ts-serve Ingress ngn-web (TLS terminates here)
  -> ClusterIP Service ngn-web:8080
  -> nginx sidecar: exact external Host/HTTPS Origin checks, no login prompt
  -> same-pod HTTP 127.0.0.1:8765: unchanged ngn serve / LocalOnly
```

- The example new URL is **https://ngn-web.your-tailnet.ts.net**. No Funnel, public Internet
  listener, NodePort, LoadBalancer, or remote ngn binding is configured.
- This example requires a custom `ts-serve` operator using `spec.ingressClassName: ts-serve`
  and `spec.defaultBackend.service` name/port, not official Tailscale CRDs or
  annotations. The Ingress name determines the hostname. The operator owns a
  separate `ngn-web-ts` Deployment whose proxy uses host networking; do not manage
  that generated Deployment in this manifest. Confirm your cluster has this
  operator and that `NGN_WEB_HOST` matches its generated hostname; the template
  does not install the operator and is not an official-operator manifest.
- nginx accepts only the exact raw Hosts `NGN_WEB_HOST` and `NGN_LEGACY_HOST`.
  When supplied, Origin must be the exact `https://`
  origin for that same Host. The two hosts are not interchangeable origins.
  Foreign hosts, `null`, HTTP origins, alternate ports, suffixes, and mismatched
  host/origin pairs fail before rewriting. Do not broaden the allowlist to make a
  failing ingress work.
- After validation, nginx rewrites upstream Host to `127.0.0.1:8765` and a nonempty
  accepted Origin to `http://127.0.0.1:8765`. An absent Origin stays absent; the
  backend still requires it for POST. `Sec-Fetch-Site` and `X-Ngn-Token` pass
  unchanged. Incoming `Authorization` and `Proxy-Authorization` headers are
  stripped before forwarding to ngn.
- UI, assets, and `GET /api/bootstrap` are password-free. Other API endpoints still
  require the app's per-process `X-Ngn-Token` CSRF token, not a user login.
  `/readyz` is a constant response with no application data. Query parameters are
  rejected, never used for authentication. Both containers must be ready:
  nginx probes use an allowed Host, and the app's exec probe discards bootstrap
  output instead of exposing its per-process token.
- TLS and tailnet admission are the operator's responsibility. Use the tailnet
  HTTPS URL; the hop from ts-serve to nginx is cluster HTTP, not end-to-end TLS.
  ClusterIP is not public Internet exposure, but clients with cluster connectivity
  can also reach the Service, forge allowed Host/Origin/fetch-metadata headers,
  and obtain the bootstrap token. These checks protect browsers, not against
  malicious authorized network clients. Tailscale ACLs/grants control the tailnet
  entry point; they do not restrict this direct cluster path.
- nginx disables access and request-error logging because URLs may contain
  accidental secrets; ngn already disables access logging. NDJSON buffering,
  caching, and upstream retries are off, timeouts are one hour, and request bodies
  are limited to 64 KiB. Do not enable request/debug logs to troubleshoot auth.

**Live validation gate:** confirm the custom ts-serve proxy preserves the external
Host and Origin, as well as `Sec-Fetch-Site` and `X-Ngn-Token`, and
streams NDJSON without buffering. A proxy that overwrites Host/Origin cannot be
made safe by simply trusting forwarded headers or disabling LocalOnly checks.
Resolve that incompatibility with the cluster administrator before any cutover. The Ingress does
not itself prove TLS, tailnet-only exposure, or header preservation.

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

Initialization uses `/state/.ngn-web-state` to recognize this deployment's state.
Without that marker, `/state` must be empty; an unrecognized nonempty directory
is rejected before any ownership changes. The init container creates the marker
before other initialization so an interrupted first startup can retry. The marker
is a regular, single-link file owned by uid/gid 1000 with mode 0600; symlinks and
hard links are rejected. Keeping it permits retries and restarts while preserving
the existing private credential store. It is an initialization record, not a
substitute for verifying the selected node and dedicated directory.

The init container runs as root only to initialize fixed directories and private
ownership, with all capabilities dropped except `CHOWN`, `DAC_OVERRIDE`, and
`FOWNER`. It rejects symlinked state directories/destinations and hard-linked
credentials. `/state` and every created directory are uid/gid 1000, mode 0700;
the auth file is mode 0600. There is deliberately no `fsGroup` that could relax
these permissions. The one-time projected Secret is copied to a regular file
atomically **only if the destination is absent**. A missing seed and missing
destination fail clearly. Existing refreshed contents are never overwritten by
the seed on restart; the seed volume is optional to allow its later deletion.

The application is uid/gid 1000, nginx uid/gid 101; both are non-root with read-only
root filesystems, no added capabilities, no privilege escalation, and separate
temporary `emptyDir` volumes. No service-account token is mounted. nginx mounts its
config and temporary volume, **never the state PVC or Codex credential**. The
application's tools still run with its own PVC/network access and are not a
hostile-code sandbox. `replicas: 1` plus `Recreate` avoids overlapping owners in
ordinary rollouts; ReadWriteOnce is not a single-process lock. Do not autoscale,
force-delete/recreate a running owner, or attach a second application to this PVC.

Config is secret-free: `provider = "openai"`, `auth = "chatgpt"`, `api = "auto"`,
`data_dir = "/state/sessions"`. Omitting `model` lets the Harness replace its normal
default with its Codex default. There is no API-key/endpoint injection or legacy
Secret reuse. The pinned amd64 application image includes the React assets and
Codex client; `command: [ngn]` overrides its legacy default CMD with `serve` and the
exact loopback/workspace/config flags. Both images are pinned by digest.

## Operator Preflight

1. Review the public template, its private render, and the live plan. Select
   `<your-context>` and confirm the namespace `nagents` exists. Confirm
   `ngn-web-local`, `ngn-web-state`, and the new workload/config/ingress names are
   unused or belong to this exact reviewed deployment. Inspect metadata only for
   existing Secrets, never their data. Verify `NGN_STATE_PATH` on the selected
   node, not on the machine running the CLI: a local `stat` does not verify a
   remote hostPath. First initialization requires an empty directory; existing
   deployment state must retain its marker. The runtime guard rejects unrecognized
   nonempty directories rather than adopting them. Confirm `<your-node>` has
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
proxy/init tests must use dummy data only, no actual model credentials, no host
Docker socket inside a container, and no private-image pull. Run repository checks
in the Python virtual environment, including `.venv/bin/pre-commit run --all-files`; new
untracked files also need explicit `--files` checks before they are staged.

## Validation And Cutover

Only after administrator review and secret preparation, apply the private rendered
`deployment.yaml`, without pruning unrelated resources. It adds only the
PV, PVC, ConfigMap, Deployment, Service, and **new** Ingress. Validate in order:

1. The PVC binds to the dedicated PV on `NGN_NODE`; only the new pod starts. Both
   containers become ready, with ngn listening on loopback only. Use `nginx -t`
   for config diagnostics; no `nginx -T`, Secret dumps, bootstrap-body dumps, or
   credential content in logs/review output. Check file types, ownership, and
   modes without reading the credential.
2. From an allowed tailnet browser, the new HTTPS URL presents a valid certificate
   and opens without a password prompt. Without an `Authorization` header,
   `GET /`, known built assets, and `GET /api/bootstrap` return 200 with no
   `WWW-Authenticate` header. Do not print the bootstrap token. Confirm that
   incoming `Authorization` headers are stripped before reaching the backend.
3. Requests with the same host's exact HTTPS Origin work. Foreign or
   `null` origins, HTTP origins, mismatched allowed-host pairs, unknown Hosts,
   `Sec-Fetch-Site: cross-site`, query parameters, POST without Origin, oversized
   bodies, and API requests with missing/invalid `X-Ngn-Token` fail, including POST;
   bootstrap remains the token-free exception. Verify both configured
   host/origin pairs in isolated proxy tests; do not repoint the old Ingress just
   to test its hostname. `/readyz` alone is not evidence that the app works.
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
   the manifest**. The new nginx already accepts
   `https://<NGN_LEGACY_HOST>`; repeat HTTPS/password-free access/origin/stream validation
   there. Retire the old UI exposure, but preserve the old Deployment, Service,
   API pod, and its data. Both hostnames expose the same single-user Harness.

If initialization rejects the state directory or marker, stop and verify the
selected node, PV path, and directory provenance with the administrator. Inspect
metadata without reading credentials. Do not delete state or the marker, or
fabricate a marker to bypass the guard. Preserve recognized partial state and
resolve the underlying startup failure before retrying. Recovery of unrecognized
existing data requires a separately reviewed plan.

Before cutover, failure leaves the old endpoint untouched; stop and fix only the
new deployment. After cutover, any decision to restore the old Ingress backend
requires administrator review because that re-exposes the old UI. Never roll back by
deleting legacy pods or retained state. ConfigMap edits require a controlled
restart of only `ngn-web` because nginx uses a subPath config mount and does not
reload automatically. Codex credential renewal remains owned by the app, not by
reapplying the original seed.
