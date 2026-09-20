"""Loopback app/client helpers shared by web suites."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from starlette.requests import ClientDisconnect

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.tools import CodingTools
from nagents.web.app import create_app
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from starlette.types import Message
    from starlette.types import Scope

    from nagents.harness.types import HarnessEvent
    from nagents.types import ContentPart


URL = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def no_guarded_workspace_io(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("requires_posix") is not None:
        yield
        return
    # Prove portable cases do not depend on workspace tools, even on a POSIX host.
    with patch.object(
        CodingTools, "directory", side_effect=OSError("Guarded workspace file tools currently require POSIX")
    ) as guarded:
        yield
        guarded.assert_not_called()


class ControlledHarness(Harness):
    """Portable scripted runs; real Harness sessions, approvals, initialization and close."""

    def __init__(self, config: HarnessConfig) -> None:
        super().__init__(config)
        self.stopped = asyncio.Event()
        self.closed = False
        self.decisions: list[bool] = []
        self.loop = asyncio.get_running_loop()

    async def initialize(self, *, create_session: bool = True) -> None:
        assert asyncio.get_running_loop() is self.loop
        # Replace only workspace discovery on this injected test instance. Keep
        # the real initialization lock and SQLite session/membership lifecycle.
        with patch.object(self.tools, "instructions", return_value=""), patch.object(self.tools, "discover_skills"):
            await super().initialize(create_session=create_session)

    async def run(self, prompt: str | list[ContentPart]) -> AsyncGenerator[HarnessEvent, None]:
        try:
            yield TextChunkEvent(chunk="partial text")
            if prompt == "approval":
                for _ in range(2):
                    try:
                        await self.approve(
                            "example", {"path": "example.txt"}, "A test operation", "sample diff", "call-1"
                        )
                        self.decisions.append(True)
                    except PermissionError:
                        self.decisions.append(False)
            elif prompt == "error":
                raise RuntimeError("SECRET-test-provider-key")
            else:
                await asyncio.Event().wait()
            yield TextDoneEvent(text="complete")
            yield DoneEvent(session_id=self.session_id)
        finally:
            # A cancellable cleanup turn catches response cancellation bugs.
            await asyncio.sleep(0.01)
            self.stopped.set()

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        await super().close()
        self.closed = True


@asynccontextmanager
async def client_app(
    tmp_path: Path,
    *,
    controlled: bool = True,
    config: HarnessConfig | None = None,
    host: str = "127.0.0.1",
    base_url: str = URL,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, dict[str, str], list[Harness]]]:
    assets = tmp_path / "static"
    assets.mkdir(exist_ok=True)
    (assets / "assets").mkdir(exist_ok=True)
    (assets / "index.html").write_text('<div id="root"></div><script src="/assets/app.js"></script>')
    (assets / "assets" / "app.js").write_text("// test asset")
    if config is None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
    harnesses: list[Harness] = []

    def factory(config: HarnessConfig) -> Harness:
        harness = ControlledHarness(config) if controlled else Harness(config)
        harnesses.append(harness)
        return harness

    app = create_app(config, host=host, assets=assets, harness_factory=factory)
    assert not harnesses  # Construction happens on the lifespan's running loop.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as client,
    ):
        bootstrap = (await client.get("/api/bootstrap")).json()
        headers = {"Origin": base_url, "X-Ngn-Token": bootstrap["token"]}
        yield app, client, headers, harnesses
    assert harnesses[0]._closed


class LiveStream:
    """Drive streaming ASGI directly: HTTPX's test transport buffers until EOF."""

    def __init__(self, app: FastAPI, headers: dict[str, str], session_id: str, prompt: str, spec: str = "2.3") -> None:
        self.disconnected = False
        self.output: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.input: asyncio.Queue[Message] = asyncio.Queue()
        self.input.put_nowait(
            {"type": "http.request", "body": json.dumps({"session_id": session_id, "prompt": prompt}).encode()}
        )
        self.raw = bytearray()
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": spec},
            "method": "POST",
            "path": "/api/run",
            "raw_path": b"/api/run",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "http_version": "1.1",
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 10000),
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"content-type", b"application/json"),
                *((key.lower().encode(), value.encode()) for key, value in headers.items()),
            ],
        }
        self.task = asyncio.create_task(app(scope, self.input.get, self.send))

    async def send(self, message: Message) -> None:
        if self.disconnected:
            raise OSError("Client connection closed")
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body":
            self.raw.extend(message.get("body", b""))
            while b"\n" in self.raw:
                line, _, rest = self.raw.partition(b"\n")
                self.raw = bytearray(rest)
                self.output.put_nowait(json.loads(line))

    async def event(self, kind: str) -> dict[str, object]:
        async with asyncio.timeout(HANG_GUARD):
            while True:
                event = await self.output.get()
                if event["event"] == kind:
                    return event

    async def disconnect(self) -> None:
        self.disconnected = True
        await self.input.put({"type": "http.disconnect"})
        with suppress(ClientDisconnect):
            await asyncio.wait_for(self.task, HANG_GUARD)
