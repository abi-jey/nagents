"""Non-executable tool previews survive reconnect without unbounded replay."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.harness.types import ToolOutput
from nagents.web.replay import RunReplay
from nagents.web.service import Run
from tests.support.channels import site

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


def progress(**fields: object) -> dict[str, object]:
    return {
        "event": "tool_call_progress",
        "generation_id": "generation",
        "index": 0,
        "id": "",
        "name": "shell",
        "arguments_text": "",
        "status": "streaming",
        **fields,
    }


def test_cumulative_progress_replaces_adjacent_preview_even_when_provider_identity_arrives_late() -> None:
    replay = RunReplay()
    replay.append(progress(arguments_text='{"command":"'))
    replay.append(progress(id="call", arguments_text='{"command":"check'))
    assert len(replay.records) == 1
    assert replay.records[0]["arguments_text"] == '{"command":"check'
    replay.append(progress(index=1, arguments_text="parallel"))
    replay.append(progress(id="call", arguments_text='{"command":"check"}', status="ready"))
    assert len(replay.records) == 3
    assert replay.records[1]["index"] == 1
    replay.append(progress(id="call", generation_id="retry"))
    replay.append(progress(id="call", generation_id="retry", extra={"task_id": "child", "activation": 1}))
    assert len(replay.records) == 5


def test_drafts_promote_to_canonical_call_and_clear_invocation_on_result() -> None:
    run = Run("root")
    run.remember(progress(arguments_text="partial"))
    run.remember(progress(id="call", arguments_text="complete", status="ready"))
    assert len(run.drafts) == 1
    call: dict[str, object] = {"event": "tool_call", "id": "call", "extra": {"generation_id": "generation", "index": 0}}
    run.remember(call)
    assert list(run.drafts.values()) == [call]
    run.remember({"event": "tool_execution_started", "id": "call"})
    assert len(run.drafts) == 2
    run.remember({"event": "tool_result", "id": "call", "result": "done"})
    assert not run.drafts
    assert run.draft_bytes == 0


def test_abandoned_and_terminal_progress_is_removed_from_active_drafts_in_its_exact_scope() -> None:
    run = Run("root")
    run.remember(progress())
    run.remember(progress(extra={"task_id": "child", "activation": 1, "followup": 1}))
    run.remember(progress(extra={"task_id": "child", "activation": 1, "followup": 2}))
    run.remember(progress(status="abandoned"))
    assert len(run.drafts) == 2
    run.remember({"event": "task_completed", "task_id": "child", "activation": 1, "followup": 1})
    assert len(run.drafts) == 1
    run.remember({"event": "run_finished", "status": "cancelled"})
    assert not run.drafts
    assert run.draft_bytes == 0


def test_compact_call_drafts_retain_the_exact_persisted_row_anchor() -> None:
    run = Run("root")
    call: dict[str, object] = {"event": "tool_call", "id": "call", "transcript_event_id": "event"}
    run.remember(call)
    run.remember({"event": "tool_call", "id": "other", "transcript_event_id": "other-event"})
    run.remember(
        {"event": "transcript_anchor", "history_id": "row", "calls": [{"event_id": "event", "call_position": 1}]}
    )
    drafts = list(run.drafts.values())
    assert drafts[0] == {**call, "history_id": "row", "call_position": 1}
    assert "history_id" not in drafts[1]
    assert run.replay.records[0] == call, "Retaining an anchor must not mutate prior replay events"


@pytest.mark.requires_posix
def test_websocket_and_cold_replay_expose_previews_before_validation_then_real_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        release = asyncio.Event()
        finish = asyncio.Event()
        identity = {"generation_id": "generation", "index": 0}
        arguments = {"value": "test fixture only"}
        executions: list[str] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallProgressEvent(
                    generation_id="generation", name="fixture_tool", arguments_text='{"value":"'
                )
                await release.wait()
                yield ToolCallProgressEvent(generation_id="generation", id="call", name="fixture_tool", status="ready")
                yield ToolCallEvent(id="call", name="fixture_tool", arguments=arguments, extra=identity)
            else:
                yield TextChunkEvent(chunk="The tool finished.")
                await finish.wait()
                yield TextDoneEvent(text="The tool finished.")

        async def fixture_tool(value: str) -> str:
            """A harmless test extension requiring real approval."""
            executions.append(value)
            await app.state.harness.emit(ToolOutput("call", "fixture_tool", "fixture output"))
            return "fixture finished"

        app.state.harness.agent.register_tool(fixture_tool)
        app.providers[0].script = script
        assert app.client.portal is not None
        with app.socket() as socket:
            socket.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
            socket.receive_json()
            app.submit("Inspect streamed tools")
            while True:
                frame = socket.receive_json()
                if frame.get("record", {}).get("event") == "tool_call_progress":
                    break
            preview = frame["record"]
            assert preview["arguments_text"] == '{"value":"'
            before = app.history(app.main)
            assert not any(row["tool_calls"] for row in cast("list[dict[str, object]]", before["history"]))
            assert not executions
            with app.socket() as cold:
                cold.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
                active = cold.receive_json()["snapshot"]["active_run"]
                assert active["events"][-1] == preview
                assert active["records"][-1] == preview
            app.client.portal.call(release.set)
            observed: list[dict[str, object]] = []
            while True:
                frame = socket.receive_json()
                record = frame.get("record", {})
                if record:
                    observed.append(record)
                if record.get("event") == "approval":
                    break
            kinds = [record["event"] for record in observed]
            assert kinds.index("tool_call_progress") < kinds.index("tool_call") < kinds.index("approval")
            assert next(record for record in observed if record["event"] == "tool_call")["extra"] == identity
            approval = observed[-1]
            assert not executions
            response = app.client.post(
                "/api/approval",
                headers=app.headers,
                json={
                    "run_id": approval["run_id"],
                    "approval_id": approval["approval_id"],
                    "call_id": "call",
                    "decision": "allow",
                },
            )
            assert response.status_code == 200
            while True:
                frame = socket.receive_json()
                record = frame.get("record", {})
                if record:
                    observed.append(record)
                if record.get("event") == "text_chunk":
                    break
            kinds = [record["event"] for record in observed]
            # Approval can publish directly while the invocation event still
            # waits in the harness queue; both remain tied to the same call.
            assert kinds.index("tool_call") < kinds.index("tool_execution_started") < kinds.index("tool_result")
            assert kinds.index("approval_closed") < kinds.index("tool_output") < kinds.index("tool_result")
            assert executions == ["test fixture only"]
            with app.socket() as cold:
                cold.send_json({"type": "subscribe", "session_id": app.main, "after": 0})
                active = cold.receive_json()["snapshot"]["active_run"]
                assert len([record for record in active["events"] if record["event"] == "tool_call"]) == 1
                assert not any(
                    record["event"] in {"tool_call_progress", "tool_call", "tool_execution_started"}
                    for record in active["records"]
                )
            app.client.portal.call(finish.set)
            app.idle()
