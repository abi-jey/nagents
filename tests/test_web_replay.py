"""Frontend-compatible live input, ordered cold replay, and hydration semantics."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import TextChunkEvent
from nagents.events import ToolCallEvent
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskStarted
from nagents.harness.types import ToolOutput
from nagents.web.replay import RunReplay
from nagents.web.service import Run
from tests.test_web_channels import site

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.test_subagents import FakeProvider
    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix


def capture_archive(app: Site) -> dict[str, object]:
    """Use real Harness/executor persistence with controlled queued event output."""

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="inspect", name="list_files", arguments={"limit": 1})
        else:
            await app.state.harness.emit(ToolOutput("inspect", "list_files", "first output\n"))
            await app.state.harness.emit(ToolOutput("inspect", "list_files", "second output\n"))
            await app.state.harness.emit(TaskStarted("helper", "Helper", "inspect", parent_session_id=app.main))
            await app.state.harness.emit(
                TaskCompleted("helper", "Helper", "finished child", "", parent_session_id=app.main)
            )
            yield TextChunkEvent(chunk="partial ")
            yield TextChunkEvent(chunk="draft")
            await asyncio.Event().wait()

    app.providers[0].script = script
    app.submit("show archive")

    async def ready() -> None:
        async with asyncio.timeout(5):
            while True:
                run = app.state.active
                if run is not None and any(record.get("chunk") == "partial draft" for record in run.drafts.values()):
                    return
                await asyncio.sleep(0.01)

    assert app.client.portal is not None
    app.client.portal.call(ready)
    return app.history(app.main)


def test_cold_snapshot_has_complete_ordered_user_tool_task_output_and_partial_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        snapshot = capture_archive(app)
        run = cast("dict[str, object]", snapshot["active_run"])
        events = cast("list[dict[str, object]]", run["events"])
        kinds = [record["event"] for record in events]
        assert run["session_id"] == snapshot["session_id"] == app.main
        assert isinstance(snapshot["retained_tasks"], list)
        assert kinds[0] == "run_started" and "user_message" in kinds
        assert "user_received" not in kinds and "message_received" not in kinds
        assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("tool_output")
        assert (
            kinds.index("tool_output")
            < kinds.index("task_started")
            < kinds.index("task_completed")
            < kinds.index("text_chunk")
        )
        assert [record["text"] for record in events if record["event"] == "tool_output"] == [
            "first output\nsecond output\n"
        ]
        assert [record["chunk"] for record in events if record["event"] == "text_chunk"] == ["partial draft"]
        assert all(record["run_id"] == run["id"] and record["session_id"] == app.main for record in events)
        message = next(record for record in events if record["event"] == "user_message")
        assert message["text"] == "show archive" and "source" not in message
        for row in cast("list[dict[str, object]]", snapshot["history"]):
            assert all(isinstance(row[key], str) for key in ("role", "content", "name", "tool_call_id"))
            assert isinstance(row["tool_calls"], list)
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            cold = socket.receive_json()
            assert cold["type"] == "snapshot" and cold["snapshot"]["active_run"]["events"] == events
        assert app.state.active is not None and not app.state.active.task.done()


def test_same_session_hydration_preserves_pending_live_approval(
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
            approval = frame["record"]
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            while True:
                hydrated = socket.receive_json()
                if hydrated["type"] == "snapshot":
                    break
            assert hydrated["snapshot"]["active_run"]["approval"]["approval_id"] == approval["approval_id"]
            active = app.state.active
            assert active is not None and active.pending is not None and not active.pending.answer.done()
            assert not app.channels[0].deliveries
            response = app.client.post(
                "/api/approval",
                headers=app.headers,
                json={
                    "run_id": approval["run_id"],
                    "approval_id": approval["approval_id"],
                    "call_id": approval["id"],
                    "decision": "allow",
                },
            )
            assert response.status_code == 200
            app.idle()
        assert len(app.channels[0].deliveries) == 1


def test_ordered_archive_includes_scheduler_lifecycle_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="timer", name="schedule_wakeup", arguments={"seconds": 60, "reason": "later"})
            else:
                yield TextChunkEvent(chunk="waiting after schedule")
                await asyncio.Event().wait()

        app.providers[0].script = script
        app.submit("schedule")

        async def ready() -> None:
            async with asyncio.timeout(5):
                while app.state.active is None or not any(
                    record.get("chunk") == "waiting after schedule" for record in app.state.active.drafts.values()
                ):
                    await asyncio.sleep(0.01)

        assert app.client.portal is not None
        app.client.portal.call(ready)
        active = cast("dict[str, object]", app.history(app.main)["active_run"])
        events = cast("list[dict[str, object]]", active["events"])
        scheduled = [record for record in events if record["event"] == "wakeup"]
        assert len(scheduled) == 1 and scheduled[0]["status"] == "scheduled" and scheduled[0]["run_id"] == active["id"]


def test_busy_read_selection_restoration_and_strict_emission_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        target = app.client.post("/api/sessions/new", headers=app.headers, json={}).json()["session_id"]
        assert (
            app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": app.main}).status_code
            == 200
        )
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            cursors = [socket.receive_json()["cursor"]]
            app.submit("hold")
            while True:
                frame = socket.receive_json()
                cursors.append(frame["cursor"])
                if frame.get("record", {}).get("event") == "text_chunk":
                    break
            active = app.state.active
            assert active is not None
            read = app.client.get(f"/api/sessions/{target}", headers=app.headers)
            assert read.status_code == 200 and read.json()["session_id"] == target
            assert app.state.selected_session_id == app.state.harness.session_id == app.main
            selected = app.client.post("/api/sessions/resume", headers=app.headers, json={"session_id": target})
            assert selected.status_code == 200 and selected.json()["retained_tasks"] == []
            assert app.state.selected_session_id == target and app.state.harness.session_id == app.main
            assert app.client.post("/api/cancel", headers=app.headers, json={"run_id": active.id}).status_code == 200
            app.idle()
            assert app.state.harness.session_id == app.state.selected_session_id == target
            while True:
                frame = socket.receive_json()
                cursors.append(frame["cursor"])
                if frame["type"] == "status" and not frame["active_run_id"]:
                    break
            assert cursors == sorted(set(cursors))
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            while True:
                hydrated = socket.receive_json()
                if hydrated["type"] == "snapshot":
                    break
            assert hydrated["cursor"] >= cursors[-1] and hydrated["snapshot"]["session_id"] == app.main
            assert app.state.harness.session_id == target


def test_ordered_replay_coalesces_only_adjacent_matching_scopes_and_is_detached() -> None:
    replay = RunReplay()
    replay.append({"event": "text_chunk", "chunk": "root "})
    replay.append({"event": "text_chunk", "chunk": "one"})
    replay.append({"event": "text_chunk", "chunk": "child", "task_id": "child", "activation": 1})
    replay.append({"event": "text_chunk", "chunk": " two"})
    events = cast("list[dict[str, object]]", replay.snapshot()["events"])
    assert [record["chunk"] for record in events] == ["root one", "child", " two"]
    events[0]["chunk"] = "mutated client snapshot"
    assert replay.records[0]["chunk"] == "root one"


@pytest.mark.parametrize("budget", ["events", "bytes"])
def test_overflow_omits_incomplete_archive_instead_of_erasing_earlier_client_evidence(
    monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    monkeypatch.setattr(
        "nagents.web.replay.MAX_RUN_EVENTS" if budget == "events" else "nagents.web.replay.MAX_RUN_BYTES", 2
    )
    run = Run("ngn-root")
    run.remember({"event": "run_started"})
    run.remember({"event": "tool_call", "id": "call", "name": "inspect"})
    run.remember({"event": "text_chunk", "chunk": "current draft"})
    snapshot = run.snapshot()
    assert snapshot["events_truncated"] is True and "events" not in snapshot
    assert any(
        record.get("chunk") == "current draft" for record in cast("list[dict[str, object]]", snapshot["records"])
    )
    assert run.replay.records == [] and run.replay.bytes == 0
