"""Local HTTP transport and process ownership for the Python engine."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import socket
import stat
import sys
import threading
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from uvicorn import Server

from nagents.harness import Harness
from nagents.protocol import PROTOCOL_VERSION
from nagents.web import local_authority
from nagents.web.http.app import create_app
from nagents.web.http.runtime import server_config

from .registration import EngineStateError
from .registration import Registration
from .registration import own_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Iterator
    from types import FrameType

    from fastapi import FastAPI
    from starlette.types import ASGIApp
    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send
    from uvicorn import Config

    from nagents.harness.config import HarnessConfig
    from nagents.web.execution.service import WebState

    from .api import Backend

LOOPBACK = "127.0.0.1"


class EngineOnly:
    """Authenticate native HTTP and WebSocket clients before routing."""

    def __init__(self, app: ASGIApp, *, authority: str, token: str) -> None:
        self.app = app
        self.authority = authority
        self.token = token.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" and scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", ()) if key != b"cache-control"
                ] + [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
            await send(message)

        headers = Headers(scope=scope)
        client = scope.get("client")
        status = 0
        detail = ""
        if not client or client[0] != LOOPBACK or headers.getlist("host") != [self.authority]:
            status, detail = 403, "Untrusted engine request."
        elif headers.getlist("origin") or headers.getlist("sec-fetch-site"):
            status, detail = 403, "Browser requests are not supported."
        elif not self._authorized(headers):
            status, detail = 401, "Engine bearer authentication is required."

        if status:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                challenge = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
                await JSONResponse({"detail": detail}, status_code=status, headers=challenge)(
                    scope, receive, secure_send
                )
            return

        await self.app(scope, receive, secure_send)

    def _authorized(self, headers: Headers) -> bool:
        authorization = headers.getlist("authorization")
        if len(authorization) != 1:
            return False
        scheme, _, credential = authorization[0].partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(credential.encode("latin-1"), self.token)


def create_engine_app(
    config: HarnessConfig,
    *,
    port: int,
    token: str,
    harness_factory: Callable[[HarnessConfig], Harness] = Harness,
    backend_factory: Callable[[Callable[[], WebState]], Backend] | None = None,
) -> FastAPI:
    """Install only the versioned API on the shared Harness/WebState lifespan."""
    from .api import install_api

    if not 1 <= port <= 65535:
        raise ValueError("The engine app needs its actual loopback port.")
    if not token:
        raise ValueError("The engine app needs a bearer token.")

    def create_backend(get_state: Callable[[], WebState]) -> Backend:
        if backend_factory is not None:
            return backend_factory(get_state)
        from .runtime import EngineRuntime

        return EngineRuntime(get_state)

    def routes(app: FastAPI, get_state: Callable[[], WebState]) -> None:
        shared_lifespan = app.router.lifespan_context
        backend: Backend

        @asynccontextmanager
        async def lifespan(application: FastAPI) -> AsyncIterator[None]:
            nonlocal backend
            async with shared_lifespan(application):
                backend = create_backend(get_state)
                yield

        app.router.lifespan_context = lifespan
        install_api(app, lambda: backend)

    app = create_app(config, harness_factory=harness_factory, headless_routes=routes)
    app.add_middleware(EngineOnly, authority=local_authority(LOOPBACK, port), token=token)
    return app


@dataclass(frozen=True, slots=True)
class StdioLease:
    """The parent's open stdin writer is the lifetime lease."""

    stdin: int
    readiness: int

    def publish(self, registration: Registration) -> None:
        payload = registration.encode()
        if os.write(self.readiness, payload) != len(payload):
            raise OSError("Incomplete private engine readiness frame.")


@contextmanager
def private_stdio() -> Iterator[StdioLease]:
    """Reserve stdout for a single readiness frame on an owner-only pipe."""
    if os.name != "posix":
        raise ValueError("The Python engine currently requires a POSIX host.")
    for descriptor in (0, 1):
        info = os.fstat(descriptor)
        if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("--stdio requires private stdin and stdout pipes from the launcher.")
    sys.stdout.flush()
    readiness = os.dup(1)
    try:
        os.set_blocking(readiness, False)
        # Diagnostics from the runtime (including trusted plugins) are not
        # readiness frames. Keep their stdout off the launcher's private pipe.
        os.dup2(2, 1)
        yield StdioLease(0, readiness)
    finally:
        try:
            sys.stdout.flush()
        finally:
            os.dup2(readiness, 1)
            os.close(readiness)


