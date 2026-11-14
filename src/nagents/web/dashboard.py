"""Read-only dashboard shell, shared by the web client and an on-demand TUI host."""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import sys
from contextlib import contextmanager
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from uvicorn import Server


def dashboard_summary(workspace: Path) -> dict[str, str]:
    """Describe the shell honestly until usage instrumentation is connected."""
    return {"workspace": str(workspace), "status": "not_connected"}


def create_dashboard_app(workspace: Path, assets: Path, authority: str) -> FastAPI:
    """Serve no harness, credentials, execution endpoints, or session mutations."""
    from fastapi import FastAPI
    from starlette.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    from .http.security import LocalOnly

    token = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnly, authority=authority, token=token)

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, object]:
        return {"token": token, "workspace": str(workspace), "capabilities": {"chat": False}}

    @app.get("/api/dashboard")
    async def summary() -> dict[str, str]:
        return dashboard_summary(workspace)

    @app.get("/", response_model=None)
    @app.get("/dashboard", response_model=None)
    async def index() -> FileResponse:
        return FileResponse(assets / "index.html")

    app.mount("/assets", StaticFiles(directory=assets / "assets", follow_symlink=False), name="assets")
    return app


class DashboardHost:
    """Own one loopback server for the lifetime of a terminal client."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.url = ""
        self._server: Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> str:
        async with self._lock:
            if self._task is not None and not self._task.done():
                return self.url
            await self.close()
            try:
                from uvicorn import Config
                from uvicorn import Server

                from . import built_assets

                class EmbeddedServer(Server):
                    def install_signal_handlers(self) -> None:
                        # Uvicorn < 0.29 uses this hook instead of capture_signals.
                        pass

                    @contextmanager
                    def capture_signals(self) -> Iterator[None]:
                        # Textual owns process signals; this server only owns its socket.
                        yield

                assets = await asyncio.to_thread(built_assets)
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket = listener
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                app = create_dashboard_app(self.workspace, assets, f"127.0.0.1:{port}")
                self._server = EmbeddedServer(
                    Config(
                        app,
                        host="127.0.0.1",
                        port=port,
                        proxy_headers=False,
                        ws="none",
                        access_log=False,
                        log_config=None,
                        log_level="warning",
                        timeout_graceful_shutdown=3,
                    )
                )
                self._task = asyncio.create_task(self._server.serve(sockets=[listener]), name="ngn-dashboard")
                async with asyncio.timeout(10):
                    while not self._server.started:
                        if self._task.done():
                            await self._task
                            raise RuntimeError("Dashboard server stopped before startup")
                        await asyncio.sleep(0.01)
                self.url = f"http://127.0.0.1:{port}/dashboard"
                return self.url
            except ModuleNotFoundError as error:
                await self.close()
                raise ValueError(
                    "Dashboard requires the web extra. Install nagents[web] in this virtualenv."
                ) from error
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        try:
            if self._task is not None:
                try:
                    async with asyncio.timeout(5):
                        await asyncio.shield(self._task)
                except TimeoutError:
                    self._task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._task
        finally:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._server = None
            self._task = None
            self.url = ""


async def open_dashboard_browser(url: str) -> bool:
    """Ask the default desktop handler without a terminal browser or executor job."""
    if any(os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return False
    if sys.platform == "darwin":
        command = ("/usr/bin/open", url)
    elif sys.platform == "win32":
        command = ("rundll32.exe", "url.dll,FileProtocolHandler", url)
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        # Unlike xdg-open/webbrowser, gio never falls back to a text-mode browser.
        command = ("/usr/bin/gio", "open", url)
    else:
        return False
    process: asyncio.subprocess.Process | None = None
    starting = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    try:
        process = await asyncio.shield(starting)
        async with asyncio.timeout(2):
            return await process.wait() == 0
    except asyncio.CancelledError:
        with suppress(OSError):
            process = await starting
        raise
    except (OSError, TimeoutError):
        return False
    finally:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
