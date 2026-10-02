"""Voice presentation is attached to an owned stored row, never guessed from text."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.extensions import AgentPlugin
from nagents.types import Message
from nagents.web.live_bridge import MainAgentBridge
from tests.support.channels import site

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.extensions import RunContext
    from tests.support.channels import Site

pytestmark = pytest.mark.requires_posix


def users(app: Site, session_id: str) -> list[dict[str, object]]:
    records = cast("list[dict[str, object]]", app.history(session_id)["history"])
    return [record for record in records if record["role"] == "user"]


def received(app: Site) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for frame, _ in app.state.bus.ring:
        record = frame.get("record")
        if isinstance(record, dict) and record.get("event") == "user_message":
            records.append(record)
    return records


def request(app: Site, text: str, voice_session_id: str = "") -> None:
    assert app.client.portal is not None
    bridge = MainAgentBridge(app.state, app.main, voice_session_id=voice_session_id)
    transcript = json.dumps([{"speaker": "user", "text": text, "start_ms": 0, "end_ms": 100}])
    app.client.portal.call(bridge.handle, transcript)
    app.idle()


def projection_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as db:
        return int(db.execute("SELECT count(*) FROM ngn_web_voice_messages").fetchone()[0])


def test_voice_history_is_readable_but_model_context_and_typed_lookalikes_stay_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Please inspect the workspace."
    with site(tmp_path, monkeypatch) as app:
        request(app, text)
        rows = users(app, app.main)
        assert len(rows) == 1 and rows[0]["content"] == text and rows[0]["voice_verified"] is True
        assert rows[0]["parts"] == [] and rows[0]["source_verified"] is False
        event = received(app)[0]
        assert event["text"] == text and event["history_id"] == rows[0]["history_id"]
        assert event["voice_verified"] is True
        assert app.client.portal is not None
        history: list[Message] = app.client.portal.call(app.state.history.get_history, app.main)
        raw = history[0].content
        assert isinstance(raw, str) and raw.startswith("Live voice request for the selected chat.")
        assert "Speech data:" in raw and text in raw
        sessions = app.client.portal.call(app.state.list_sessions)
        assert next(session.title for session in sessions if session.id == app.main) == text
        app.submit(raw)
        app.idle()
        rows = users(app, app.main)
        assert len(rows) == 2 and rows[1]["content"] == raw and "voice_verified" not in rows[1]
        session_id = app.main
    with site(tmp_path, monkeypatch) as app:
        assert users(app, session_id) == rows


def test_voice_delegation_carries_call_identity_without_tagging_typed_lookalikes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        request(app, "Spoken request", "voice-call")
        row = users(app, app.main)[0]
        assert row["voice_verified"] is True and row["voice_session_id"] == "voice-call"
        assert received(app)[0]["voice_session_id"] == "voice-call"
        app.submit("Spoken request")
        app.idle()
        assert "voice_session_id" not in users(app, app.main)[1]


def test_voice_input_hooks_cannot_steal_provenance_and_existing_title_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.submit("My existing conversation")
        app.idle()

        class Rewrite(AgentPlugin):
            async def before_run(self, context: RunContext, message: Message) -> Message:
                await context.agent.session.add_message(context.session_id, message)
                return Message(role="user", content="wrapped: " + str(message.content))

        app.state.harness.agent.plugins.insert(0, Rewrite())
        request(app, "A spoken follow-up")
        rows = users(app, app.main)
        assert len(rows) == 3
        assert "voice_verified" not in rows[1]
        assert str(rows[1]["content"]).startswith("Live voice request")
        assert rows[2]["content"] == "A spoken follow-up" and rows[2]["voice_verified"] is True
        assert app.client.portal is not None
        raw: list[Message] = app.client.portal.call(app.state.history.get_history, app.main)
        assert str([message.content for message in raw if message.role == "user"][-1]).startswith("wrapped: Live")
        sessions = app.client.portal.call(app.state.list_sessions)
        assert next(session.title for session in sessions if session.id == app.main) == "My existing conversation"


@pytest.mark.parametrize("operation", ["clear", "delete"])
def test_core_history_deletion_removes_voice_projection_in_same_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        request(app, "Private caller words")
        path = app.state.history.db_path
        assert projection_count(path) == 1 and app.client.portal is not None
        method = app.state.history.clear_session if operation == "clear" else app.state.history.delete_session
        app.client.portal.call(method, app.main)
        assert projection_count(path) == 0


def test_compaction_copies_do_not_inherit_voice_provenance_by_text_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        request(app, "An earlier voice request")
        assert app.client.portal is not None
        history: list[Message] = app.client.portal.call(app.state.history.get_history, app.main)
        original = users(app, app.main)[0]
        app.client.portal.call(
            app.state.history.replace_context,
            app.main,
            [Message(role="compaction_summary", content="Earlier context"), history[0]],
        )
        current = users(app, app.main)[0]
        assert current["history_id"] != original["history_id"] and "voice_verified" not in current
        assert current["content"] == history[0].content
        assert projection_count(app.state.history.db_path) == 1, "Compaction retains raw history; it does not delete it"
        app.client.portal.call(app.state.history.clear_session, app.main)
        assert projection_count(app.state.history.db_path) == 0