@contextmanager
def _stop_on_owner_exit(stop: asyncio.Event, lease: StdioLease | None) -> Iterator[None]:
    loop = asyncio.get_running_loop()

    def read_lease() -> None:
        try:
            if lease is not None and os.read(lease.stdin, 4096):
                return  # stdin carries no commands.
        except BlockingIOError:
            return
        except OSError:
            pass  # A broken lease is no longer owned by the launcher.
        if lease is not None:
            loop.remove_reader(lease.stdin)
        stop.set()

    def request_exit(signum: int, frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)

    handlers = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, request_exit)
        if lease is not None:
            loop.add_reader(lease.stdin, read_lease)
        yield
    finally:
        if lease is not None:
            loop.remove_reader(lease.stdin)
        for signum, handler in handlers.items():
            if handler is not None:
                signal.signal(signum, handler)


class _QuietHandshakeLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn logs WebSocket URLs through its error logger, even with
        # access_log=False. An invalid client may have put a secret in the URL.
        return not isinstance(record.msg, str) or ' - "WebSocket ' not in record.msg


class _EngineServer(Server):
    def __init__(self, config: Config, stop: asyncio.Event, publish: Callable[[], None]) -> None:
        super().__init__(config)
        self.stop = stop
        self.publish = publish
        self.readiness_error: OSError | EngineStateError | None = None

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Do not let Uvicorn re-raise SIGTERM before registration cleanup.
        yield

    def install_signal_handlers(self) -> None:
        """Older Uvicorn versions also defer to the engine's signal owner."""

    async def main_loop(self) -> None:
        # Uvicorn reaches this only after lifespan startup and socket listen.
        # Return (not raise) on publication failure to run Uvicorn shutdown.
        if self.stop.is_set():
            return
        try:
            self.publish()
        except (OSError, EngineStateError) as error:
            self.readiness_error = error
            return

        async def wait_for_stop() -> None:
            await self.stop.wait()
            self.should_exit = True

        waiter = asyncio.create_task(wait_for_stop())
        try:
            await super().main_loop()
        finally:
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter


async def serve_engine(config: HarnessConfig, *, port: int = 0, lease: StdioLease | None = None) -> None:
    """Own one state directory, private credential and loopback listener until shutdown."""
    if not 0 <= port <= 65535:
        raise ValueError("--port must be between 0 and 65535.")
    stop = asyncio.Event()
    with (
        _stop_on_owner_exit(stop, lease),
        own_state(config.data_dir) as store,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
    ):
        listener.bind((LOOPBACK, port))
        bound_port = listener.getsockname()[1]
        registration = Registration(
            url=f"http://{local_authority(LOOPBACK, bound_port)}",
            token=secrets.token_urlsafe(32),
            pid=os.getpid(),
            protocol=PROTOCOL_VERSION,
        )
        app = create_engine_app(config, port=bound_port, token=registration.token)
        options = server_config(app, host=LOOPBACK, port=bound_port)
        options.lifespan = "on"
        publisher = store.publish if lease is None else lease.publish
        server = _EngineServer(options, stop, lambda: publisher(registration))
        logger = logging.getLogger("uvicorn.error")
        quiet_handshakes = _QuietHandshakeLogs()
        logger.addFilter(quiet_handshakes)
        try:
            await server.serve(sockets=[listener])
        except SystemExit as error:
            raise RuntimeError("The Python engine server could not start.") from error
        finally:
            logger.removeFilter(quiet_handshakes)
        if server.readiness_error is not None:
            raise RuntimeError(
                "The Python engine could not publish private readiness metadata."
            ) from server.readiness_error
        if not server.started:
            raise RuntimeError("The Python engine server could not start.")
