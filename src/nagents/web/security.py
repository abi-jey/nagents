"""Same-origin loopback boundary. This is not authentication against local users."""

import secrets
from typing import TYPE_CHECKING

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
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "microphone=(self), camera=()",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


class LocalOnly:
    def __init__(self, app: "ASGIApp", *, authority: str, token: str) -> None:
        self.app = app
        self.authority = authority
        self.token = token

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] == "websocket":
            headers = Headers(scope=scope)
            protocols = [
                part.strip() for value in headers.getlist("sec-websocket-protocol") for part in value.split(",")
            ]
            tokens = [
                protocol.removeprefix("ngn.token.") for protocol in protocols if protocol.startswith("ngn.token.")
            ]
            if (
                scope["path"] != "/api/events"
                or scope["query_string"]
                or headers.getlist("host") != [self.authority]
                or headers.getlist("origin") != [f"http://{self.authority}"]
                or headers.get("sec-fetch-site", "") not in {"", "none", "same-origin"}
                or len(protocols) != 2
                or protocols.count("ngn.events.v1") != 1
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
        if headers.getlist("host") != [self.authority]:
            await reject(403, "Untrusted Host. Open the exact loopback URL printed by ngn serve.")
            return
        if (headers.getlist("origin") and headers.getlist("origin") != [origin]) or headers.get(
            "sec-fetch-site", ""
        ) not in {"", "none", "same-origin"}:
            await reject(403, "Same-origin requests only.")
            return
        path = scope["path"]
        if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
            await reject(404, "Not found.")
            return
        if scope["query_string"]:
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
            if headers.getlist("origin") != [origin]:
                await reject(403, "An exact same-origin Origin header is required.")
                return
            if path == "/api/dictation/transcribe":
                if [value.lower() for value in headers.getlist("content-type")] != ["audio/wav"]:
                    await reject(415, "Use audio/wav for the recording upload.")
                    return
                if headers.getlist("content-encoding"):
                    await reject(415, "Content-Encoding is not supported for recordings.")
                    return
                # The route acquires the idle slot before reading, then counts
                # actual bytes under its captured configuration and a deadline.
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
