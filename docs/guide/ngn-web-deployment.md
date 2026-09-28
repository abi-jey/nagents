# Private ngn Web Deployment

This is a cluster-specific, single-trusted-operator example for the React client
in [ngn serve](ngn-web.md), not the [legacy API server](server.md). The public
template is [`examples/k8s/ngn-web.yaml`](../../examples/k8s/ngn-web.yaml).
It creates a separate `ngn-web` Deployment, Service, custom `ts-serve` Ingress,
ConfigMap, and retained node-local PV/PVC; it does not replace the legacy
`nagents` workload or its Ingress. Review the actual cluster and private render
before applying. A server-side dry-run does not establish live health.

The application binds **127.0.0.1:8765 inside the pod**. A same-pod nginx
container accepts Service traffic on 8080, checks the external Host and HTTPS
Origin, then forwards to the loopback application using its expected local Host
and Origin. **Access is password-free**: tailnet ACLs/grants and trusted cluster
networking are the access boundary. All clients allowed to reach the Service
share one workspace, sessions and per-call approvals, without user isolation.

## Private rendering

Keep actual node names, domains, contexts, credentials and rendered manifests
outside Git and shared logs. The template is **not apply-ready**: obtain reviewed
amd64 digests for an app image built from this source revision (including the
React bundle) and for `nginxinc/nginx-unprivileged`. The template never uses an
unreviewed `:latest` or an older revision's digest.

| Input | Meaning |
| --- | --- |
| `NGN_NODE` | Selected node's `kubernetes.io/hostname` label |
| `NGN_STATE_PATH` | New, dedicated absolute directory **on that node**, e.g. `/var/lib/nagents/ngn-web` |
| `NGN_HOST` | Private HTTPS hostname assigned to the `ngn-web` Ingress by the custom operator (lowercase, no scheme or port) |
| `NGN_APP_DIGEST` | Reviewed 64-character SHA-256 hex digest of the app image |
| `NGN_NGINX_DIGEST` | Reviewed 64-character SHA-256 hex digest of the unprivileged nginx image |

With GNU `envsubst` installed, set these five values **privately** and run from
the repository root. The destination is outside the checkout and is never
overwritten by this command:

```bash
(
  set -eu
  : "${NGN_NODE:?Set a private node label}"
  : "${NGN_STATE_PATH:?Set a dedicated node directory}"
  : "${NGN_HOST:?Set the operator-assigned HTTPS hostname}"
  : "${NGN_APP_DIGEST:?Supply a reviewed app digest}"
  : "${NGN_NGINX_DIGEST:?Supply a reviewed nginx digest}"
  [[ "$NGN_NODE" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || exit 1
  [[ "$NGN_STATE_PATH" =~ ^/[a-zA-Z0-9/_-]+$ ]] || exit 1
  [[ "$NGN_HOST" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || exit 1
  [[ "$NGN_APP_DIGEST" =~ ^[0-9a-f]{64}$ ]] || exit 1
  [[ "$NGN_NGINX_DIGEST" =~ ^[0-9a-f]{64}$ ]] || exit 1
  export NGN_NODE NGN_STATE_PATH NGN_HOST NGN_APP_DIGEST NGN_NGINX_DIGEST
  umask 077
  deployment_dir="$HOME/.local/share/ngn/deployments/ngn-web"
  install -d -m 0700 "$deployment_dir"
  if [ -e "$deployment_dir/deployment.yaml" ] || [ -L "$deployment_dir/deployment.yaml" ]; then
    printf '%s\n' 'Private deployment exists; compare before replacing it.' >&2
    exit 1
  fi
  envsubst '${NGN_NODE} ${NGN_STATE_PATH} ${NGN_HOST} ${NGN_APP_DIGEST} ${NGN_NGINX_DIGEST}' \
    < examples/k8s/ngn-web.yaml > "$deployment_dir/deployment.yaml"
  chmod 0600 "$deployment_dir/deployment.yaml"
)
```

Only substitute the listed variables: bare `envsubst` also expands nginx's
`$http_host` and the init container's shell variables. These checks reject
syntax injection, not unsafe node/storage choices. Verify the label and a new,
narrow state path; never choose `/`, `/home`, `/var`, a checkout or existing
unrelated data. Compare any new render with previously deployed resources.

## Topology and boundary

```text
Tailnet browser --HTTPS--> custom ts-serve Ingress ngn-web (TLS terminates here)
  --> ClusterIP Service ngn-web:8080 --> nginx:8080 (Host/Origin checks)
  --> same-pod ngn serve bound 127.0.0.1:8765
```

This example needs a **custom** `ts-serve` operator using
`spec.ingressClassName: ts-serve` and `spec.defaultBackend.service`; it is not
an official Tailscale operator manifest. The Ingress name determines the
private hostname. Confirm the operator produces `NGN_HOST`, terminates private
HTTPS without Funnel, preserves the external Host and request headers, supports
streaming NDJSON and the `/api/events` and GPT-Live audio WebSocket upgrades.
Do not manage the operator-generated proxy workload with this manifest. There
is no public Internet listener, NodePort or LoadBalancer in this template. The
hop between ingress and pod is cluster HTTP, not end-to-end TLS.

