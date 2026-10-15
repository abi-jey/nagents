"""Explicit loopback test bridge to one deployed ngn origin, never a general proxy."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote
from urllib.parse import urlsplit

import aiohttp
import uvicorn
from examples.live.browser_e2e.server import log_configuration
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from starlette.responses import FileResponse
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from starlette.websockets import WebSocketState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REQUEST_HEADERS = frozenset({"accept", "content-type", "x-ngn-token", "range", "if-none-match"})
RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-security-policy",
        "content-disposition",
        "cache-control",
        "etag",
        "content-range",
        "accept-ranges",
        "permissions-policy",
        "x-content-type-options",
        "referrer-policy",
    }
)
MAX_REQUEST_BYTES = 1024 * 1024


def deployed_origin(value: str) -> str:
    """A fixed HTTPS origin; credentials, paths, queries and redirects are forbidden."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
        or "\\" in value
    ):
        raise ValueError("--origin must be one HTTPS origin without credentials, path, query or fragment")
    # Access port to reject malformed values before any request is possible.
    _ = parsed.port
    return f"https://{parsed.netloc}"


def allowed_path(path: str) -> bool:
    decoded = unquote(path)
    return (
        decoded.startswith("/")
        and not decoded.startswith("//")
        and "\\" not in decoded
        and not any(segment in {".", ".."} for segment in decoded.split("/"))
        and (decoded == "/" or decoded.startswith(("/api/", "/assets/")) or decoded == "/favicon.svg")
    )


def same_origin(headers: dict[str, str], port: int, *, websocket: bool = False) -> bool:
    authority = f"127.0.0.1:{port}"
    origin = headers.get("origin", "")
    return (
        headers.get("host") == authority
        and headers.get("sec-fetch-site", "") in {"", "none", "same-origin"}
        and origin in ({f"http://{authority}"} if websocket else {"", f"http://{authority}"})
    )


class RedirectRefused(aiohttp.ClientError):
    """Never send browser tokens or token-bearing WebSocket protocols to a redirect."""


async def refuse_redirect(*_args: object) -> None:
    # ws_connect follows redirects internally. The trace runs before following
    # Location, so this applies the same policy as HTTP allow_redirects=False.
    raise RedirectRefused("The configured ngn origin redirected")


