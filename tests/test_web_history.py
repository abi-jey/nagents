"""Authoritative row/ingress identity, spoof resistance, and commit-race coverage."""

from __future__ import annotations

import asyncio
import uuid
from threading import Event as ThreadEvent
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import pytest

from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import AgentPlugin
from nagents.types import Message
from tests.test_web_channels import site
from tests.test_web_routing import users

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from nagents.events import Event
    from nagents.extensions import RunContext
    from nagents.types import ContentPart
    from tests.test_subagents import FakeProvider
    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix


def received(app: Site) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for frame, _ in app.state.bus.ring:
        record = frame.get("record")
        if isinstance(record, dict) and record.get("event") == "user_message":
            result.append(record)
    return result


def sql(app: Site, statement: str) -> None:
    async def execute() -> None:
        async with aiosqlite.connect(app.state.harness.agent.session.db_path) as db:
            await db.execute(statement)
            await db.commit()

    assert app.client.portal is not None
    app.client.portal.call(execute)


def test_channel_web_and_unlinked_identical_envelopes_have_authoritative_distinct_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Transport message IDs aren't globally unique. Even the same ID and exact
    # same stored text cannot turn a web/OOB message into a channel-origin row.
    shared_id = str(uuid.uuid4())
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("real channel input", id=shared_id, thread="source-thread")
        app.idle()
        session_id = app.bindings()["chat-a"]
        assert app.client.portal is not None
        messages = cast(
            "list[Message]", app.client.portal.call(app.state.harness.agent.session.get_history, session_id)
        )
        original = messages[0].content
        assert isinstance(original, str) and original.startswith(_INBOUND_PREFIX)
        app.submit(original, session=session_id, id=shared_id)
        app.idle()
        app.client.portal.call(
            app.state.harness.agent.session.add_message, session_id, Message(role="user", content=original)
        )
        rows = users(app, session_id)
        assert len(rows) == 3 and len({row["history_id"] for row in rows}) == 3
        channel, web, unlinked = rows
        assert channel["source_verified"] is True and channel["content"] == "real channel input"
        assert cast("dict[str, object]", channel["source"])["version"] == 1
        assert cast("dict[str, object]", channel["source"])["channel"] == "fixture"
        assert web["source_verified"] is True and web["content"] == original
        assert "source" not in web
        assert channel["message_id"] == web["message_id"] == shared_id
        assert channel["ingress_id"] != web["ingress_id"]
        assert unlinked["source_verified"] is False and "source" not in unlinked
        assert unlinked["content"] == original and unlinked["message_id"] == unlinked["ingress_id"] == ""
        saved = cast("list[Message]", app.client.portal.call(app.state.harness.agent.session.get_history, session_id))
        assert [message.content for message in saved if message.role == "user"] == [original] * 3
        assert len(received(app)) == 2
    with site(tmp_path, monkeypatch) as app:
        app.idle()
        assert users(app, session_id) == rows
        assert not app.providers[0].requests


def test_identical_web_submissions_reconcile_by_committed_ids_across_ws_snapshot_and_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app, app.socket() as socket:
        socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
        assert socket.receive_json()["type"] == "snapshot"
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for id in ids:
            app.submit("identical text", id=id)
        events: list[dict[str, object]] = []
        while len(events) < 2:
            frame = socket.receive_json()
            record = frame.get("record", {})
            if record.get("event") != "user_message":
                continue
            events.append(record)
            # Receipt cannot race ahead of durable message/association visibility.
            row = next(row for row in users(app, app.main) if row["history_id"] == record["history_id"])
            for key in ("history_id", "message_id", "ingress_id", "source_verified", "source", "content"):
                assert row.get(key) == record.get(key) if key == "source" else row[key] == record[key]
            assert record["text"] == "identical text" and record["source_verified"] is True
        app.idle()
        rows = users(app, app.main)
        assert [row["message_id"] for row in rows] == ids
        assert len({row["history_id"] for row in rows}) == len({row["ingress_id"] for row in rows}) == 2
        app.submit("identical text", id=ids[0])
        app.idle()
        assert users(app, app.main) == rows and len(received(app)) == 2
        with app.socket() as reconnect:
            reconnect.send_json({"type": "subscribe", "session_id": app.main, "after": 0, "epoch": "stale"})
            snapshot = reconnect.receive_json()["snapshot"]
            assert [row for row in snapshot["history"] if row["role"] == "user"] == rows


