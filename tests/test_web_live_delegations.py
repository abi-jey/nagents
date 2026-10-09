"""Delegation observability follows assistant lifecycle independently of voice."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.web.live_runtime import MAX_COMPLETED
from nagents.web.live_runtime import MAX_COMPLETED_DELEGATIONS
from nagents.web.live_runtime import MAX_DELEGATION_REQUEST
from nagents.web.live_runtime import MAX_DELEGATION_RESULT
from nagents.web.live_runtime import MAX_DELEGATION_TIMELINE
from nagents.web.live_runtime import MAX_EVENTS
from nagents.web.live_runtime import LiveService
from nagents.web.live_runtime import _Record
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path

    from nagents import Agent


def update(record: _Record, identifier: str, status: str, run: str = "") -> dict[str, object]:
    return {
        "delegation_id": identifier,
        "voice_session_id": record.identifier,
        "chat_session_id": "ngn-chat",
        "run_id": run,
        "status": status,
        "agent": "Research assistant",
        "provider": "chat-connection",
        "model": "chat-model",
        "text": "Assistant lifecycle status",
    }


def test_delegation_snapshot_survives_event_rollover_and_retains_every_active_task() -> None:
    record = _Record()
    record.delegation(update(record, "long-task", "queued"))
    record.delegation(update(record, "long-task", "working", "long-run"))
    for index in range(MAX_COMPLETED_DELEGATIONS + 8):
        identifier = f"task-{index}"
        record.delegation(update(record, identifier, "queued"))
        record.delegation(update(record, identifier, "working", f"run-{index}"))
        record.delegation(update(record, identifier, "completed", f"run-{index}"))
    for _ in range(MAX_EVENTS + 1):
        record.append("transcript", "Caption", speaker="assistant")
    assert not any(event["type"] == "delegation" for event in record.events)
    assert len(record.delegations) == MAX_COMPLETED_DELEGATIONS + 1
    assert record.delegation_details.keys() == record.delegations.keys()
    assert record.delegations["long-task"]["status"] == "working"
    assert record.snapshot(record.cursor)["delegations"]
    record.delegation(update(record, "long-task", "completed", "long-run"))
    assert len(record.delegations) == MAX_COMPLETED_DELEGATIONS
    assert record.delegation_details.keys() == record.delegations.keys()
    assert str(record.delegations["long-task"]["status"]) == "completed"


def test_closed_call_reporter_never_updates_a_new_call_or_regresses_a_terminal_task() -> None:
    def unused(voice: str) -> Agent:
        raise AssertionError("No provider is needed to observe lifecycle")

    service = LiveService(unused)
    old, new = _Record(), _Record()
    service._records[old.identifier] = old
    report = service.delegation_reporter(old.identifier)
    report(update(old, "old-task", "queued"))
    report(update(old, "old-task", "working", "old-run"))
    old.transition("closed", "Voice ended; assistant work continues")
    assert old.delegations["old-task"]["status"] == "working"
    service._records[new.identifier] = new
    new.delegation(update(new, "new-task", "queued"))
    report(update(old, "old-task", "completed", "old-run"))
    before = old.cursor
    report(update(old, "old-task", "working", "old-run"))
    report(update(new, "new-task", "failed"))
    assert old.cursor == before and str(old.delegations["old-task"]["status"]) == "completed"
    assert new.delegations["new-task"]["status"] == "queued"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": True},
        {"status": "speaking"},
        {"delegation_id": " \t"},
        {"delegation_id": "x" * 257},
        {"chat_session_id": "another-chat"},
        {"voice_session_id": "another-voice"},
        {"agent": {}},
        {"agent": "\x00\n\t"},
        {"run_id": "run-before-admission"},
    ],
)
def test_malformed_or_wrong_scope_status_is_not_published(changes: dict[str, object]) -> None:
    record = _Record()
    record.delegation({**update(record, "task", "queued"), **changes})
    assert not record.events and not record.delegations


def test_run_identity_is_fixed_once_admitted_and_metadata_is_bounded() -> None:
    record = _Record()
    record.delegation({**update(record, "task", "queued"), "agent": "Agent\n" + "x" * 300, "api_key": "never-emit"})
    assert len(str(record.delegations["task"]["agent"])) == 128
    assert "never-emit" not in str(record.snapshot())
    record.delegation(update(record, "task", "working", "owned-run"))
    before = record.cursor
    record.delegation(update(record, "task", "working"))
    record.delegation(update(record, "task", "completed", "unrelated-run"))
    record.delegation(update(record, "task", "queued"))
    assert record.cursor == before and record.delegations["task"]["status"] == "working"


def test_closed_call_with_working_assistant_survives_call_cache_retention() -> None:
    def unused(voice: str) -> Agent:
        raise AssertionError("No provider is needed to observe lifecycle")

    service = LiveService(unused)
    old = _Record()
    service._records[old.identifier] = old
    report = service.delegation_reporter(old.identifier)
    report(update(old, "long-task", "queued"))
    report(update(old, "long-task", "working", "owned-run"))
    old.transition("closed", "Voice ended while the assistant continued")
    for _ in range(MAX_COMPLETED + 2):
        ended = _Record(status="closed")
        service._records[ended.identifier] = ended
    service._prune_records()
    assert len(service._records) == MAX_COMPLETED + 1 and service._lookup(old.identifier) is old
    report(update(old, "long-task", "completed", "owned-run"))
    assert len(service._records) == MAX_COMPLETED and old.identifier not in service._records


def test_details_preserve_exact_payloads_without_expanding_polling_snapshots() -> None:
    record = _Record()
    transcript, prompt = '[{"speaker":"user","text":"Two plus two?"}]', "Instructions\n\nTwo plus two?\n"
    record.delegation({**update(record, "task", "queued"), "request_transcript": transcript, "api_key": "secret"})
    record.delegation(update(record, "task", "working", "run"))
    before = record.cursor
    record.delegation({**update(record, "task", "working", "run"), "request_input": prompt})
    assert record.cursor == before + 1  # Newly dispatched input is observable despite an unchanged phase.
    record.delegation(
        {
            **update(record, "task", "working", "run"),
            "request_transcript": "replace transcript",
            "request_input": "replace input",
        }
    )
    assert record.cursor == before + 1  # Once captured, request payloads are immutable.
    record.delegation({**update(record, "task", "completed", "run"), "result_text": "Four.\n"})
    details = record.delegation_details["task"].snapshot()
    assert details["source"] == "app_callback"
    assert details["request"] == {
        "transcript": {"text": transcript, "truncated": False, "characters": len(transcript)},
        "input": {"text": prompt, "truncated": False, "characters": len(prompt)},
    }
    assert details["result"] == {
        "kind": "assistant_output",
        "text": "Four.\n",
        "truncated": False,
        "characters": 6,
    }
    timeline = cast("list[dict[str, object]]", details["timeline"])
    assert [event["status"] for event in timeline] == ["queued", "working", "working", "completed"]
    assert [event["seq"] for event in timeline] == [1, 2, 3, 4]
    assert all(
        event["delegation_id"] == "task" and event["voice_session_id"] == record.identifier for event in timeline
    )
    assert "secret" not in str(details)
    for hidden in (transcript, prompt, "Four.", "request_input", "timeline", "result"):
        assert hidden not in str(record.snapshot())
    assert record.delegations["task"]["has_details"] is True


def test_details_cap_text_and_timeline_independently_of_caption_rollover() -> None:
    record = _Record()
    transcript = "🎤" * (MAX_DELEGATION_REQUEST + 9)
    result = "result" * MAX_DELEGATION_RESULT
    record.delegation({**update(record, "task", "queued"), "request_transcript": transcript})
    record.delegation({**update(record, "task", "working", "run"), "request_input": transcript})
    for index in range(MAX_DELEGATION_TIMELINE + 1):
        record.delegation({**update(record, "task", "working", "run"), "text": f"Observed processing stage {index}"})
    record.delegation({**update(record, "task", "completed", "run"), "result_text": result})
    for _ in range(MAX_EVENTS + 1):
        record.append("transcript", "Caption", speaker="assistant")
    details = record.delegation_details["task"].snapshot()
    request = cast("dict[str, dict[str, object]]", details["request"])
    assert (
        request["transcript"]
        == request["input"]
        == {
            "text": transcript[:MAX_DELEGATION_REQUEST],
            "characters": len(transcript),
            "truncated": True,
        }
    )
    assert details["result"] == {
        "kind": "assistant_output",
        "text": result[:MAX_DELEGATION_RESULT],
        "characters": len(result),
        "truncated": True,
    }
    timeline = cast("list[dict[str, object]]", details["timeline"])
    assert len(timeline) == MAX_DELEGATION_TIMELINE and details["timeline_truncated"] is True
    assert timeline[-1]["status"] == "completed"
    assert not any(event["type"] == "delegation" for event in record.events)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_unadmitted_terminal_details_have_safe_explanation_and_no_dispatched_input(status: str) -> None:
    record = _Record()
    record.delegation({**update(record, "task", "queued"), "request_transcript": "Caller request"})
    record.delegation(
        {
            **update(record, "task", status),
            "text": "Request did not start.",
            "request_input": "Never dispatched",
            "result_text": "Private exception",
        }
    )
    details = record.delegation_details["task"].snapshot()
    assert details["request"] == {"transcript": {"text": "Caller request", "truncated": False, "characters": 14}}
    assert details["result"] == {
        "kind": "terminal_explanation",
        "text": "Request did not start.",
        "truncated": False,
        "characters": 22,
    }
    before = record.delegation_details["task"].snapshot()
    record.delegation({**update(record, "task", "working", "run"), "request_input": "Late replacement"})
    assert record.delegation_details["task"].snapshot() == before


def test_wrong_call_or_root_cannot_modify_payload_details() -> None:
    record = _Record()
    record.delegation({**update(record, "task", "queued"), "request_transcript": "Original"})
    before = record.delegation_details["task"].snapshot()
    for changed in ({"voice_session_id": "another-call"}, {"chat_session_id": "ngn-another"}, {"status": True}):
        record.delegation({**update(record, "task", "working", "run"), **changed, "request_input": "Wrong request"})
    assert record.delegation_details["task"].snapshot() == before


def test_inspector_route_is_opt_in_and_scoped_to_retained_call_and_delegation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            service = cast("LiveService", app.state.live)
            record = _Record()
            service._records[record.identifier] = record
            record.delegation({**update(record, "task", "queued"), "request_transcript": "Exact caller payload\n"})
            record.delegation({**update(record, "task", "working", "run"), "request_input": "Exact assistant input\n"})
            path = f"/api/live/sessions/{record.identifier}/delegations/task"
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            details = response.json()
            assert details["voice_session_id"] == record.identifier and details["chat_session_id"] == "ngn-chat"
            assert details["run_id"] == "run" and details["source"] == "app_callback"
            assert details["request"]["input"]["text"] == "Exact assistant input\n"
            assert len(details["timeline"]) == 2 and "result" not in details
            snapshot = await client.get(f"/api/live/sessions/{record.identifier}", headers=headers)
            assert snapshot.status_code == 200 and "Exact caller payload" not in snapshot.text
            assert "Exact assistant input" not in snapshot.text and "timeline" not in snapshot.text
            assert (await client.get(path + "?after=0", headers=headers)).status_code == 400
            assert (await client.get(path.replace("/task", "/missing"), headers=headers)).status_code == 404
            assert (await client.get(path.replace(record.identifier, "missing"), headers=headers)).status_code == 404
            assert (await client.get(path.replace("/task", "/unknown!"), headers=headers)).status_code == 404
            assert (await client.get(path.replace("/task", "/" + "x" * 257), headers=headers)).status_code == 422
            record.delegation({**update(record, "task", "completed", "run"), "result_text": "Actual result"})
            record.transition("closed", "Voice ended")
            ended = (await client.get(path, headers=headers)).json()
            assert ended["result"]["text"] == "Actual result" and ended["status"] == "completed"

    asyncio.run(scenario())
