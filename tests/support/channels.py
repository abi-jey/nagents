"""Channel transport test doubles shared by web channel suites."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from importlib import metadata
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi.testclient import TestClient

from nagents.channels.types import Channel
from nagents.channels.types import ChannelActivity
from nagents.channels.types import ChannelAttachment
from nagents.channels.types import ChannelCommand
from nagents.channels.types import ChannelDelivery
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelMessage
from nagents.channels.types import ChannelPlugin
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelValue
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.harness import runtime
from nagents.harness.config import HarnessConfig
from nagents.web.app import create_app
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    import pytest
    from starlette.testclient import WebSocketTestSession

    from nagents.channels.types import ChannelReceiver
    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.service import WebState


URL = "http://127.0.0.1:8765"


class FakeChannel(Channel):
    capabilities = ("receive", "send_text", "fetch_attachment")

    def __init__(self, name: str) -> None:
        self.name = name
        self.incoming: asyncio.Queue[tuple[ChannelMessage, asyncio.Future[None]]] = asyncio.Queue()
        self.deliveries: list[ChannelSend] = []
        self.activities: list[ChannelActivity] = []
        self.attachments: dict[str, tuple[bytes, str]] = {}
        self.closed = False

    async def fetch_attachment(self, attachment: ChannelAttachment) -> tuple[bytes, str]:
        result = self.attachments.get(attachment.reference)
        if result is None:
            raise ChannelError("Fixture attachment is unavailable")
        return result

    async def listen(self, receive: ChannelReceiver) -> None:
        while True:
            message, accepted = await self.incoming.get()
            await receive(message)
            accepted.set_result(None)

    async def emit(self, message: ChannelMessage) -> None:
        accepted = asyncio.get_running_loop().create_future()
        await self.incoming.put((message, accepted))
        await asyncio.wait_for(accepted, HANG_GUARD)

    def command(self, message: ChannelMessage) -> ChannelCommand | None:
        if message.text.startswith("/"):
            name, _, arguments = message.text[1:].partition(" ")
            return ChannelCommand(name, arguments)
        return None

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        self.deliveries.append(message)
        return ChannelDelivery((str(len(self.deliveries)),))

    async def activity(self, event: ChannelActivity) -> None:
        self.activities.append(event)

    async def close(self) -> None:
        self.closed = True


class Site:
    def __init__(
        self, client: TestClient, channels: list[FakeChannel], providers: list[FakeProvider], state: WebState
    ) -> None:
        self.client = client
        self.channels = channels
        self.providers = providers
        self.state = state
        self.token = client.get("/api/bootstrap").json()["token"]
        self.headers = {"Origin": URL, "X-Ngn-Token": self.token}
        self.main = self.state.selected_session_id

    def configure(self, id: str = "fixture", **values: object) -> dict[str, object]:
        before = self.client.get("/api/channels", headers=self.headers).json()
        response = self.client.put(
            f"/api/channels/{id}",
            headers=self.headers,
            json={
                "revision": before["revision"],
                "plugin": "fixture",
                "enabled": True,
                "config": {"label": "public"},
                "secrets": {"token": "test-only-secret"},
                **values,
            },
        )
        assert response.status_code == 200, response.text
        return cast("dict[str, object]", response.json())

    def emit(self, text: str, chat: str = "chat-a", id: str = "", thread: str = "") -> None:
        channel = self.state.channels.channels["fixture"]
        assert isinstance(channel, FakeChannel)
        assert self.client.portal is not None
        self.client.portal.call(
            channel.emit, ChannelMessage(id or uuid.uuid4().hex, chat, "sender", text, thread, "remote-id")
        )

    def idle(self) -> None:
        async def wait() -> None:
            async with asyncio.timeout(HANG_GUARD):
                while True:
                    pending = await self.state.channels.store.has_pending()
                    if not pending and self.state.active is None and not self.state.mutating:
                        return
                    await asyncio.sleep(0.01)

        assert self.client.portal is not None
        self.client.portal.call(wait)

    def bindings(self) -> dict[str, str]:
        response = self.client.get("/api/channels", headers=self.headers).json()
        return {item["conversation_id"]: item["session_id"] for item in response["bindings"]}

    def roots(self) -> dict[str, str]:
        sessions = cast("list[dict[str, str]]", self.history(self.main)["sessions"])
        return {session["id"]: session["title"] for session in sessions}

    def history(self, session: str) -> dict[str, object]:
        response = self.client.get(f"/api/sessions/{session}", headers=self.headers)
        assert response.status_code == 200, response.text
        return cast("dict[str, object]", response.json())

    def submit(self, prompt: str, session: str = "", id: str = "") -> dict[str, str]:
        response = self.client.post(
            "/api/messages",
            headers=self.headers,
            json={
                "session_id": session or self.main,
                "prompt": prompt,
                "message_id": id or str(uuid.uuid4()),
            },
        )
        assert response.status_code == 200, response.text
        return cast("dict[str, str]", response.json())

    def socket(self) -> WebSocketTestSession:
        return self.client.websocket_connect(
            URL.replace("http:", "ws:") + "/api/events",
            headers={"Origin": URL},
            subprotocols=["ngn.events.v1", f"ngn.token.{self.token}"],
        )


@contextmanager
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    channels: list[FakeChannel] = []
    providers: list[FakeProvider] = []

    def factory(config: dict[str, ChannelValue]) -> Channel:
        if config.get("label") == "invalid":
            raise RuntimeError(str(config.get("token")))
        channel = FakeChannel(str(config["name"]))
        channels.append(channel)
        return channel

    descriptor = ChannelPlugin(
        "Fixture",
        "Fake transport",
        {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "token": {"type": "string", "writeOnly": True},
            },
            "additionalProperties": False,
        },
        factory,
    )
    point = metadata.EntryPoint(name="fixture", value="fixture:plugin", group="nagents.channels")
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: metadata.EntryPoints((point,)))
    monkeypatch.setattr(metadata.EntryPoint, "load", lambda self: descriptor)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        content = str(next(message.content for message in reversed(messages) if message.role == "user"))
        if "hold" in content:
            yield TextChunkEvent(chunk="unfinished draft")
            await asyncio.Event().wait()
        elif content == "privilege" and messages[-1].role != "tool":
            yield ToolCallEvent(
                id="outbound",
                name="channel_send",
                arguments={
                    "channel": "fixture",
                    "destination": "chat-a",
                    "text": "approved outbound",
                },
            )
        else:
            yield TextChunkEvent(chunk="answer")
            yield TextDoneEvent(text="answer")

    def provider(config: HarnessConfig, login_store: object | None = None) -> FakeProvider:
        fake = FakeProvider(config, len(providers), script)
        providers.append(fake)
        return fake

    monkeypatch.setattr(runtime, "HarnessProvider", provider)
    assets = tmp_path / "static"
    (assets / "assets").mkdir(parents=True, exist_ok=True)
    (assets / "index.html").write_text("fixture")
    config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key")

    def harness(config: HarnessConfig) -> Harness:
        instance = Harness(config)
        instance.agent.compactor = None
        return instance

    app = create_app(config, assets=assets, harness_factory=harness)
    with TestClient(app, base_url=URL) as client:
        yield Site(client, channels, providers, app.state.web)
    assert all(channel.closed for channel in channels if channel in app.state.web.channels.channels.values())
    assert all(provider.closed for provider in providers)
    assert app.state.web.active is None
    assert all(task.done() for task in app.state.web.channels.tasks)
