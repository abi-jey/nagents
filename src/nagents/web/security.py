"""Same-origin loopback boundary. This is not authentication against local users."""

import re
import secrets
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp
    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-DNS-Prefetch-Control": "off",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "microphone=(self), camera=()",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
        "media-src 'self' blob: data:; connect-src 'self'; worker-src 'self'; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


class LocalOnly:
    def __init__(self, app: "ASGIApp", *, authority: str, token: str, enforce_authority: bool = True) -> None:
        self.app = app
        self.authority = authority
        self.token = token
        # A non-loopback bind has no single trusted authority to compare against,
        # so the exact Host/Origin checks are skipped. The per-process token,
        # same-origin fetch metadata, query, method, and path checks still apply.
        self.enforce_authority = enforce_authority

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] == "websocket":
            headers = Headers(scope=scope)
            protocols = [
                part.strip() for value in headers.getlist("sec-websocket-protocol") for part in value.split(",")
            ]
            tokens = [
                protocol.removeprefix("ngn.token.") for protocol in protocols if protocol.startswith("ngn.token.")
            ]
            path = scope["path"]
            expected = (
                "ngn.events.v1"
                if path == "/api/events"
                else ("ngn.live.v2" if "ngn.live.v2" in protocols else "ngn.live.v1")
                if re.fullmatch(r"/api/live/sessions/[A-Za-z0-9_-]{1,128}/audio", path)
                else ""
            )
            if (
                not expected
                or scope["query_string"]
                or (self.enforce_authority and headers.getlist("host") != [self.authority])
                or (self.enforce_authority and headers.getlist("origin") != [f"http://{self.authority}"])
                or headers.get("sec-fetch-site", "") not in {"", "none", "same-origin"}
                or len(protocols) != 2
                or protocols.count(expected) != 1
                or len(tokens) != 1
                or not secrets.compare_digest(tokens[0].encode(), self.token.encode())
            ):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message: "Message") -> None:
            if message["type"] == "http.response.start":
                headers = {key.lower().encode(): value.encode() for key, value in SECURITY_HEADERS.items()}
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k not in headers]
                message["headers"].extend(headers.items())
            await send(message)

        async def reject(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, secure_send)

        headers = Headers(scope=scope)
        origin = f"http://{self.authority}"
        if self.enforce_authority and headers.getlist("host") != [self.authority]:
            await reject(403, "Untrusted Host. Open the exact loopback URL printed by ngn serve.")
            return
        if headers.get("sec-fetch-site", "") not in {"", "none", "same-origin"}:
            await reject(403, "Same-origin requests only.")
            return
        if self.enforce_authority and headers.getlist("origin") and headers.getlist("origin") != [origin]:
            await reject(403, "Same-origin requests only.")
            return
        path = scope["path"]
        if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
            await reject(404, "Not found.")
            return
        # Only bounded, non-secret Live cursor, delegation and Voice scope selectors are accepted.
        # Other queries (including duplicate parameters and tokens) remain rejected.
        live_cursor = (
            scope["method"] == "GET"
            and re.fullmatch(r"/api/live/sessions/[A-Za-z0-9_-]{1,128}", path) is not None
            and re.fullmatch(rb"after=[0-9]{1,16}", scope["query_string"]) is not None
        )
        voice_scope = (
            scope["method"] == "GET"
            and path == "/api/live/settings"
            and scope["query_string"] in {b"scope=global", b"scope=workspace"}
        )
        delegation_query = False
        if (
            scope["method"] == "GET"
            and len(scope["query_string"]) <= 4096
            and re.fullmatch(r"/api/live/sessions/[A-Za-z0-9_-]{1,128}/delegation-details", path)
        ):
            try:
                query = scope["query_string"].decode("ascii")
                if re.search(r"%(?![0-9a-fA-F]{2})", query):
                    raise ValueError("Invalid query encoding")
                pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=1, errors="strict")
                delegation_query = (
                    len(pairs) == 1
                    and pairs[0][0] == "delegation_id"
                    and 0 < len(pairs[0][1]) <= 256
                    and bool(pairs[0][1].strip())
                )
            except (ValueError, UnicodeError):
                pass
        if scope["query_string"] and not (live_cursor or voice_scope or delegation_query):
            await reject(400, "Query parameters are not supported. Do not put tokens in URLs.")
            return
        method = scope["method"]
        if path.startswith("/api/") and not (path == "/api/bootstrap" and method == "GET"):
            tokens = headers.getlist("x-ngn-token")
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0].encode(), self.token.encode()):
                await reject(403, "Invalid web token. Reconnect to this ngn serve instance.")
                return
        if method not in {"GET", "HEAD", "POST", "PUT", "DELETE"}:
            await reject(405, "Method not allowed.")
            return
        if method in {"POST", "PUT", "DELETE"}:
            if self.enforce_authority and headers.getlist("origin") != [origin]:
                await reject(403, "An exact same-origin Origin header is required.")
                return
            parts = path.split("/")
            if method == "POST" and len(parts) == 6 and parts[1:3] == ["api", "sessions"] and parts[4] == "uploads":
                # Authentication precedes streaming. The dedicated route enforces
                # MIME, actual byte counts, concurrency and a read deadline.
                await self.app(scope, receive, secure_send)
                return
            if headers.get("content-type", "").lower() not in {"application/json", "application/json; charset=utf-8"}:
                await reject(415, "Use application/json.")
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY:
                    await reject(413, "Request body exceeds 64 KiB.")
                    return
                if not message.get("more_body", False):
                    break
            delivered = False

            async def buffered_receive() -> "Message":
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, secure_send)
        else:
            await self.app(scope, receive, secure_send)
