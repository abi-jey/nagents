"""Durable, exact-row response evidence across root/child interruption and reload."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import cast
from uuid import uuid4

import pytest

from nagents.events import ReasoningChunkEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import Usage
from nagents.session import response_metadata as storage
from nagents.session.response_metadata import response_write
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.support.web import submit_and_wait
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from tests.support.providers import FakeProvider


def object_record(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    return cast("dict[str, object]", value)


def object_records(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [object_record(item) for item in value]


def test_root_round_metadata_reload_fork_and_totals_are_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert "PRIVATE SUMMARY" not in str(messages)
        if not any(message.role == "tool" for message in messages):
            yield ReasoningChunkEvent(displayable=True, chunk="PRIVATE SUMMARY")
            yield ToolCallEvent(
                id="bad",
                name="not_installed",
                arguments={},
                usage=Usage(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    reasoning_tokens=3,
                    reasoning_reported=True,
                    reasoning_in_output="included",
                ),
            )
        else:
            yield TextDoneEvent(text="Visible answer")
            yield TextDoneEvent(
                text="Visible answer",
                usage=Usage(
                    prompt_tokens=20,
                    completion_tokens=7,
                    total_tokens=27,
                    reasoning_tokens=2,
                    reasoning_reported=True,
                    reasoning_in_output="included",
                ),
            )

    async def scenario() -> None:
        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            root = state.selected_session_id
            events = await submit_and_wait(app, headers, root, "go")
            completed = [
                record
                for record in events
                if record.get("event") == "response_metadata" and record.get("status") == "completed"
            ]
            assert len(completed) == 2 and len({record["response_id"] for record in completed}) == 2
            assert completed[-1]["run_usage"] == {
                "prompt_tokens": 30,
                "completion_tokens": 12,
                "total_tokens": 42,
                "reasoning_tokens": 5,
                "response_count": 2,
                "usage_response_count": 2,
                "reasoning_response_count": 2,
                "complete": True,
                "prompt_reported": True,
                "completion_reported": True,
                "total_reported": True,
                "prompt_response_count": 2,
                "completion_response_count": 2,
                "total_response_count": 2,
                "reasoning_reported": True,
                "reasoning_in_output": "included",
            }
            original = await state.history.snapshot(root)
            assistant = [row for row in original if row.get("role") == "assistant"]
            assert len(assistant) == 2
            for row, event in zip(assistant, completed, strict=True):
                assert row["history_id"] == event["history_id"]
                metadata = object_record(row["response_metadata"])
                assert metadata["response_id"] == event["response_id"]
                assert object_record(metadata["run_usage"])["total_tokens"] == 42
            fork = await client.post(f"/api/sessions/{root}/fork", headers=headers, json={})
            assert fork.status_code == 200, fork.text
            fork_id = fork.json()["session_id"]
            copied = [row for row in await state.history.snapshot(fork_id) if row.get("role") == "assistant"]
            assert len(copied) == 2
            for before, after in zip(assistant, copied, strict=True):
                before_metadata = object_record(before["response_metadata"])
                after_metadata = object_record(after["response_metadata"])
                assert after_metadata["response_id"] != before_metadata["response_id"]
                assert after_metadata["response_session_id"] == fork_id
                assert after_metadata["history_id"] == after["history_id"]
                assert after_metadata["summary"] == before_metadata["summary"]
            await state.history.clear_session(fork_id)
            assert await state.history.snapshot(fork_id) == []
            assert len([row for row in await state.history.snapshot(root) if row.get("role") == "assistant"]) == 2

    asyncio.run(scenario())


def test_cancel_checkpoints_latest_summary_without_final_generator_yield(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        ready = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield ReasoningChunkEvent(displayable=True, chunk="first ")
            for _ in range(20):
                yield ReasoningChunkEvent(displayable=True, chunk="part ")
            ready.set()
            await asyncio.Event().wait()
            yield TextDoneEvent(text="unreachable")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, _, headers, state, _, _):
            root = state.selected_session_id
            stream = LiveStream(app, headers, root, "go")
            await stream.start_task
            async with asyncio.timeout(HANG_GUARD):
                await ready.wait()
            run = state.run_for(root)
            assert run is not None
            await state.stop(run)
            await stream.disconnect()
            records = [row for row in await state.history.snapshot(root) if row.get("role") == "response_metadata"]
            assert len(records) == 1
            metadata = object_record(records[0]["response_metadata"])
            assert metadata["summary"] == "first " + "part " * 20
            assert metadata["status"] == "cancelled" and metadata["history_id"] == ""
            assert not metadata["usage_reported"]
            assert not state.responses.pending and not state.responses.checkpoints
            model_history = await state.history.get_history(root)
            assert len(model_history) == 1 and model_history[0].role == "user"
            replay = run.snapshot()
            snapshots = [
                event for event in object_records(replay["events"]) if event.get("event") == "response_metadata"
            ]
            assert len(snapshots) == 1 and snapshots[0]["status"] == "cancelled"

    asyncio.run(scenario())


def test_atomic_assistant_anchor_wins_cancel_and_retention_keeps_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    async def scenario() -> None:
        async with scheduled_app(tmp_path, monkeypatch, script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="go"))
            terminal = ResponseMetadataEvent(
                response_id="race",
                response_session_id=root,
                revision=5,
                summary="savedsummary",
                status="completed",
                usage_reported=True,
                usage=Usage(prompt_tokens=4, completion_tokens=3, total_tokens=7, reasoning_reported=True),
            )
            with response_write(terminal):
                row = await state.history.add_message(root, Message(role="assistant", content="committed"))

            def mutate(db: sqlite3.Connection) -> None:
                record = asdict(terminal)
                record.pop("type")
                record.pop("timestamp")
                record.pop("extra")
                cancelled = storage.save(
                    db,
                    {
                        **record,
                        "revision": 6,
                        "status": "cancelled",
                        "run_id": "run-race",
                        "event": "response_metadata",
                    },
                )
                assert cancelled["status"] == "completed" and cancelled["history_id"] == str(row)
                assert cancelled["summary"] == "savedsummary" and cancelled["run_id"] == "run-race"
                for index in range(101):
                    storage.save(
                        db,
                        {**record, "response_id": f"later-{index}", "run_id": "other", "history_id": "", "revision": 1},
                    )
                old = storage.lookup(db, root, "race")
                assert old["summary"] == "" and old["summary_expired"] is True
                assert object_record(old["usage"])["total_tokens"] == 7
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM ngn_response_metadata WHERE json_extract(payload,'$.summary') != ''"
                    ).fetchone()[0]
                    == 100
                )
                assert storage.run_totals(db, root, "run-race")["total_tokens"] == 7

            await state.history.store._transaction(mutate)

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_child_metadata_does_not_attach_to_main_and_survives_detail_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            assert "CHILD SUMMARY" not in str(messages)
            if provider.harness_config.agent == "reviewer":
                yield ReasoningChunkEvent(displayable=True, chunk="CHILD SUMMARY")
                yield TextChunkEvent(chunk="Child answer")
                yield TextDoneEvent(
                    text="Child answer",
                    usage=Usage(
                        prompt_tokens=31,
                        completion_tokens=9,
                        total_tokens=40,
                        reasoning_tokens=4,
                        reasoning_reported=True,
                        reasoning_in_output="included",
                    ),
                )
            else:
                yield TextDoneEvent(
                    text="Main answer", usage=Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)
                )

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            prefix = f"/api/sessions/{root}/tasks"
            response = await client.post(
                prefix, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "child task"}
            )
            assert response.status_code == 200, response.text
            task = response.json()["task"]["id"]
            await until(lambda: state.run_for(root) is None)
            detail = (await client.get(f"{prefix}/{task}", headers=headers)).json()
            rows = [row for row in detail["history"] if row.get("role") == "assistant"]
            assert len(rows) == 1
            metadata = object_record(rows[0]["response_metadata"])
            assert metadata["summary"] == "CHILD SUMMARY" and metadata["status"] == "completed"
            assert metadata["history_id"] == rows[0]["history_id"]
            assert metadata["response_session_id"] == detail["task"]["child_session_id"]
            assert object_record(metadata["usage"])["reasoning_tokens"] == 4
            root_rows = await state.history.snapshot(root)
            assert "CHILD SUMMARY" not in str(root_rows)
            again = (await client.get(f"{prefix}/{task}", headers=headers)).json()
            assert again["history"] == detail["history"]

    asyncio.run(scenario())


def test_parallel_roots_with_identical_answers_keep_independent_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        arrived = 0
        both = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            nonlocal arrived
            prompt = str(messages[-1].content)
            yield ReasoningChunkEvent(displayable=True, chunk=f"Summary for {prompt}")
            arrived += 1
            if arrived == 2:
                both.set()
            await both.wait()
            yield TextDoneEvent(
                text="Identical answer",
                usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12, reasoning_reported=True),
            )

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            first = state.selected_session_id
            await state.history.add_message(first, Message(role="user", content="Keep first"))
            second = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            async with asyncio.timeout(HANG_GUARD):
                await asyncio.gather(
                    submit_and_wait(app, headers, first, "first"), submit_and_wait(app, headers, second, "second")
                )
            ids = []
            for root, prompt in ((first, "first"), (second, "second")):
                rows = [row for row in await state.history.snapshot(root) if row.get("role") == "assistant"]
                assert len(rows) == 1 and rows[0]["content"] == "Identical answer"
                metadata = object_record(rows[0]["response_metadata"])
                assert metadata["summary"] == f"Summary for {prompt}"
                assert (
                    metadata["response_session_id"] == root
                    and object_record(metadata["run_usage"])["response_count"] == 1
                )
                ids.append(metadata["response_id"])
            assert len(set(ids)) == 2

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_child_cancel_before_observer_drain_keeps_scope_and_monotonic_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        ready = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "reviewer":
                yield ReasoningChunkEvent(displayable=True, chunk="Stopped child summary")
                ready.set()
                await asyncio.Event().wait()
            yield TextDoneEvent(text="Main")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            prefix = f"/api/sessions/{root}/tasks"
            result = await client.post(
                prefix, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "child"}
            )
            assert result.status_code == 200, result.text
            task = result.json()["task"]["id"]
            async with asyncio.timeout(HANG_GUARD):
                await ready.wait()
            run = state.run_for(root)
            assert run is not None and run.harness is not None
            await state.stop(run)
            detail = (await client.get(f"{prefix}/{task}", headers=headers)).json()
            rows = [row for row in detail["history"] if row.get("role") == "response_metadata"]
            assert len(rows) == 1
            metadata = object_record(rows[0]["response_metadata"])
            assert metadata["summary"] == "Stopped child summary" and metadata["status"] == "cancelled"
            assert metadata["task_id"] == task and metadata["activation"] == detail["task"]["activation"]
            assert metadata["turn_id"] and metadata["response_session_id"] == detail["task"]["child_session_id"]
            assert "Stopped child summary" not in str(await state.history.snapshot(root))
            captured = [
                event
                for event in object_records(run.snapshot()["events"])
                if event.get("event") == "task_activity"
                and object_record(event.get("payload", {})).get("type") == "response_metadata"
            ]
            assert captured and object_record(captured[-1]["payload"])["status"] == "cancelled"
            assert captured[-1]["sequence"] == run.harness.tasks._lookup(task).activity_sequence

    asyncio.run(scenario())


def test_restart_terminalizes_checkpoints_and_native_usage_coverage_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    async def scenario() -> None:
        async with scheduled_app(tmp_path, monkeypatch, script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="keep"))
            checkpoint = ResponseMetadataEvent(
                response_id="crashed",
                response_session_id=root,
                revision=3,
                status="thinking",
                summary="Checkpoint",
                usage_reported=True,
                usage=Usage(
                    reasoning_tokens=7,
                    reasoning_reported=True,
                    reasoning_in_output="separate",
                    total_tokens=7,
                    reported=True,
                ),
            )
            record = asdict(checkpoint)
            for key in ("type", "timestamp", "extra"):
                record.pop(key)
            record.update(run_id="lost-run", event="response_metadata", turn_id="old-turn")
            await state.history.store._transaction(lambda db: storage.save(db, record))
        async with scheduled_app(tmp_path, monkeypatch, script) as (_, _, _, state, _, _):
            rows = [row for row in await state.history.snapshot(root) if row.get("role") == "response_metadata"]
            assert len(rows) == 1
            metadata = object_record(rows[0]["response_metadata"])
            assert metadata["status"] == "cancelled" and metadata["summary"] == "Checkpoint"
            totals = object_record(metadata["run_usage"])
            assert totals["reasoning_tokens"] == 7 and totals["reasoning_reported"]
            assert not totals["prompt_reported"] and not totals["completion_reported"] and not totals["total_reported"]
            assert totals["total_response_count"] == 0 and not totals["complete"]

    asyncio.run(scenario())
