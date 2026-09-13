"""Real HTTP/WS, SQLite, and Harness execution with transport/provider fakes only."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import uuid
from contextlib import contextmanager
from importlib import metadata
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nagents.channels.types import Channel
from nagents.channels.types import ChannelActivity
from nagents.channels.types import ChannelCommand
from nagents.channels.types import ChannelDelivery
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
from nagents.types import Message
from nagents.web import catalog
from nagents.web.app import create_app
from nagents.web.service import Run
from nagents.web.subscriptions import EventBus
from nagents.web.subscriptions import Subscriber
from tests.test_subagents import FakeProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.testclient import WebSocketTestSession

    from nagents.channels.types import ChannelReceiver
    from nagents.events import Event
    from nagents.web.service import WebState

URL = "http://127.0.0.1:8765"
pytestmark = pytest.mark.requires_posix


class FakeChannel(Channel):
    def __init__(self, name: str) -> None:
        self.name = name
        self.incoming: asyncio.Queue[tuple[ChannelMessage, asyncio.Future[None]]] = asyncio.Queue()
        self.deliveries: list[ChannelSend] = []
        self.activities: list[ChannelActivity] = []
        self.closed = False

    async def listen(self, receive: ChannelReceiver) -> None:
        while True:
            message, accepted = await self.incoming.get()
            await receive(message)
            accepted.set_result(None)

    async def emit(self, message: ChannelMessage) -> None:
        accepted = asyncio.get_running_loop().create_future()
        await self.incoming.put((message, accepted))
        await asyncio.wait_for(accepted, 5)

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
            async with asyncio.timeout(10):
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

    def provider(config: HarnessConfig) -> FakeProvider:
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


def test_chat_roots_main_reattachment_history_and_typing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("first", thread="thread-a")
        app.emit("second", "chat-b")
        app.idle()
        bindings = app.bindings()
        assert len({*bindings.values(), app.main}) == 3
        assert app.state.harness.session_id == app.main == app.state.selected_session_id
        first = app.history(bindings["chat-a"])
        assert [message["content"] for message in cast("list[dict[str, object]]", first["history"])] == [
            "first",
            "answer",
        ]
        assert first["history"][0]["source"]["conversation_id"] == "chat-a"  # type: ignore[index]
        assert not app.channels[0].deliveries  # Final model text is local only.
        assert [
            (event.active, event.thread_id) for event in app.channels[0].activities if event.conversation_id == "chat-a"
        ] == [(True, "thread-a"), (False, "thread-a")]
        app.emit("/session main")
        app.idle()
        assert app.bindings()["chat-a"] == app.main
        assert app.channels[0].deliveries[-1].text == f"Session: {app.main}"
        app.submit("web origin")
        app.idle()
        assert app.channels[0].activities[-2:] == [
            ChannelActivity("chat-a", True, session_id=app.main),
            ChannelActivity("chat-a", False, session_id=app.main),
        ]
        app.emit("/session default")
        app.idle()
        assert app.bindings()["chat-a"] == bindings["chat-a"]
        app.emit("/sessions")
        app.idle()
        assert bindings["chat-b"] in app.channels[0].deliveries[-1].text


def test_frozen_pending_binding_command_dedup_and_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message_id = str(uuid.uuid4())
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.submit("hold", id=message_id)
        app.emit("pending", id="pending-id")
        before = app.bindings()["chat-a"]
        app.emit("/session main", id="attach-id")
        assert app.bindings()["chat-a"] == app.main
        before_new = app.roots()
        app.emit("/new New title", id="new-id")
        new = app.bindings()["chat-a"]
        after_new = app.roots()
        assert set(after_new) - before_new.keys() == {new}
        app.emit("/new New title", id="new-id")
        assert app.bindings()["chat-a"] == new and app.roots() == after_new
        app.submit("hold", id=message_id)
        main = app.main
    with site(tmp_path, monkeypatch) as app:
        app.idle()
        restarted_roots = app.roots()
        assert list(restarted_roots.values()).count("New title") == 1
        assert app.state.channels.catalog.connections["fixture"].main_session_id == main
        assert len(app.providers[0].requests) == 1
        assert [row["content"] for row in cast("list[dict[str, object]]", app.history(before)["history"])] == [
            "pending",
            "answer",
        ]
        app.emit("/new New title", id="new-id")
        app.emit("pending", id="pending-id")
        app.idle()
        assert app.bindings()["chat-a"] == new and len(app.providers[0].requests) == 1
        assert app.roots() == restarted_roots
        assert app.history(main)["history"]  # Interrupted user input retained, not replayed.


def test_ws_server_owned_disconnect_draft_cancel_and_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        with app.socket() as socket:
            assert socket.accepted_subprotocol == "ngn.events.v1"
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            initial = socket.receive_json()
            assert initial["type"] == "snapshot"
            ack = app.submit("hold")
            while True:
                frame = socket.receive_json()
                if frame.get("record", {}).get("event") == "text_chunk":
                    break
            assert frame["record"]["chunk"] == "unfinished draft"
            run_id = frame["record"]["run_id"]
        assert app.state.active is not None and app.state.active.id == run_id
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0, "epoch": "old-process"})
            snapshot = socket.receive_json()["snapshot"]
            assert snapshot["active_run"]["message_id"] == ack["message_id"]
            assert snapshot["active_run"]["records"][0]["chunk"] == "unfinished draft"
            settings = app.client.get("/api/settings", headers=app.headers).json()
            assert (
                app.client.post(
                    "/api/settings",
                    headers=app.headers,
                    json={"revision": settings["revision"], "values": settings["values"]},
                ).status_code
                == 409
            )
            assert app.client.post("/api/channels/refresh", headers=app.headers, json={}).status_code == 409
            assert (
                app.client.post(
                    "/api/dictation/transcribe",
                    headers={**app.headers, "Content-Type": "audio/wav"},
                    content=b"invalid",
                ).status_code
                == 409
            )
            assert app.client.post("/api/cancel", headers=app.headers, json={"run_id": run_id}).status_code == 200
        app.idle()


@pytest.mark.parametrize("bad", ["origin", "host", "missing", "duplicate", "query", "protocol", "token"])
def test_ws_rejects_before_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    with site(tmp_path, monkeypatch) as app:
        headers = {"Origin": URL}
        protocols = ["ngn.events.v1", f"ngn.token.{app.token}"]
        path = URL.replace("http:", "ws:") + "/api/events"
        if bad == "origin":
            headers["Origin"] = "http://evil.test"
        elif bad == "host":
            headers["Host"] = "localhost:8765"
        elif bad == "missing":
            headers = {}
        elif bad == "duplicate":
            protocols.append(protocols[-1])
        elif bad == "query":
            path += "?token=never-allowed"
        elif bad == "protocol":
            protocols[0] = "wrong"
        else:
            protocols[-1] = "ngn.token.wrong"
        with (
            pytest.raises(WebSocketDisconnect) as error,
            app.client.websocket_connect(path, headers=headers, subprotocols=protocols),
        ):
            pytest.fail("Unauthenticated socket accepted")
        assert error.value.code == 1008


@pytest.mark.parametrize(
    "frame",
    [
        "{}",
        '{"type":"subscribe","session_id":"ngn-unknown"}',
        '{"type":"subscribe","type":"unsubscribe","session_id":"ngn-any"}',
        "x" * 4097,
    ],
)
def test_ws_invalid_frames_and_root_membership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: str) -> None:
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        socket.send_text(frame)
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008


def test_ws_out_of_band_history_and_replay_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            initial = socket.receive_json()
            assert app.client.portal is not None
            app.client.portal.call(
                app.state.harness.agent.session.add_message, app.main, Message(role="user", content="outside writer")
            )
            while True:
                frame = socket.receive_json()
                if frame["type"] == "snapshot" and frame["snapshot"]["history"]:
                    break
            assert frame["snapshot"]["history"][-1]["content"] == "outside writer"
        with app.socket() as socket:
            socket.send_json(
                {"type": "subscribe", "session_id": app.main, "after": initial["cursor"], "epoch": initial["epoch"]}
            )
            assert socket.receive_json()["cursor"] > initial["cursor"]
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 2**52, "epoch": initial["epoch"]})
            assert socket.receive_json()["type"] == "snapshot"


def test_bus_is_bounded_and_slow_consumer_does_not_block() -> None:
    bus = EventBus()
    slow = Subscriber(session_id="ngn-root", ready=True)
    bus.subscribers.add(slow)
    for index in range(2000):
        bus.event({"session_id": "ngn-root", "event": "text_chunk", "chunk": "x" * 4096, "n": index})
    assert slow.lagged and not bus.listening("ngn-root")
    assert slow.queue.qsize() == 1 and bus.bytes <= 2 * 1024 * 1024 and len(bus.ring) <= 1024
    assert bus.discarded > 0 and bus.cursor == 2000


def test_reconnect_checkpoint_keeps_task_activation_and_call_scopes_distinct() -> None:
    run = Run("ngn-root")
    run.remember({"event": "text_chunk", "chunk": "root"})
    child = {"task_id": "child", "activation": 2}
    run.remember({"event": "text_chunk", "extra": child, "chunk": "child draft"})
    run.remember({"event": "tool_call", "id": "same-call", "name": "shell"})
    run.remember({"event": "tool_output", "call_id": "same-call", "text": "root output"})
    run.remember({"event": "tool_call", **child, "id": "same-call", "name": "shell"})
    run.remember({"event": "tool_output", **child, "call_id": "same-call", "text": "child output"})
    run.remember({"event": "tool_result", "id": "same-call"})
    records = cast("list[dict[str, object]]", run.snapshot()["records"])
    assert [record.get("chunk") for record in records if record["event"] == "text_chunk"] == ["root", "child draft"]
    outputs = [record for record in records if record["event"] == "tool_output"]
    assert len(outputs) == 1 and outputs[0]["text"] == "child output" and outputs[0]["activation"] == 2
    run.remember({"event": "text_done", "extra": child, "text": "child final now in history"})
    assert [record["chunk"] for record in run.drafts.values() if record["event"] == "text_chunk"] == ["root"]


def test_large_root_history_can_still_subscribe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        assert app.client.portal is not None
        text = "persisted " * 250000
        app.client.portal.call(
            app.state.harness.agent.session.add_message, app.main, Message(role="user", content=text)
        )
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            snapshot = socket.receive_json()
            assert snapshot["type"] == "snapshot" and snapshot["snapshot"]["history"][0]["content"] == text


def test_config_secrets_atomic_revisions_and_error_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        before = app.client.get("/api/channels", headers=app.headers).json()
        assert before["plugins"][0]["schema"] == {"type": "object"}  # No descriptor imports at startup/GET.
        refreshed = app.client.post("/api/channels/refresh", json={}, headers=app.headers)
        assert refreshed.json()["plugins"][0]["schema"]["properties"]["token"]["writeOnly"]
        configured = app.configure()
        assert "test-only-secret" not in json.dumps(configured)
        path = app.state.channels.catalog.path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        with pytest.raises(PermissionError):
            app.state.harness.tools.relative(str(path))
        app.configure(secrets={})
        assert app.state.channels.catalog.connections["fixture"].secrets == {"token": "test-only-secret"}
        app.configure(secrets={"token": ""})
        assert app.state.channels.catalog.connections["fixture"].secrets == {}
        response = app.client.put(
            "/api/channels/fixture",
            headers=app.headers,
            json={
                "revision": app.state.channels.catalog.revision,
                "plugin": "fixture",
                "enabled": True,
                "config": {"label": "invalid"},
                "secrets": {"token": "never-return-this"},
            },
        )
        assert response.status_code == 422 and "never-return-this" not in response.text
        stale = app.client.request(
            "DELETE", "/api/channels/fixture", headers=app.headers, json={"revision": before["revision"]}
        )
        assert stale.status_code == 409
        response = app.client.request(
            "DELETE",
            "/api/channels/fixture",
            headers=app.headers,
            json={"revision": app.state.channels.catalog.revision},
        )
        assert response.status_code == 200 and response.json()["connections"] == []


def test_outbound_requires_matching_live_approval_subscriber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.submit("privilege")
        app.idle()
        assert not app.channels[0].deliveries
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            assert socket.receive_json()["type"] == "snapshot"
            app.submit("privilege")
            while True:
                frame = socket.receive_json()
                if frame.get("record", {}).get("event") == "approval":
                    break
            approval = frame["record"]
            result = app.client.post(
                "/api/approval",
                headers=app.headers,
                json={
                    "run_id": approval["run_id"],
                    "approval_id": approval["approval_id"],
                    "call_id": approval["id"],
                    "decision": "allow",
                },
            )
            assert result.status_code == 200
            app.idle()
        assert len(app.channels[0].deliveries) == 1 and app.channels[0].deliveries[0].text == "approved outbound"


def test_disconnect_denies_pending_approval_without_cancelling_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            socket.receive_json()
            app.submit("privilege")
            while True:
                frame = socket.receive_json()
                if frame.get("record", {}).get("event") == "approval":
                    break
        app.idle()
        assert not app.channels[0].deliveries
        assert len(app.providers[0].requests) == 2
        assert app.history(app.main)["history"][-1]["content"] == "answer"  # type: ignore[index]


def test_busy_selection_does_not_reroute_model_and_typing_stops_after_reattach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("hold", thread="source-thread")
        root = app.bindings()["chat-a"]

        async def started() -> None:
            async with asyncio.timeout(5):
                while not app.channels[0].activities:
                    await asyncio.sleep(0.01)

        assert app.client.portal is not None
        app.client.portal.call(started)
        run = app.state.active
        assert run is not None
        selected = app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": root})
        assert selected.status_code == 200 and app.state.selected_session_id == root
        assert app.state.harness.session_id == root
        app.emit("/session main")
        assert app.bindings()["chat-a"] == app.main
        assert app.channels[0].activities[-1] == ChannelActivity("chat-a", False, "source-thread", root)
        assert app.state.active is run and not run.task.done()
        assert app.client.post("/api/cancel", headers=app.headers, json={"run_id": run.id}).status_code == 200
        app.idle()
        assert app.state.selected_session_id == app.state.harness.session_id == root
        assert app.channels[0].activities[-1] == ChannelActivity("chat-a", False, "source-thread", root)
        assert app.history(root)["session_id"] == root


@pytest.mark.parametrize("origin", ["web", "other-connector"])
def test_activity_tracks_all_current_bindings_and_reattachment_during_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        for chat in ("chat-a", "chat-b"):
            app.emit("/session main", chat)
        app.idle()
        app.configure("other")
        other = app.state.channels.channels["other"]
        assert isinstance(other, FakeChannel)
        assert app.client.portal is not None
        app.client.portal.call(other.emit, ChannelMessage("attach", "other-room", "sender", "/session main"))
        app.idle()
        if origin == "web":
            app.submit("hold")
        else:
            app.client.portal.call(other.emit, ChannelMessage("work", "other-room", "sender", "hold"))

        async def started() -> None:
            async with asyncio.timeout(5):
                while len(app.channels[0].activities) < 2:
                    await asyncio.sleep(0.01)

        app.client.portal.call(started)
        run = app.state.active
        assert run is not None
        bot = app.channels[0]
        assert bot.activities == [ChannelActivity(chat, True, session_id=app.main) for chat in ("chat-a", "chat-b")]
        assert other.activities == [ChannelActivity("other-room", True, session_id=app.main)]
        app.emit("/session default", "chat-a")
        detached_root = app.bindings()["chat-a"]
        assert bot.activities[-1] == ChannelActivity("chat-a", False, session_id=app.main)
        assert app.state.active is run and not run.task.done()
        app.emit("/session main", "chat-a", id="reattach")
        assert bot.activities[-1] == ChannelActivity("chat-a", True, session_id=app.main)
        unchanged = list(bot.activities)
        app.emit("/session main", "chat-a", id="reattach")
        # Late cleanup for a different session cannot stop this session's indicator.
        app.client.portal.call(app.state.channels.activity, detached_root, False)
        assert bot.activities == unchanged
        app.emit("/session main", "chat-c")
        assert bot.activities[-1] == ChannelActivity("chat-c", True, session_id=app.main)
        assert app.client.post("/api/cancel", headers=app.headers, json={"run_id": run.id}).status_code == 200
        app.idle()
        assert bot.activities[-3:] == [
            ChannelActivity(chat, False, session_id=app.main) for chat in ("chat-b", "chat-a", "chat-c")
        ]
        assert other.activities[-1] == ChannelActivity("other-room", False, session_id=app.main)
        assert not app.state.channels.activities.current


@pytest.mark.parametrize("failure", ["exception", "timeout", "self-cancel", "bindings"])
def test_web_activity_control_failure_does_not_fail_model_or_expose_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    monkeypatch.setattr("nagents.channels.runtime.ACTIVITY_TIMEOUT", 0.01)
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("/session main")
        app.idle()
        controls = 0

        async def broken_indicator(channel: FakeChannel, event: ChannelActivity) -> None:
            nonlocal controls
            channel.activities.append(event)
            controls += 1
            try:
                if failure == "timeout":
                    await asyncio.Event().wait()
                elif failure == "self-cancel":
                    raise asyncio.CancelledError
                else:
                    raise RuntimeError("SECRET-activity-control")
            finally:
                controls -= 1

        async def broken_bindings() -> list[dict[str, str]]:
            raise RuntimeError("SECRET-activity-control")

        if failure == "bindings":
            monkeypatch.setattr(app.state.channels.store, "bindings", broken_bindings)
        else:
            monkeypatch.setattr(FakeChannel, "activity", broken_indicator)
        app.submit("finish normally")
        app.idle()
        snapshot = app.history(app.main)
        assert cast("list[dict[str, object]]", snapshot["history"])[-1]["content"] == "answer"
        assert len(app.providers[0].requests) == 1 and controls == 0
        assert "SECRET-activity-control" not in json.dumps(snapshot) + caplog.text
        if failure != "bindings":
            assert [event.active for event in app.channels[0].activities] == [True, False]


def test_real_ws_slow_subscriber_closes_without_blocking_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        socket.receive_json()

        def burst() -> None:
            for _ in range(100):
                app.state.bus.event({"session_id": app.main, "event": "notice", "text": "burst"})

        assert app.client.portal is not None
        app.client.portal.call(burst)
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1013


def test_other_chat_publishes_global_busy_without_switching_ui_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            socket.receive_json()
            app.emit("hold")
            while True:
                frame = socket.receive_json()
                if frame["type"] == "status" and frame["active_run_id"]:
                    break
            assert frame["active_session_id"] == app.bindings()["chat-a"] != app.main
            snapshot = app.history(app.main)
            assert snapshot["session_id"] == app.state.selected_session_id == app.main
            assert snapshot["active_run"] is None and snapshot["active_run_id"] == frame["active_run_id"]
            assert (
                app.client.post("/api/cancel", headers=app.headers, json={"run_id": frame["active_run_id"]}).status_code
                == 200
            )
        app.idle()


def test_dynamic_plugin_target_discovery_collisions_and_no_module_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_path = tmp_path / "installed-target"
    monkeypatch.setenv("NGN_CHANNEL_PLUGIN_PATH", str(plugin_path))
    instance = catalog.ChannelCatalog(tmp_path / "data", tmp_path / "data" / "workspace" / "sessions.db")
    instance.initialize()
    assert str(plugin_path) in sys.path
    module = "ngn_test_dynamic_" + uuid.uuid4().hex
    distribution = plugin_path / f"{module}-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {module}\nVersion: 1.0\n")
    (distribution / "entry_points.txt").write_text(f"[nagents.channels]\n{module} = {module}:plugin\n")
    module_path = plugin_path / f"{module}.py"
    module_path.write_text(
        "from nagents.channels.types import ChannelPlugin\nplugin = ChannelPlugin('New descriptor', 'Installed now', {'type':'object','properties':{'token':{'type':'string','writeOnly':True}}}, lambda config: None)\n"
    )
    instance.discover()
    assert module in instance.plugins and module not in sys.modules
    assert instance.plugins[module].schema == {"type": "object"}
    instance.discover(descriptors=True)
    assert module in sys.modules and instance.plugins[module].secret_fields() == {"token"}
    module_path.write_text("raise RuntimeError('upgrades require restart')\n")
    instance.discover(descriptors=True)
    assert instance.plugins[module].name == "New descriptor"
    duplicate = metadata.EntryPoint(name=module, value=f"{module}:plugin", group="nagents.channels")
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: metadata.EntryPoints((duplicate, duplicate)))
    instance.discover()
    with pytest.raises(HTTPException) as error:
        instance.plugin(module)
    assert error.value.status_code == 409
    sys.modules.pop(module, None)
    sys.path.remove(str(plugin_path))