def create_bridge(origin: str, fixtures: Path, *, port: int) -> FastAPI:
    target = deployed_origin(origin)
    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(refuse_redirect)
    client: aiohttp.ClientSession

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal client
        # No ambient proxy settings, cookies or credentials. Only explicit
        # browser ngn-token headers/protocols go to the fixed target origin.
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=90),
            trace_configs=[trace],
        ) as client:
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], response_model=None)
    async def forward(request: Request, path: str) -> FileResponse | JSONResponse | StreamingResponse:
        relative = f"/{path}"
        if (
            len(request.headers.getlist("host")) != 1
            or len(request.headers.getlist("origin")) > 1
            or not same_origin(dict(request.headers), port)
            or not allowed_path(relative)
        ):
            return JSONResponse({"detail": "Exact loopback origin and ngn paths only"}, status_code=403)
        if request.method in {"GET", "HEAD"}:
            if relative == "/assets/voice-e2e-capture.js":
                return FileResponse(Path(__file__).with_name("capture-worklet.js"), media_type="text/javascript")
            if relative == "/assets/voice-e2e-input.wav":
                return FileResponse(fixtures / "caller.wav", media_type="audio/wav")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return JSONResponse({"detail": "Test bridge request too large"}, status_code=413)
        headers = {key: value for key, value in request.headers.items() if key.lower() in REQUEST_HEADERS}
        headers.update({"Origin": target, "Accept-Encoding": "identity"})
        url = target + relative + (f"?{request.url.query}" if request.url.query else "")
        try:
            response = await client.request(request.method, url, headers=headers, data=body, allow_redirects=False)
        except (aiohttp.ClientError, TimeoutError):
            return JSONResponse({"detail": "Configured deployment connection failed"}, status_code=502)
        if 300 <= response.status < 400 and response.status != 304:
            response.close()
            return JSONResponse({"detail": "Deployment redirect refused"}, status_code=502)
        # A compressing ingress despite Accept-Encoding would change audio
        # timing and require preserving Content-Encoding; refuse it explicitly.
        if response.headers.get("Content-Encoding", "identity") not in {"", "identity"}:
            response.close()
            return JSONResponse({"detail": "Unexpected deployment compression"}, status_code=502)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.content.iter_any():
                    yield chunk
            finally:
                response.close()

        return StreamingResponse(
            stream(),
            status_code=response.status,
            headers={key: value for key, value in response.headers.items() if key.lower() in RESPONSE_HEADERS},
        )

    @app.websocket("/{path:path}")
    async def socket(browser: WebSocket, path: str) -> None:
        relative = f"/{path}"
        if (
            len(browser.headers.getlist("host")) != 1
            or len(browser.headers.getlist("origin")) != 1
            or not same_origin(dict(browser.headers), port, websocket=True)
            or not allowed_path(relative)
            or browser.query_params
            or not (
                relative == "/api/events" or re.fullmatch(r"/api/live/sessions/[A-Za-z0-9_-]{1,128}/audio", relative)
            )
        ):
            await browser.close(code=1008)
            return
        protocols = browser.scope.get("subprotocols", [])
        expected = "ngn.events.v1" if relative == "/api/events" else "ngn.live.v2"
        if len(protocols) != 2 or expected not in protocols or sum(p.startswith("ngn.token.") for p in protocols) != 1:
            await browser.close(code=1008)
            return
        try:
            async with client.ws_connect(
                target + relative, origin=target, protocols=protocols, compress=0, max_msg_size=2 * 1024 * 1024
            ) as upstream:
                if upstream.protocol != expected:
                    await browser.close(code=1008)
                    return
                await browser.accept(subprotocol=expected)
                sent = received = 0
                peak_send_ms = peak_receive_ms = 0.0
                last_report = time.monotonic()

                def report(*, final: bool = False) -> None:
                    nonlocal last_report
                    if not final and time.monotonic() - last_report < 5:
                        return
                    last_report = time.monotonic()
                    # Numeric evidence about the added loopback hop. No paths,
                    # token protocols, content or provider payloads are logged.
                    print(
                        f"voice_e2e_bridge wall_ns={time.time_ns()} final={int(final)} sent={sent} received={received} "
                        f"peak_send_ms={peak_send_ms:.3f} peak_receive_ms={peak_receive_ms:.3f}",
                        flush=True,
                    )

                async def outgoing() -> None:
                    nonlocal sent, peak_send_ms
                    while True:
                        message = await browser.receive()
                        began = time.perf_counter()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await upstream.send_bytes(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send_str(message["text"])
                        sent += 1
                        peak_send_ms = max(peak_send_ms, (time.perf_counter() - began) * 1000)
                        report()

                async def incoming() -> None:
                    nonlocal received, peak_receive_ms
                    async for message in upstream:
                        began = time.perf_counter()
                        if message.type == aiohttp.WSMsgType.BINARY:
                            await browser.send_bytes(message.data)
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            await browser.send_text(message.data)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise aiohttp.ClientError("Deployment WebSocket failed")
                        else:
                            continue
                        received += 1
                        peak_receive_ms = max(peak_receive_ms, (time.perf_counter() - began) * 1000)
                        report()

                tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    report(final=True)
        except (aiohttp.ClientError, TimeoutError, WebSocketDisconnect):
            if browser.client_state != WebSocketState.DISCONNECTED:
                await browser.close(code=1011)
        finally:
            if (
                browser.client_state != WebSocketState.DISCONNECTED
                and browser.application_state != WebSocketState.DISCONNECTED
            ):
                await browser.close()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, type=deployed_origin)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8880)
    args = parser.parse_args()
    app = create_bridge(args.origin, args.fixtures.resolve(), port=args.port)
    print(f"Test bridge to {args.origin}; opens the real deployment, including its real saved sessions.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_config=log_configuration())


if __name__ == "__main__":
    main()