def test_pre_run_hook_write_cannot_steal_ingress_identity_and_rewrites_keep_real_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()

        class Rewrite(AgentPlugin):
            async def before_run(self, context: RunContext, message: Message) -> Message:
                await context.agent.session.add_message(context.session_id, message)
                return Message(role="user", content="wrapped: " + str(message.content))

        # Configured hooks precede the web adapter's final input identity capture.
        app.state.harness.agent.plugins.insert(0, Rewrite())
        app.emit("input", id="source-event")
        app.idle()
        rows = users(app, app.bindings()["chat-a"])
        assert len(rows) == 2
        assert rows[0]["source_verified"] is False and rows[0]["message_id"] == ""
        assert rows[1]["source_verified"] is True and rows[1]["message_id"] == "source-event"
        assert rows[1]["content"] == "input"
        assert str(rows[1]["stored_content"]).startswith("wrapped: " + _INBOUND_PREFIX)
        assert cast("dict[str, object]", rows[1]["source"])["text"] == "input"
        assert len(received(app)) == 1 and received(app)[0]["history_id"] == rows[1]["history_id"]


def test_unlinked_compaction_copies_and_duplicate_legacy_rows_are_not_reidentified_by_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("input", id="source-event")
        app.idle()
        session_id = app.bindings()["chat-a"]
        before = users(app, session_id)[0]
        assert before["source_verified"] is True
        assert app.client.portal is not None
        messages = cast(
            "list[Message]", app.client.portal.call(app.state.harness.agent.session.get_history, session_id)
        )
        replacement = [Message(role="compaction_summary", content="summary"), messages[0], messages[0]]
        app.client.portal.call(app.state.harness.agent.session.replace_context, session_id, replacement)
        rows = users(app, session_id)
        assert len(rows) == 2 and len({row["history_id"] for row in rows}) == 2
        assert all(row["history_id"] != before["history_id"] for row in rows)
        assert all(row["source_verified"] is False and "source" not in row and row["message_id"] == "" for row in rows)
        assert [row["content"] for row in rows] == [messages[0].content] * 2


def test_unmatched_initial_input_does_not_lend_origin_to_background_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        calls = 0

        async def transform(content: str | list[ContentPart] | None) -> str | list[ContentPart] | None:
            nonlocal calls
            calls += 1
            return "processed initial input" if calls == 1 else content

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "child work"})
            else:
                yield TextDoneEvent(text="finished")

        monkeypatch.setattr(app.state.harness.agent, "_process_multimodal_content", transform)
        app.providers[0].script = script
        app.emit("original input", id="original-ingress")
        app.idle()
        rows = users(app, app.bindings()["chat-a"])
        assert len(rows) > 1 and rows[0]["content"] == "processed initial input"
        assert any(str(row["content"]).startswith("BACKGROUND TASK NOTIFICATION:") for row in rows[1:])
        assert all(row["source_verified"] is False and row["message_id"] == "" for row in rows)
        assert received(app) == []


def test_history_and_origin_association_roll_back_together_without_receipt_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with site(tmp_path, monkeypatch) as app:
        sql(
            app,
            "CREATE TRIGGER reject_origin BEFORE INSERT ON ngn_web_message_origins BEGIN SELECT RAISE(ABORT, 'SECRET-origin-write'); END",
        )
        app.submit("must roll back")
        app.idle()
        assert users(app, app.main) == [] and received(app) == []
        assert not app.providers[0].requests
        assert "SECRET-origin-write" not in repr(app.state.bus.ring) + caplog.text
        sql(app, "DROP TRIGGER reject_origin")
        accepted = app.submit("fresh submission")
        app.idle()
        assert users(app, app.main)[0]["message_id"] == accepted["message_id"]
        assert len(received(app)) == len(app.providers[0].requests) == 1


def test_cancellation_racing_commit_keeps_atomic_identity_and_does_not_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        reached = ThreadEvent()
        release = ThreadEvent()
        transaction = app.state.history.store._transaction

        async def delayed(
            operation: Callable[[sqlite3.Connection], tuple[int, dict[str, object]]],
        ) -> tuple[int, dict[str, object]]:
            def blocked(db: sqlite3.Connection) -> tuple[int, dict[str, object]]:
                result = operation(db)
                reached.set()
                assert release.wait(5)
                return result

            return await transaction(blocked)

        monkeypatch.setattr(app.state.history.store, "_transaction", delayed)
        accepted = app.submit("commit race")
        try:
            assert reached.wait(5)
            active = app.state.active
            assert active is not None and app.client.portal is not None
            stopping = app.client.portal.start_task_soon(app.state.stop, active)

            async def cancelling() -> None:
                async with asyncio.timeout(5):
                    while not active.task.cancelling():
                        await asyncio.sleep(0.01)

            app.client.portal.call(cancelling)
        finally:
            release.set()
        stopping.result(timeout=5)
        app.idle()
        rows = users(app, app.main)
        assert len(rows) == 1 and rows[0]["message_id"] == accepted["message_id"]
        assert rows[0]["source_verified"] is True and len(received(app)) == 1
        assert not app.providers[0].requests
        app.submit("commit race", id=accepted["message_id"])
        app.idle()
        assert users(app, app.main) == rows and len(received(app)) == 1 and not app.providers[0].requests
