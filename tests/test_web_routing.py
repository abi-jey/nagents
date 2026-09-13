"""Ordered source admission across routing commands, model backlogs, and restart."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import pytest

from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.channels.types import ChannelActivity
from nagents.channels.types import ChannelMessage
from nagents.channels.types import ChannelSend
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from tests.test_web_channels import FakeChannel
from tests.test_web_channels import site

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.testclient import WebSocketTestSession

    from nagents.events import Event
    from nagents.types import Message
    from tests.test_subagents import FakeProvider
    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix


def target_root(app: Site) -> str:
    response = app.client.post("/api/sessions/new", headers=app.headers, json={})
    assert response.status_code == 200
    target = str(response.json()["session_id"])
    response = app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": app.main})
    assert response.status_code == 200
    return target


def deliver(app: Site, message: ChannelMessage) -> None:
    channel = app.state.channels.channels["fixture"]
    assert isinstance(channel, FakeChannel) and app.client.portal is not None
    app.client.portal.call(channel.emit, message)


def journal(app: Site) -> dict[str, dict[str, str]]:
    async def read() -> dict[str, dict[str, str]]:
        async with aiosqlite.connect(app.state.harness.agent.session.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT message_id, session_id, status, thread_id, reply_to FROM ngn_web_inbox "
                "WHERE channel = 'fixture' ORDER BY id"
            )
            return {
                str(row["message_id"]): {
                    key: str(row[key])
                    for key in (
                        "session_id",
                        "status",
                        "thread_id",
                        "reply_to",
                    )
                }
                for row in await cursor.fetchall()
            }

    assert app.client.portal is not None
    return cast("dict[str, dict[str, str]]", app.client.portal.call(read))


def users(app: Site, session_id: str) -> list[dict[str, object]]:
    return [row for row in cast("list[dict[str, object]]", app.history(session_id)["history"]) if row["role"] == "user"]


def approve_send(app: Site, socket: WebSocketTestSession, thread: str, reply: str) -> None:
    while True:
        frame = socket.receive_json()
        if frame.get("record", {}).get("event") == "approval":
            break
    record = frame["record"]
    assert record["tool"] == "channel_send"
    assert record["arguments"]["thread_id"] == thread and record["arguments"]["reply_to"] == reply
    response = app.client.post(
        "/api/approval",
        headers=app.headers,
        json={
            "run_id": record["run_id"],
            "approval_id": record["approval_id"],
            "call_id": record["id"],
            "decision": "allow",
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize("blocker", ["A", "other-web-run"])
def test_a_command_b_c_switches_at_admission_before_ack_and_preserves_reply_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        target = target_root(app)
        assert app.client.portal is not None
        started = app.client.portal.call(asyncio.Event)
        release = app.client.portal.call(asyncio.Event)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            content = str(next(message.content for message in reversed(messages) if message.role == "user"))
            if content == "block-current":
                started.set()
                yield TextChunkEvent(chunk="current web work")
                await release.wait()
                yield TextDoneEvent(text="web work finished")
                return
            source = cast("dict[str, str]", json.loads(content.removeprefix(_INBOUND_PREFIX)))
            if messages[-1].role == "tool":
                yield TextDoneEvent(text="answer " + source["message_id"])
                return
            if blocker == "A" and source["message_id"] == "A":
                started.set()
                yield TextChunkEvent(chunk="A still working")
                await release.wait()
            yield ToolCallEvent(
                id="send-" + source["message_id"],
                name="channel_send",
                arguments={
                    "channel": "fixture",
                    "destination": source["conversation_id"],
                    "text": "model " + source["message_id"],
                    "thread_id": source["thread_id"],
                    "reply_to": source["reply_to"],
                },
            )

        app.providers[0].script = script
        if blocker == "other-web-run":
            app.submit("block-current")
            app.client.portal.call(asyncio.wait_for, started.wait(), 5)
        deliver(app, ChannelMessage("A", "chat", "sender", "A", "thread-S", "reply-A"))
        old = app.bindings()["chat"]
        assert len({old, target, app.main}) == 3
        app.client.portal.call(asyncio.wait_for, started.wait(), 5)
        active = app.state.active
        assert active is not None
        selected = app.state.harness.session_id
        deliver(app, ChannelMessage("B", "chat", "sender", f"/session {target}", "command-thread", "reply-B"))
        assert app.bindings()["chat"] == target
        assert app.state.active is active and not active.task.done()
        assert app.state.harness.session_id == selected and app.state.selected_session_id == app.main
        channel = app.channels[0]
        assert not channel.deliveries  # B's user-facing acknowledgement is still behind the blocked LLM.
        if blocker == "A":
            assert channel.activities == [
                ChannelActivity("chat", True, "thread-S", old),
                ChannelActivity("chat", False, "thread-S", old),
            ]
        deliver(app, ChannelMessage("C", "chat", "sender", "C", "thread-T", "reply-C"))
        assert journal(app) == {
            "A": {
                "session_id": old,
                "status": "running" if blocker == "A" else "queued",
                "thread_id": "thread-S",
                "reply_to": "reply-A",
            },
            "B": {"session_id": target, "status": "queued", "thread_id": "command-thread", "reply_to": "reply-B"},
            "C": {"session_id": target, "status": "queued", "thread_id": "thread-T", "reply_to": "reply-C"},
        }
        with app.socket() as old_socket, app.socket() as target_socket:
            for socket, session_id in ((old_socket, old), (target_socket, target)):
                socket.send_json({"type": "subscribe", "session_id": session_id, "after": 0})
                assert socket.receive_json()["type"] == "snapshot"
            app.client.portal.call(release.set)
            approve_send(app, old_socket, "thread-S", "reply-A")
            approve_send(app, target_socket, "thread-T", "reply-C")
            app.idle()
        assert channel.deliveries == [
            ChannelSend("chat", "model A", "thread-S", "reply-A"),
            ChannelSend("chat", f"Session: {target}", "command-thread", "reply-B"),
            ChannelSend("chat", "model C", "thread-T", "reply-C"),
        ]
        assert [row["message_id"] for row in users(app, old)] == ["A"]
        assert [row["message_id"] for row in users(app, target)] == ["C"]
        assert all(row["status"] == "completed" for row in journal(app).values())
        assert app.state.selected_session_id == app.main


def test_admission_switch_and_frozen_A_C_targets_survive_restart_before_command_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        target = target_root(app)
        app.submit("hold")

        async def blocked() -> None:
            async with asyncio.timeout(5):
                while not app.providers[0].requests:
                    await asyncio.sleep(0.01)

        assert app.client.portal is not None
        app.client.portal.call(blocked)
        batch = (
            ChannelMessage("A", "chat", "sender", "A", "thread-S", "reply-A"),
            ChannelMessage("B", "chat", "sender", f"/session {target}", "command-thread", "reply-B"),
            ChannelMessage("C", "chat", "sender", "C", "thread-T", "reply-C"),
        )
        for message in batch:
            deliver(app, message)
        before = journal(app)
        old = before["A"]["session_id"]
        assert old != target and before["B"]["session_id"] == before["C"]["session_id"] == target
        assert all(row["status"] == "queued" for row in before.values())
        assert app.bindings()["chat"] == target and not app.channels[0].deliveries
    with site(tmp_path, monkeypatch) as app:
        app.idle()
        assert app.bindings()["chat"] == target
        assert [row["message_id"] for row in users(app, old)] == ["A"]
        assert [row["message_id"] for row in users(app, target)] == ["C"]
        assert journal(app) == {id: {**row, "status": "completed"} for id, row in before.items()}
        assert app.channels[0].deliveries == [ChannelSend("chat", f"Session: {target}", "command-thread", "reply-B")]
        assert app.channels[0].activities == [
            ChannelActivity("chat", True, "thread-T", target),
            ChannelActivity("chat", False, "thread-T", target),
        ]
        for message in batch:
            deliver(app, message)
        app.idle()
        assert len(app.providers[0].requests) == 2 and len(app.channels[0].deliveries) == 1
        deliver(app, ChannelMessage("D", "chat", "sender", "after restart", "thread-D", "reply-D"))
        app.idle()
        assert [row["message_id"] for row in users(app, target)] == ["C", "D"]
        assert [row["message_id"] for row in users(app, old)] == ["A"]


@pytest.mark.parametrize("target", ["ngn-hidden-child", "ngn-not-in-this-workspace"])
def test_session_command_cannot_bind_non_root_or_unknown_target_while_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        assert app.client.portal is not None
        app.client.portal.call(app.state.harness.agent.session.get_or_create_session, "ngn-hidden-child", "harness")
        app.submit("hold")
        app.emit("A", id="A")
        old = app.bindings()["chat-a"]
        app.emit(f"/session {target}", id="B")
        app.emit("C", id="C")
        assert app.bindings()["chat-a"] == old
        assert all(row["session_id"] == old for row in journal(app).values())
        assert app.state.selected_session_id == app.main