nginx accepts only `NGN_HOST` without a port and either no Origin (for ordinary
reads) or exactly `https://NGN_HOST`. It rewrites a valid HTTPS Origin to
`http://127.0.0.1:8765` for ngn's local-authority checks. It drops upstream
Authorization headers; the app still requires its per-process `X-Ngn-Token` on
API calls except `GET /api/bootstrap`, plus same-origin fetch metadata, the
expected Origin on mutations/WebSockets, and path/query restrictions. The
64 KiB proxy body cap matches ordinary JSON mutations; authenticated image/PDF
uploads have a dedicated 8 MiB location and are further validated by ngn.
WebSocket upgrades and unbuffered responses support event streaming and Voice
duplex; nginx's `/readyz` checks only the proxy. Neither header filtering nor
the bootstrap token authenticates clients already on the trusted cluster network:
they can send an allowed Host and retrieve a token. Tailscale grants do not
restrict direct cluster Service access. Review that network reachability.

## Persistent state and provider setup

The pre-bound, `Retain` hostPath PV `ngn-web-local` and PVC
`nagents/ngn-web-state` use the dedicated `NGN_STATE_PATH` on `NGN_NODE` with
an empty storage class. The declared 5 GiB is a **binding size, not a filesystem
quota**. This is not replicated; arrange backups and monitor capacity. Losing
the node makes the pod unavailable. Do not auto-delete the PVC/PV or node data.

The `initialize-state` init container accepts an empty node directory or its
own existing marker, then creates fixed 0700 directories and assigns them to UID
1000 without recursively changing workspace files. Unrecognized non-empty
hostPath data fails startup rather than being adopted. It creates no credential
file and requires no seed Secret. The app runs as UID 1000 with a read-only root
filesystem and persistent `/state/workspace`, `/state/sessions`, `/state/home`,
`/state/config`, `/state/data` and `/state/channel-plugins`. The nginx sidecar
has a separate `/tmp` volume; neither container gets a Docker socket or a
service-account token. There is no `fsGroup` to relax private config/login
permissions. `replicas: 1` and `Recreate` avoid ordinary rollout overlap;
ReadWriteOnce is not a process lock. Do not attach another app to the PVC.

The ConfigMap's `config.yaml` contains only flat, secret-free ngn YAML: OpenAI `auth: api-key`,
`api: auto`, `api_key_env: OPENAI_API_KEY`, and `data_dir: /state/sessions`.
`OPENAI_API_KEY` comes from an **optional** `ngn-web-provider` Secret key of the
same name. With no Secret/key, the UI still starts and presents
provider setup guidance; a ready pod does not establish model access. To use
the example API-key connection, prepare a key file privately and create the
Secret separately, for example:

```bash
kubectl --context '<your-context>' -n nagents create secret generic ngn-web-provider \
  --from-file=OPENAI_API_KEY=/private/path/openai-api-key
```

Rotate it with a controlled pod restart so the process reads the new env value.
The Secret is never
put in the ConfigMap, image or browser. For other provider routes, change the
private ConfigMap and optional container env reference to match the intended
env-var name, or use **Settings → Provider connections** to save a named
connection whose key is already in the server's environment. See
[ngn configuration](ngn-configuration.md#shared-named-provider-connections) and
[container authentication](ngn-container.md#provision-and-verify). Saved
connections store env-var **names**, not values; switching connections does not
choose a chat model. Set the chat model separately in Settings. GPT-Live Voice
duplex preferences are independent of the chat model and need a supported
provider/credential; a ChatGPT subscription is not a Voice API key. Optional
ChatGPT/Codex login requires its own protected store on this PVC and a deliberate
login/transfer after deployment; it is not seeded or required at startup.

The workspace and session database persist saved conversations, web settings,
channel bindings and durable queued work. Global settings and provider/model
YAML under `/state/config` and provider login data under `/state/data` also
persist. Protect backups as credentials; check active runs and queued work before
maintenance. Process-local `schedule_wakeup` timers and child task handles do
not survive pod replacement. ConfigMap changes require a controlled pod
replacement because nginx mounts its config via `subPath`.

## Operator validation and cutover

1. Confirm the `nagents` namespace, `ghcr-pull-secret` if needed, custom operator,
   selected Linux amd64 node, dedicated empty node directory, and that all new
   resource names are unused or owned by this exact deployment. Inspect Secret
   metadata only. Preserve any legacy API workload and its ephemeral data.
2. Review the private render and image digests. In the approved cluster context,
   validate with `kubectl apply --dry-run=server -f <private-render-path>`; a
   dry-run does not start containers, create node storage or test HTTPS. Apply
   the private render only after administrator review and without pruning
   unrelated resources.
3. Verify the PVC binds, both containers become ready, the new private HTTPS URL
   serves React assets and bootstrap without a password, and an unknown Host or
   mismatched Origin is rejected. Check `/api/events`, Voice audio upgrade where
   configured, streaming, token checks, allowed query selectors and image/PDF
   uploads. `/readyz` and `/api/bootstrap` do not verify provider access.
4. Select the intended connection and chat model, then run one approved live
   request to check authentication and inference. At an idle window, restart the
   pod and verify retained workspace, sessions and config. Do not assume that
   process-local wakeups survived. If migrating a legacy endpoint, separately
   review any ingress cutover and new allowed hostnames; the public template
   does not change the legacy `nagents` Ingress.

If startup refuses existing node data, investigate provenance and recover
separately; do not remove the marker or clear retained files to bypass the
guard. Keep real cluster validation and deployment under operator control.
