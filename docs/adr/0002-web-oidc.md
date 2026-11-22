# 0002. Built-in OIDC for the private web workspace

- Status: Accepted
- Date: 2026-11-22

## Context

The Python web server's same-origin checks and browser token protect against
cross-site requests but do not identify users who can reach the server.
Private deployments need to restrict access to the trusted workspace to
accounts assigned to the application by their identity provider.

## Decision

The Python web server provides opt-in OpenID Connect against a configured HTTPS
issuer. It uses a confidential client, authorization code flow with S256 PKCE,
single-use state, and signed ID token validation. Assignment is enforced by the
identity provider prior to token issuance. NGN does not grant separate roles or
isolate workspaces by account.

NGN issues an opaque browser session and requires it for the document, assets,
API (including bootstrap), and WebSocket upgrades. The health probe reveals
only process health. Existing same-origin and browser-token checks remain in
place. Deployments keep their private network boundary and HTTPS termination;
the web server validates the configured public Host and does not trust
forwarded identity or origin headers.

The TUI, Engine v1, the native browser gateway, and unconfigured local Python
web use are unchanged.

## Alternatives considered

An external authentication proxy would add another component and require
separately preventing direct access to the web service. Built-in admission
protects that service even when called directly.

## Consequences

NGN owns session expiry, local logout, and WebSocket termination. Sessions are
in memory, expire after one hour, and are not shared across processes. Removing
an assignment blocks the next sign-in, not an already issued NGN session.
Provider tokens are used only for sign-in and are not retained or forwarded.
No rollout should rely on assignment until an unassigned account has been
tested and denied.
