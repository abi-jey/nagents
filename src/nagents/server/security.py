"""Single-operator access boundary, not a tool or filesystem sandbox.

NAGENTS_SERVER_TOKEN is read once, after the app loads dotenv. Unset means
loopback clients AND loopback Host headers only. A configured token must be a
nonempty RFC 6750 bearer token; an empty/invalid value fails startup, never
disables authentication. Use a high-entropy random value and restart to rotate.

Every HTTP route except GET /health requires the token when configured. API
clients send Authorization: Bearer <token>. Browsers can use HTTP Basic with
username "nagents" and the token as password, preserving native EventSource
and attachment links without putting credentials in URLs or browser storage.
Only same-origin browser requests are accepted; there is no cross-origin API.

Use TLS for non-local access. Always configure a token behind a reverse proxy:
a loopback proxy is otherwise indistinguishable from a local operator. The
proxy must preserve Host and the request scheme for same-origin checks. This
shared credential grants full control, not per-user/session authorization.
For TLS termination at a proxy, use an ASGI runner configured with explicitly
trusted proxy IPs to supply the original scheme. The module entrypoint ignores
forwarded headers deliberately; never trust arbitrary proxy headers in local mode.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import os
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp
    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send


def load_server_token() -> str:
    token = os.environ.get("NAGENTS_SERVER_TOKEN")
    if token is None:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
        raise ValueError("NAGENTS_SERVER_TOKEN must be a nonempty RFC 6750 bearer token")
    return token


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ServerSecurityMiddleware:
    """Check access before routing or starting an SSE stream; never buffer it."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def no_cache_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        # Keep the shipped Kubernetes liveness/readiness probe public.
        if scope["method"] == "GET" and scope["path"] == "/health":
            await self.app(scope, receive, no_cache_send)
            return

        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        try:
            host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else urlsplit("")
            valid_host = (
                host.hostname
                and host.username is None
                and host.password is None
                and not (host.path or host.query or host.fragment)
            )
            _ = host.port  # Validate malformed ports as well as malformed IPv6 literals.
        except ValueError:
            valid_host = False
        if not valid_host:
            await JSONResponse({"detail": "Invalid Host header"}, status_code=400)(scope, receive, no_cache_send)
            return

        origins = headers.getlist("origin")
        if (origins and origins != [f"{scope['scheme']}://{hosts[0]}"]) or headers.get(
            "sec-fetch-site", "none"
        ) not in {"same-origin", "none"}:
            await JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)(
                scope, receive, no_cache_send
            )
            return

        if self._token:
            authorized = False
            credentials = headers.getlist("authorization")
            if len(credentials) == 1:
                scheme, _, value = credentials[0].partition(" ")
                if scheme.lower() == "bearer":
                    authorized = hmac.compare_digest(value.encode("utf-8"), self._token)
                elif scheme.lower() == "basic":
                    try:
                        decoded = base64.b64decode(value, validate=True)
                        authorized = hmac.compare_digest(decoded, b"nagents:" + self._token)
                    except (ValueError, binascii.Error):
                        pass
            if not authorized:
                await JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="nagents", charset="UTF-8"'},
                )(scope, receive, no_cache_send)
                return
        else:
            client = scope.get("client")
            if not client or not is_loopback_host(client[0]) or not is_loopback_host(host.hostname or ""):
                await JSONResponse({"detail": "Local access only; configure NAGENTS_SERVER_TOKEN"}, status_code=403)(
                    scope, receive, no_cache_send
                )
                return

        await self.app(scope, receive, no_cache_send)
