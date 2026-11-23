# 0003. Enforce and audit the agent pod's network access

- Status: Proposed
- Date: 2026-11-23

## Context

NGN can run user-approved code, install dependencies and use workspace credentials.
Signing a user in does not constrain what that code can contact. An unrestricted
pod can reach internal services or send workspace data to an external destination.
Conversely, blocking all outbound HTTPS breaks sign-in, providers and package
installation. This decision concerns network reachability, not the agent's ability
to write files or install packages in its container.

## Decision

Enforce policy **outside the agent pod**. Start with an inventory of required flows,
then isolate the selected workload for both ingress and egress using a
NetworkPolicy-capable CNI. Use Cilium policy where DNS-name controls are required.
Keep rules in the private deployment configuration; public examples and tickets
must not contain live labels, addresses, captures or destination inventories.

| Purpose | Guideline |
| --- | --- |
| Browser HTTP and WebSocket ingress | Allow only the authenticated ingress connector to the web port. Treat voice/webhook ports separately: identify their sender and authentication before enforcing policy. ClusterIP alone is not isolation. |
| Client IP attribution | Trust a forwarding header only when the immediate peer is a verified connector and the edge overwrites or sanitizes it. A host-network tunnel may appear as a node IP; do not assume a pod selector matches it, or trust the whole node/pod CIDR. If attribution is ambiguous, record the transport peer as such. IP addresses are not authentication. |
| DNS and identity | Allow TCP and UDP DNS to the actual cluster resolver (including node-local DNS if used). Permit the issuer's discovery, authorization back-channel/token and JWKS dependencies where the **server** needs them. The browser redirect is not pod egress. |
| Providers, packages and source | Inventory exact approved endpoints, redirects, artifact hosts and protocols. Prefer a package mirror or authenticated egress gateway; apply expiring, owner-reviewed exceptions for new destinations. Do not turn every dependency request into a permanent wildcard. |
| Internal and host services | Leave unrelated namespaces, metadata endpoints, control-plane and node management services outside the allowlist. Verify both IP families and the local node path: standard NetworkPolicy has node-traffic exceptions. Do not treat it as a host firewall. |

Kubernetes policies are additive: an existing broad allow still grants access.
An IP allowlist is brittle for CDNs; an FQDN rule may authorize shared addresses
and does not restrict URL paths or uploaded content. An approved service can
still receive sensitive data. Keep credentials narrowly scoped. Where finer
control is needed, route the workload through a constrained egress proxy and
prevent direct egress around it. Do not install a second CNI just for a UI.

## Visibility and alternatives

| Option | Useful for | Limits |
| --- | --- | --- |
| [Cilium Hubble UI and Relay](https://docs.cilium.io/en/stable/observability/hubble/hubble-ui/) | First choice on Cilium: service map and flow list for debugging peers, ports and forwarded/dropped verdicts. Use a controlled denied connection to confirm visibility. | Relay/UI is not archival storage; use a [flow exporter](https://docs.cilium.io/en/stable/observability/hubble/configuration/export/) for retained evidence. A forwarded flow is not proof that an application request succeeded. |
| [Net Watcher network map](https://github.com/abi-jey/net-watcher/blob/main/docs/network-map.md) | Captured connections, namespace/IP/time filters, capture-time pod identity and matched DNS answers with TTL. Useful for investigating where a package or provider name resolved. | Capture placement, SNAT, encrypted DNS and stale inventory limit attribution. Map edges are observations, not policy verdicts. Track exact pod selection and connection filtering in [Net Watcher #3](https://github.com/abi-jey/net-watcher/issues/3). |
| [Kubernetes NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/) alone | Portable namespace/pod/port rules without a Cilium-specific dependency. | Enforcement depends on the CNI. No portable FQDN filtering, connection audit log or traffic UI. [Cilium DNS policy](https://docs.cilium.io/en/stable/security/policy/language/#dns-based) can supplement it. |

### Audit contract

Record UTC timestamp, direction, namespace, workload, pod UID at observation,
peer identity or address, IP family, port/protocol, observation source, and
verdict/reason **when supplied by the enforcement point**. Record unknown rather
than guessing. Keep DNS evidence separate from connection evidence. Publish
collector health, dropped-event counts and retention gaps alongside the flows.
Do not capture payloads, headers, cookies, callback URLs or tokens. DNS names and
addresses themselves can be sensitive: restrict dashboards and export access,
audit reads, and set an explicit retention period and storage limit before export.

## Rollout and rollback

1. **Observe privately.** Check the installed CNI, supported policy modes, node
   placement and connector identity. Exercise fresh sign-in, key discovery, chat,
   events, voice, package install and registry redirects. Record required peers
   and owners. A quiet observation window does not prove a flow is unnecessary.
2. **Canary with disposable state.** Use a separate test workload and credentials;
   never attach two agent instances to the production workspace. Prepare emergency
   access independent of the web UI. Review all policies selecting the canary.
3. **Prove enforcement and evidence together.** Verify the intended connector and
   approved dependencies work, a separate unauthorized test pod cannot reach the
   web port, and outbound connections to operator-controlled disallowed test
   destinations fail. Verify the expected verdict, correct pod and timestamp in
   the dashboard/export. Test a restart with a new UID/IP and both address families
   where enabled. Avoid sending real user data in test traffic.
4. **Promote privately.** Version the policy and test evidence; name the operator
   and the rollback revision. Monitor new denials, auth failures, DNS resolution
   and dropped telemetry. Roll back the exact policy change if a required flow
   breaks. Do not compensate by opening all egress or changing pod privileges.
5. **Separate internet cutover.** A green private rollout does not authorize a
   public route. Review edge access control, rate limiting, connector-only access
   and log redaction separately before changing exposure.

## Consequences

The agent retains container-local flexibility; the operator owns its network
permissions and the audit trail. New integrations may require reviewed network
exceptions. A compromised node, shared approved destination or lost telemetry
remains a risk even when the visible policy and tests pass.
