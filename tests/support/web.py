"""Loopback app/client helpers shared by web suites."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
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

    def load_project_instructions(self) -> None:
        """This portable scripted client never invokes guarded workspace tools."""

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
    if (
        controlled
        and not config.demo
        and not config.provider
        and config.provider_profile().kind in {"openai", "openai_compatible"}
    ):
        store = ScopedProviderRegistryStore(config.workspace, paths=config.provider_paths)
        registry = store.load()
        if registry.active:
            config.provider = registry.active
            config.providers = dict(registry.providers)
        else:
            selected = config.provider_profile()
            store.workspace_store.save(
                ProviderRegistry(active="fixture", providers={"fixture": selected}),
                expected=store.workspace_store.load().revision,
            )
            config.provider = "fixture"
            config.providers = {"fixture": selected}
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
    """Subscribe to durable session events, then submit through the public message API."""

    def __init__(self, app: FastAPI, headers: dict[str, str], session_id: str, prompt: str, spec: str = "2.3") -> None:
        self.app = app
        self.headers = headers
        self.session_id = session_id
        self.prompt = prompt
        self.message_id = str(uuid.uuid4())
        self.disconnected = False
        self.output: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.input: asyncio.Queue[Message] = asyncio.Queue()
        self.input.put_nowait({"type": "websocket.connect"})
        self.accepted = asyncio.Event()
        self.subscribed = asyncio.Event()
        self.admitted = asyncio.Event()
        scope: Scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": spec},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 10000),
            "root_path": "",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"sec-websocket-protocol", f"ngn.events.v1, ngn.token.{headers['X-Ngn-Token']}".encode()),
                *((key.lower().encode(), value.encode()) for key, value in headers.items()),
            ],
            "subprotocols": ["ngn.events.v1", f"ngn.token.{headers['X-Ngn-Token']}"],
        }
        self.socket_task = asyncio.create_task(app(scope, self.input.get, self.send))
        self._run_task: asyncio.Task[None] | None = None
        self.start_task = asyncio.create_task(self.start())

    async def send(self, message: Message) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            self.accepted.set()
        elif kind == "websocket.send":
            text = message.get("text")
            if text:
                frame = json.loads(text)
                if frame.get("type") == "event":
                    record = frame["record"]
                    if record.get("event") == "run_started" and record.get("message_id") == self.message_id:
                        self.admitted.set()
                    self.output.put_nowait(record)
                elif frame.get("type") == "snapshot":
                    # The subscription handshake is not a public event. A test
                    # may already be consuming output while start() is waiting.
                    self.subscribed.set()
        elif kind == "websocket.close":
            self.disconnected = True

    async def start(self) -> None:
        await self.accepted.wait()
        await self.input.put(
            {"type": "websocket.receive", "text": json.dumps({"type": "subscribe", "session_id": self.session_id})}
        )
        await asyncio.wait_for(self.subscribed.wait(), HANG_GUARD)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=URL) as client:
            response = await client.post(
                "/api/messages",
                headers=self.headers,
                json={
                    "session_id": self.session_id,
                    "prompt": self.prompt,
                    "message_id": self.message_id,
                },
            )
        assert response.status_code == 200, response.text
        await asyncio.wait_for(self.admitted.wait(), HANG_GUARD)
        run = self.app.state.web.run_for(self.session_id)
        assert run is not None and run.message_id == self.message_id
        self._run_task = run.task

    @property
    def task(self) -> asyncio.Task[None]:
        assert self._run_task is not None, "The durable message has not been admitted yet"
        return self._run_task

    async def event(self, kind: str) -> dict[str, object]:
        await self.start_task
        async with asyncio.timeout(HANG_GUARD):
            while True:
                event = await self.output.get()
                if event.get("event") == kind:
                    return event

    async def disconnect(self) -> None:
        await self.start_task
        self.disconnected = True
        await self.input.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.socket_task, HANG_GUARD)

    async def run_to_completion(self) -> list[dict[str, object]]:
        await self.start_task
        events: list[dict[str, object]] = []
        async with asyncio.timeout(HANG_GUARD):
            while True:
                event = await self.output.get()
                events.append(event)
                if event.get("event") == "run_finished":
                    channels = self.app.state.web.channels
                    await channels.join_claim_cleanup(self.session_id)
                    async with asyncio.timeout(HANG_GUARD):
                        while True:
                            channels.changed.clear()
                            if (
                                self.session_id not in channels.work_tasks
                                and self.session_id not in channels.work_cleanup
                                and self.app.state.web.run_for(self.session_id) is None
                            ):
                                return events
                            await channels.changed.wait()


async def submit_and_wait(
    app: FastAPI, headers: dict[str, str], session_id: str, prompt: str
) -> list[dict[str, object]]:
    """Submit durable work and collect its authenticated session events through completion."""
    stream = LiveStream(app, headers, session_id, prompt)
    events = await stream.run_to_completion()
    await stream.disconnect()
    return events
