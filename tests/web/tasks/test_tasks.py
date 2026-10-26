"""Real child inspection and verified input receipts through the public web API."""

from __future__ import annotations

import asyncio
import json
from functools import wraps
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi import HTTPException

from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote.profiles import RemoteProfile
from nagents.harness.session_identity import SessionIdentityStore
from nagents.harness.tools import CodingTools
from nagents.types import JsonValue
from nagents.types import Message
from nagents.types import ToolCall
from nagents.web.service import Run
from nagents.web.tasks import routes as task_api
from nagents.web.tasks.store import TaskStore
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import assert_balanced
from tests.support.web import submit_and_wait
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.web.routing import Work
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("delay_history_marker", [False, True])
def test_task_api_start_inspect_busy_fifo_and_retained_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay_history_marker: bool
) -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        marker_blocked, release_marker = asyncio.Event(), asyncio.Event()
        seen: list[str] = []
        (tmp_path / "fixture.txt").write_text("PRIVATE TOOL EVIDENCE")

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            assert_balanced(messages)
            if provider.harness_config.agent != "reviewer":
                yield TextDoneEvent(text="Main synthesis")
                return
            prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
            seen.append(prompt)
            if prompt == "PRIVATE CHILD ASSIGNMENT":
                yield TextChunkEvent(chunk="PRIVATE CHILD STREAM")
                started.set()
                await release.wait()
                yield ToolCallEvent(id="read-a", name="read_file", arguments={"path": "fixture.txt"})
                yield ToolCallEvent(id="read-b", name="read_file", arguments={"path": "fixture.txt"})
            else:
                if prompt == "PRIVATE NOTE ONE":
                    index = next(index for index, message in enumerate(messages) if message.content == prompt)
                    assert {message.tool_call_id for message in messages[:index] if message.role == "tool"} == {
                        "read-a",
                        "read-b",
                    }
                yield TextDoneEvent(text="Child result")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            original_record = state.tasks.record
            marker_held = False

            async def delayed_record(run: Run, record: dict[str, object]) -> None:
                nonlocal marker_held
                payload = record.get("payload")
                if (
                    delay_history_marker
                    and not marker_held
                    and record.get("event") == "task_activity"
                    and isinstance(payload, dict)
                    and payload.get("type") == "history_committed"
                ):
                    marker_held = True
                    marker_blocked.set()
                    await release_marker.wait()
                await original_record(run, record)

            monkeypatch.setattr(state.tasks, "record", delayed_record)
            root = state.selected_session_id
            prefix = f"/api/sessions/{root}/tasks"
            catalog = (await client.get(prefix, headers=headers)).json()
            reviewer = next(profile for profile in catalog["profiles"] if profile["id"] == "reviewer")
            assert reviewer["available"] and not reviewer["used"]
            first_id = str(uuid4())
            response = await client.post(
                prefix,
                headers=headers,
                json={"message_id": first_id, "agent": "reviewer", "text": "PRIVATE CHILD ASSIGNMENT"},
            )
            assert response.status_code == 200, response.text
            task_id = response.json()["task"]["session_id"]
            async with asyncio.timeout(HANG_GUARD):
                await started.wait()
            try:
                if delay_history_marker:
                    async with asyncio.timeout(HANG_GUARD):
                        await marker_blocked.wait()
                detail = (await client.get(f"{prefix}/{task_id}", headers=headers)).json()
                receipt = detail["receipts"][0]
                # Provider entry follows the atomic user-row/receipt commit,
                # independently of the root observer's presentation watermark.
                assert receipt["status"] == "picked_up" and receipt["history_id"]
                committed = await state.executions.owners[root].tasks.history(task_id)
                assert sum(message.content == "PRIVATE CHILD ASSIGNMENT" for message in committed) == 1
                if delay_history_marker:
                    assert detail["history_boundary"] < receipt["history_id"]
                    assert not any(row["history_id"] == str(receipt["history_id"]) for row in detail["history"])
            finally:
                release_marker.set()
            # The public inspector deliberately exposes only a coherent prefix
            # whose ordered history marker has reached the root observer.
            async with asyncio.timeout(HANG_GUARD):
                while True:
                    response = await client.get(f"{prefix}/{task_id}", headers=headers)
                    assert response.status_code == 200, response.text
                    detail = response.json()
                    visible = [row for row in detail["history"] if row["history_id"] == str(receipt["history_id"])]
                    if visible:
                        break
                    await asyncio.sleep(0)
            assert len(visible) == 1 and visible[0]["content"] == "PRIVATE CHILD ASSIGNMENT"
            assert detail["receipts"][0]["status"] == "picked_up"
            notes = [{"message_id": str(uuid4()), "text": text} for text in ("PRIVATE NOTE ONE", "PRIVATE NOTE TWO")]
            for note in notes:
                response = await client.post(f"{prefix}/{task_id}/messages", headers=headers, json=note)
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "queued"
            used = state.executions.owners[root].tasks._used
            retried = await client.post(f"{prefix}/{task_id}/messages", headers=headers, json=notes[0])
            assert retried.json()["status"] == "queued"
            assert state.executions.owners[root].tasks._used == used
            conflict = await client.post(
                f"{prefix}/{task_id}/messages", headers=headers, json={**notes[0], "text": "different"}
            )
            assert conflict.status_code == 409
            release.set()
            await until(lambda: state.run_for(root) is None)
            detail = (await client.get(f"{prefix}/{task_id}", headers=headers)).json()
            assert all(receipt["status"] == "picked_up" for receipt in detail["receipts"])
            assert seen == ["PRIVATE CHILD ASSIGNMENT", "PRIVATE NOTE ONE", "PRIVATE NOTE TWO"]
            assert not detail["activity"]
            assert detail["task"]["provider"] and detail["task"]["model"]
            root_history = await state.history.snapshot(root)
            assert "PRIVATE NOTE" not in json.dumps(root_history)
            assert "PRIVATE CHILD ASSIGNMENT" not in json.dumps(root_history)
            direct = {"message_id": str(uuid4()), "text": "continue child"}
            continued = await client.post(f"{prefix}/{task_id}/messages", headers=headers, json=direct)
            assert continued.status_code == 200, continued.text
            await until(lambda: state.run_for(root) is None)
            assert seen[-1] == "continue child"
            detail = (await client.get(f"{prefix}/{task_id}", headers=headers)).json()
            assert detail["task"]["activation"] == 1
            assert detail["receipts"][-1]["status"] == "picked_up"

    asyncio.run(scenario())


def test_task_evidence_restores_from_trash_and_permanent_delete_scrubs_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            identity = await SessionIdentityStore(state.history.db_path).reserve(root, "", "")
            child, task_id, message_id = identity.storage_id, identity.session_id, str(uuid4())
            store = state.tasks.store
            await store.validate(root)
            await state.history.get_or_create_session(child, "harness")
            await state.history.add_message(child, Message(role="user", content="private child history"))
            await state.history.add_message(
                child,
                Message(
                    role="assistant",
                    content="safe",
                    tool_calls=[ToolCall("opaque", "example", {}, {"reasoning": "PRIVATE PROVIDER STATE"})],
                ),
            )
            await store.save(
                root,
                {
                    "session_id": task_id,
                    "title": "",
                    "root_session_id": root,
                    "status": "completed",
                    "profile": "reviewer",
                },
                child,
            )
            await store.insert(root, task_id, message_id, "private receipt body", task_id)
            await store.fail(message_id, "Previous process ended")
            removed = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
            assert removed.status_code == 200, removed.text
            item = removed.json()["trash"]
            assert (await client.get(f"/api/sessions/{root}/tasks/{task_id}", headers=headers)).status_code == 404
            restored = await client.post(
                f"/api/trash/{root}/restore", headers=headers, json={"deletion_id": item["deletion_id"]}
            )
            assert restored.status_code == 200, restored.text
            detail = (await client.get(f"/api/sessions/{root}/tasks/{task_id}", headers=headers)).json()
            assert detail["history"][0]["content"] == "private child history"
            assert "PRIVATE PROVIDER STATE" not in json.dumps(detail)
            assert set(detail["history"][1]["tool_calls"][0]) == {"id", "name", "arguments"}
            assert detail["receipts"][0]["text"] == "private receipt body"
            purged = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True})
            assert purged.status_code == 200, purged.text
            assert not await state.history.get_history(child)
            row = await store.db._transaction(lambda db: store.receipt(store.row(db, message_id)))
            assert row["text"] == "" and row["text_pruned"]
            assert row["message_id"] == message_id
            assert not await store.db._transaction(
                lambda db: db.execute("SELECT 1 FROM ngn_web_agent_sessions WHERE root = ?", (root,)).fetchone()
            )

    asyncio.run(scenario())


def test_mid_tool_history_watermark_does_not_repeat_persisted_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        (tmp_path / "fixture.txt").write_text("tool content")
        read_file = CodingTools.read_file

        @wraps(read_file)
        async def held_read(
            owner: CodingTools, path: str, start_line: int = 1, limit: int = 200
        ) -> dict[str, JsonValue]:
            entered.set()
            await release.wait()
            return await read_file(owner, path, start_line, limit)

        monkeypatch.setattr(CodingTools, "read_file", held_read)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "reviewer" and messages[-1].role == "user":
                yield TextChunkEvent(chunk="Inspecting the file")
                yield TextDoneEvent(text="Inspecting the file")
                yield ToolCallEvent(id="held", name="read_file", arguments={"path": "fixture.txt"})
            else:
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            response = await client.post(
                path, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "inspect"}
            )
            task_id = response.json()["task"]["session_id"]
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                await until(
                    lambda: any(
                        isinstance(payload := record.get("payload"), dict)
                        and payload.get("type") == "tool_execution_started"
                        for record in state.tasks.activity.get((root, task_id), [])
                    )
                )
                detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
                raw = await state.executions.owners[root].tasks.history(task_id)
                assert any(message.tool_calls for message in raw), "The assistant tool call really is already persisted"
                assert not any(row["tool_calls"] for row in detail["history"])
                assert not any(row["content"] == "Inspecting the file" for row in detail["history"])
                assert any(record["payload"]["type"] == "tool_call" for record in detail["activity"])
            finally:
                release.set()
            await until(lambda: state.run_for(root) is None)
            final = (await client.get(f"{path}/{task_id}", headers=headers)).json()
            assert sum(bool(row["tool_calls"]) for row in final["history"]) == 1
            assert not final["activity"]

    asyncio.run(scenario())


def test_task_routes_scope_and_history_query_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            prefix = f"/api/sessions/{root}/tasks"
            response = await client.post(
                prefix, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "inspect"}
            )
            task_id = response.json()["task"]["session_id"]
            await until(lambda: state.run_for(root) is None)
            assert (await client.get(f"{prefix}/{task_id}?limit=1&before=9999999", headers=headers)).status_code == 200
            for query in ("limit=0", "limit=201", "limit=1&limit=2", "limit=1&token=secret", "before=-1", "limit=1&x="):
                assert (await client.get(f"{prefix}/{task_id}?{query}", headers=headers)).status_code == 400
            await state.harness.create_session("ngn-other-root")
            other = f"/api/sessions/ngn-other-root/tasks/{task_id}"
            assert (await client.get(other, headers=headers)).status_code == 404
            assert (
                await client.post(
                    f"{other}/messages", headers=headers, json={"message_id": str(uuid4()), "text": "wrong root"}
                )
            ).status_code == 404
            child = state.executions.owners[root].tasks._lookup(task_id)._storage_id
            assert (await client.get(f"/api/sessions/{child}/tasks/{task_id}", headers=headers)).status_code == 404
            assert (await client.get(f"{prefix}/{task_id}")).status_code == 403
            assert state.selected_session_id == root

    asyncio.run(scenario())


def test_restart_receipts_are_terminal_and_manifest_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            old = state.tasks.store
            await old.validate(root)
            identifier = str(uuid4())
            await old.insert(root, "old-task", identifier, "unread note", "old-task")
            await old.save(
                root,
                {
                    "session_id": "s1",
                    "title": "",
                    "root_session_id": root,
                    "status": "running",
                    "profile": "reviewer",
                },
                "ngn-child",
            )
            recovered = TaskStore(state.history.db_path, "new-process")
            await recovered.initialize()
            receipt = await recovered.duplicate(root, "old-task", identifier, "unread note")
            assert receipt["status"] == "error" and "restart" in str(receipt["reason"])
            tasks = await recovered.tasks(root)
            assert tasks[0]["status"] == "cancelled" and tasks[0]["can_message"] is False
            assert not state.executions.runs
            with pytest.raises(HTTPException, match="409"):
                await recovered.duplicate(root, "another-task", identifier, "unread note")

    asyncio.run(scenario())


def test_task_settings_are_saved_for_next_activation_without_changing_busy_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        seen: list[tuple[str, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "reviewer":
                prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
                seen.append((prompt, provider.model))
                if prompt == "first":
                    entered.set()
                    await release.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            response = await client.post(
                path, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "first"}
            )
            task_id = response.json()["task"]["session_id"]
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
            configured = await client.put(
                f"{path}/{task_id}/settings",
                headers=headers,
                json={"model": "next-model", "compact_trigger": "tokens", "compact_tokens": 20000},
            )
            assert configured.status_code == 200, configured.text
            task = configured.json()["task"]
            assert task["model"] == "fake-model" and task["next_settings"]["model"] == "next-model"
            note = await client.post(
                f"{path}/{task_id}/messages", headers=headers, json={"message_id": str(uuid4()), "text": "busy note"}
            )
            assert note.status_code == 200 and note.json()["status"] == "queued"
            release.set()
            await until(lambda: state.run_for(root) is None)
            assert seen == [("first", "fake-model"), ("busy note", "fake-model")]
            followup = await client.post(
                f"{path}/{task_id}/messages", headers=headers, json={"message_id": str(uuid4()), "text": "idle note"}
            )
            assert followup.status_code == 200, followup.text
            await until(lambda: state.run_for(root) is None)
            assert seen[-1] == ("idle note", "next-model")
            detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
            assert detail["task"]["model"] == "next-model" and not detail["task"]["next_settings"]
            assert detail["task"]["compaction"]["token_threshold"] == 14000

    asyncio.run(scenario())


def test_cancel_racing_receipt_commit_terminalizes_unread_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, commit, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen: list[str] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "reviewer":
                prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
                seen.append(prompt)
                entered.set()
                await asyncio.Event().wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            response = await client.post(
                path, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "original"}
            )
            task_id = response.json()["task"]["session_id"]
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
            insert = state.tasks.store.insert

            async def delayed(
                root: str, target: str, identifier: str, text: str, session_id: str = ""
            ) -> dict[str, object]:
                receipt = await insert(root, target, identifier, text, session_id)
                commit.set()
                await release.wait()
                return receipt

            monkeypatch.setattr(state.tasks.store, "insert", delayed)
            note = {"message_id": str(uuid4()), "text": "must remain unread"}
            request = asyncio.create_task(client.post(f"{path}/{task_id}/messages", headers=headers, json=note))
            async with asyncio.timeout(HANG_GUARD):
                await commit.wait()
                run = state.run_for(root)
                assert run is not None
                await state.stop(run)
                release.set()
                response = await request
            assert response.status_code == 409
            duplicate = await client.post(f"{path}/{task_id}/messages", headers=headers, json=note)
            assert duplicate.status_code == 200 and duplicate.json()["status"] == "error"
            detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
            assert not any(row["content"] == note["text"] for row in detail["history"])
            assert seen == ["original"]
            assert detail["receipts"][0]["status"] == "picked_up"
            assert detail["receipts"][1]["status"] == "error"

    asyncio.run(scenario())


def test_profile_start_receipt_stays_on_first_task_when_it_spawns_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 1 and messages[-1].role == "user" and messages[-1].content == "spawn nested":
                yield ToolCallEvent(id="nested", name="to_agent", arguments={"prompt": "nested child"})
            else:
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            body = {"message_id": str(uuid4()), "agent": "reviewer", "text": "spawn nested"}
            response = await client.post(path, headers=headers, json=body)
            assert response.status_code == 200, response.text
            task_id = response.json()["task"]["session_id"]
            await until(lambda: state.run_for(root) is None)
            tasks = (await client.get(path, headers=headers)).json()["tasks"]
            assert len(tasks) == 2
            parent = next(task for task in tasks if task["session_id"] == task_id)
            child = next(task for task in tasks if task["session_id"] != task_id)
            assert parent["parent_session_id"] == "" and child["parent_session_id"] == task_id
            assert parent["origin_history_id"] == child["origin_history_id"] == ""
            retry = (await client.post(path, headers=headers, json=body)).json()
            assert retry["receipt"]["session_id"] == task_id and retry["receipt"]["status"] == "picked_up"
            assert retry["task"]["session_id"] == task_id
            assert not (await client.get(f"{path}/{child['session_id']}", headers=headers)).json()["receipts"]

    asyncio.run(scenario())


def test_native_delegation_origin_is_exact_durable_tool_row_and_survives_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            if messages[-1].role == "user" and prompt in {"first root", "second root", "parent task"}:
                assignment = {"first root": "parent task", "second root": "other task", "parent task": "nested task"}[
                    prompt
                ]
                # Deliberately reuse the provider call ID; it is not an anchor.
                yield ToolCallEvent(
                    id="reused-call", name="to_agent", arguments={"prompt": assignment, "agent": "reviewer"}
                )
            else:
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            root = state.selected_session_id
            first_events = await submit_and_wait(app, headers, root, "first root")
            second_events = await submit_and_wait(app, headers, root, "second root")
            history = await state.history.snapshot(root)

            def assigned(row: dict[str, object], prompt: str) -> bool:
                calls = row.get("tool_calls")
                return isinstance(calls, list) and any(
                    isinstance(call, dict)
                    and isinstance(arguments := call.get("arguments"), dict)
                    and arguments.get("prompt") == prompt
                    for call in calls
                )

            first_row = next(row for row in history if assigned(row, "parent task"))
            second_row = next(row for row in history if assigned(row, "other task"))
            tasks = (await client.get(f"/api/sessions/{root}/tasks", headers=headers)).json()["tasks"]
            parent = next(task for task in tasks if task["prompt"] == "parent task")
            nested = next(task for task in tasks if task["prompt"] == "nested task")
            other = next(task for task in tasks if task["prompt"] == "other task")
            assert parent["origin_history_id"] == nested["origin_history_id"] == first_row["history_id"]
            assert other["origin_history_id"] == second_row["history_id"] != first_row["history_id"]
            assert parent["origin_run_id"] and parent["origin_run_id"] == nested["origin_run_id"]
            assert other["origin_run_id"] != parent["origin_run_id"]
            events = [event for event in [*first_events, *second_events] if event.get("event") == "task_started"]
            assert all(event["origin_history_id"] for event in events)
            response = await client.post(
                f"/api/sessions/{root}/tasks/{parent['session_id']}/messages",
                headers=headers,
                json={"message_id": str(uuid4()), "text": "follow up"},
            )
            assert response.status_code == 200, response.text
            await until(lambda: state.run_for(root) is None)
            retained = next(
                task for task in await state.tasks.store.tasks(root) if task["session_id"] == parent["session_id"]
            )
            assert retained["origin_history_id"] == first_row["history_id"]
            assert retained["origin_run_id"] == parent["origin_run_id"]
            restarted = TaskStore(state.history.db_path, "origin-restart")
            await restarted.initialize()
            restored = {task["session_id"]: task for task in await restarted.tasks(root)}
            assert restored[parent["session_id"]]["origin_history_id"] == first_row["history_id"]
            assert restored[nested["session_id"]]["origin_history_id"] == first_row["history_id"]

    asyncio.run(scenario())


def test_delayed_old_completion_does_not_terminalize_new_activation_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "reviewer" and messages[-1].content == "second activation":
                entered.set()
                await release.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            response = await client.post(
                path,
                headers=headers,
                json={"message_id": str(uuid4()), "agent": "reviewer", "text": "first activation"},
            )
            task_id = response.json()["task"]["session_id"]
            await until(lambda: state.run_for(root) is None)
            response = await client.post(
                f"{path}/{task_id}/messages",
                headers=headers,
                json={"message_id": str(uuid4()), "text": "second activation"},
            )
            assert response.status_code == 200, response.text
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                note = {"message_id": str(uuid4()), "text": "new activation note"}
                response = await client.post(f"{path}/{task_id}/messages", headers=headers, json=note)
                assert response.json()["status"] == "queued"
                run = state.run_for(root)
                assert run is not None
                assert state.executions.owners[root].tasks._lookup(task_id).activation == 1
                await state.tasks.record(
                    run,
                    {
                        "event": "task_completed",
                        "session_id": task_id,
                        "root_session_id": root,
                        "activation": 0,
                    },
                )
                retry = await client.post(f"{path}/{task_id}/messages", headers=headers, json=note)
                assert retry.json()["status"] == "queued"
            finally:
                release.set()
            await until(lambda: state.run_for(root) is None)
            detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
            assert detail["receipts"][-1]["status"] == "picked_up"

    asyncio.run(scenario())


def test_completion_transaction_racing_next_activation_only_fails_matching_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        completing, release_completion, completed_transaction, resumed, release_child = (
            asyncio.Event() for _ in range(5)
        )
        parent_waiting, release_parent = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent != "reviewer":
                # Hold a real parent model round, not merely the web event
                # consumer. Once the core producer finishes, its task group
                # correctly stops accepting same-run child continuations.
                parent_waiting.set()
                await release_parent.wait()
            elif messages[-1].content == "resume":
                resumed.set()
                await release_child.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            original = state.tasks.store.fail_task

            async def delayed(root: str, session_id: str, activation: int) -> list[dict[str, object]]:
                if activation == 0:
                    completing.set()
                    await release_completion.wait()
                result = await original(root, session_id, activation)
                if activation == 0:
                    completed_transaction.set()
                return result

            monkeypatch.setattr(state.tasks.store, "fail_task", delayed)
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            response = await client.post(
                path, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "first"}
            )
            task_id = response.json()["task"]["session_id"]
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await completing.wait()
                    await parent_waiting.wait()
                await until(lambda: state.executions.owners[root].tasks._workers[task_id].done())
                followup = await client.post(
                    f"{path}/{task_id}/messages", headers=headers, json={"message_id": str(uuid4()), "text": "resume"}
                )
                assert followup.status_code == 200, followup.text
                async with asyncio.timeout(HANG_GUARD):
                    await resumed.wait()
                note = {"message_id": str(uuid4()), "text": "queued for second activation"}
                queued = await client.post(f"{path}/{task_id}/messages", headers=headers, json=note)
                assert queued.json()["status"] == "queued" and queued.json()["activation"] == 1
                release_completion.set()
                async with asyncio.timeout(HANG_GUARD):
                    await completed_transaction.wait()
                duplicate = await client.post(f"{path}/{task_id}/messages", headers=headers, json=note)
                assert duplicate.json()["status"] == "queued"
            finally:
                release_completion.set()
                release_child.set()
                release_parent.set()
            await until(lambda: state.run_for(root) is None)
            detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
            assert all(receipt["status"] == "picked_up" for receipt in detail["receipts"])

    asyncio.run(scenario())


def test_profile_catalog_contains_unused_but_never_enables_main_only_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            state.harness.config.profiles["private"] = AgentProfile(description="Not a child", subagent_available=False)
            state.harness.config.profiles["internal"] = AgentProfile(description="Tool-only", user_invocable=False)
            path = f"/api/sessions/{state.selected_session_id}/tasks"
            profiles = {
                profile["id"]: profile for profile in (await client.get(path, headers=headers)).json()["profiles"]
            }
            assert profiles["assistant"]["used"]
            assert not profiles["private"]["available"] and not profiles["internal"]["available"]
            for profile in ("private", "internal"):
                rejected = await client.post(
                    path, headers=headers, json={"message_id": str(uuid4()), "agent": profile, "text": "start"}
                )
                assert rejected.status_code == 403
            assert not state.executions.runs

    asyncio.run(scenario())


def test_task_profile_exposes_secret_free_auth_and_workspace_remote_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider, messages
            yield TextDoneEvent(text="done")

        config_home = tmp_path / "private-config"
        data_home = tmp_path / "private-data"
        config_home.mkdir(mode=0o700)
        config_home.chmod(0o700)
        (config_home / "ngn" / "workspaces").mkdir(mode=0o700, parents=True)
        (config_home / "ngn").chmod(0o700)
        (config_home / "ngn" / "workspaces").chmod(0o700)
        data_home.mkdir(mode=0o700)
        data_home.chmod(0o700)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
        monkeypatch.setenv("NGN_TASK_REMOTE_TOKEN", "private-task-token")
        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            remote = AgentProfile(
                description="Workspace remote",
                a2a=RemoteProfile(url="https://peer.example", token_env="NGN_TASK_REMOTE_TOKEN"),
            )
            store = state.harness._agent_catalog.remote_store
            initial = store.load()
            saved = store.save({"remote-peer": remote}, expected=initial.revision)
            state.harness.refresh_agent_definitions()
            path = f"/api/sessions/{state.selected_session_id}/tasks"

            first = await client.get(path, headers=headers)
            assert first.status_code == 200
            profiles = {profile["id"]: profile for profile in first.json()["profiles"]}
            assert profiles["assistant"]["authentication"] == {
                "state": "not_required",
                "expires_at": 0.0,
                "error_code": "",
                "refreshable": False,
            }
            item = profiles["remote-peer"]
            assert item["authentication"] == {
                "state": "token_ready",
                "expires_at": 0.0,
                "error_code": "",
                "refreshable": False,
            }
            assert item["trust_remote_tools"] is False
            assert "private-task-token" not in json.dumps(first.json())

            trusted = store.save(
                saved.agents,
                expected=saved.revision,
                trust_updates=(("remote-peer", True),),
            )
            state.harness.refresh_agent_definitions()
            second = await client.get(path, headers=headers)
            item = next(profile for profile in second.json()["profiles"] if profile["id"] == "remote-peer")
            assert item["trust_remote_tools"] is True
            assert item["authentication"]["state"] == "token_ready"
            assert store.load().revision == trusted.revision
            assert "private-task-token" not in json.dumps(second.json())

    asyncio.run(scenario())


def test_profile_start_observer_survives_http_waiter_removing_its_ready_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        observer_binding, release_observer, observer_bound, child_running, release_child = (
            asyncio.Event() for _ in range(5)
        )

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "reviewer" and messages[-1].content == "initial":
                child_running.set()
                await release_child.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            message_id = str(uuid4())
            bind = state.tasks.store.bind

            async def ordered_bind(identifier: str, task: dict[str, object], storage_id: str) -> dict[str, object]:
                run = state.run_for(root)
                if identifier == message_id and run is not None:
                    if asyncio.current_task() is run.task:
                        observer_binding.set()
                        await release_observer.wait()
                        receipt = await bind(identifier, task, storage_id)
                        observer_bound.set()
                        return receipt
                    # The actual child input completes its binding first, after
                    # the observer has captured the same HTTP readiness event.
                    await observer_binding.wait()
                return await bind(identifier, task, storage_id)

            monkeypatch.setattr(state.tasks.store, "bind", ordered_bind)
            try:
                response = await client.post(
                    path, headers=headers, json={"message_id": message_id, "agent": "reviewer", "text": "initial"}
                )
                assert response.status_code == 200, response.text
                task_id = response.json()["task"]["session_id"]
                async with asyncio.timeout(HANG_GUARD):
                    await child_running.wait()
                assert message_id not in state.tasks.created
                release_observer.set()
                async with asyncio.timeout(HANG_GUARD):
                    await observer_bound.wait()
                    while True:
                        detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
                        if detail["activity_cursor"] > 0 or state.run_for(root) is None:
                            break
                        await asyncio.sleep(0)
                assert state.run_for(root) is not None, "A completed HTTP waiter must not fail the owned run"
                assert detail["receipts"][0]["status"] == "picked_up"
            finally:
                release_observer.set()
                release_child.set()
            await until(lambda: state.run_for(root) is None)
            assert state.executions.owners[root].tasks._lookup(task_id).status == "completed"
            continued = await client.post(
                f"{path}/{task_id}/messages", headers=headers, json={"message_id": str(uuid4()), "text": "continue"}
            )
            assert continued.status_code == 200, continued.text
            await until(lambda: state.run_for(root) is None)
            detail = (await client.get(f"{path}/{task_id}", headers=headers)).json()
            assert detail["receipts"][-1]["status"] == "picked_up"

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["before_insert", "during_insert", "timeout"])
def test_child_followup_waits_for_exact_draining_owner_without_blocking_other_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    async def scenario() -> None:
        parent_waiting, release_parent, draining, release_cleanup, inserted, release_insert, waiting = (
            asyncio.Event() for _ in range(7)
        )
        if stage == "before_insert":
            release_parent.set()
        old_runs: list[Run] = []
        seen: list[str] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            if provider.index == 0:
                parent_waiting.set()
                await release_parent.wait()
            if provider.harness_config.agent == "reviewer":
                seen.append(prompt)
                if prompt == "follow":
                    assert old_runs[0].task.done(), "Continuation must start only after its previous web owner joined"
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            finish, insert, wait_for_drain = state.tasks.finish, state.tasks.store.insert, state.tasks.wait_for_drain
            note = {"message_id": str(uuid4()), "text": "follow"}

            async def held_finish(run: Run) -> None:
                if run.session_id == root and not old_runs:
                    old_runs.append(run)
                    draining.set()
                    await release_cleanup.wait()
                await finish(run)

            async def held_insert(
                root: str, target: str, identifier: str, text: str, session_id: str = ""
            ) -> dict[str, object]:
                receipt = await insert(root, target, identifier, text, session_id)
                if identifier == note["message_id"] and stage != "before_insert":
                    inserted.set()
                    await release_insert.wait()
                return receipt

            async def observed_wait(run: Run, deadline: float) -> None:
                waiting.set()
                await wait_for_drain(run, deadline)

            monkeypatch.setattr(state.tasks, "finish", held_finish)
            monkeypatch.setattr(state.tasks.store, "insert", held_insert)
            monkeypatch.setattr(state.tasks, "wait_for_drain", observed_wait)
            if stage == "timeout":
                monkeypatch.setattr(task_api, "TASK_DRAIN_TIMEOUT", 0.0)
            response = await client.post(
                path, headers=headers, json={"message_id": str(uuid4()), "agent": "reviewer", "text": "first"}
            )
            assert response.status_code == 200, response.text
            task_id = response.json()["task"]["session_id"]
            async with asyncio.timeout(HANG_GUARD):
                await parent_waiting.wait()
                if stage == "before_insert":
                    await draining.wait()
            request = asyncio.create_task(client.post(f"{path}/{task_id}/messages", headers=headers, json=note))
            try:
                async with asyncio.timeout(HANG_GUARD):
                    if stage != "before_insert":
                        await inserted.wait()
                        release_parent.set()
                        await draining.wait()
                        release_insert.set()
                    await waiting.wait()
                if stage == "timeout":
                    failed = await request
                    assert failed.status_code == 409
                    receipt = await state.tasks.store.duplicate(root, task_id, note["message_id"], note["text"])
                    assert receipt["status"] == "error"
                    assert state.executions.owners[root].tasks._lookup(task_id).activation == 0
                    assert seen.count("follow") == 0
                    return
                assert not request.done()
                duplicate = await state.tasks.store.duplicate(root, task_id, note["message_id"], note["text"])
                if stage == "before_insert":
                    assert duplicate == {}, "Draining was known before admission; no receipt should be journaled yet"
                else:
                    assert duplicate["status"] == "queued" and duplicate["owner_run_id"] == ""
                # A separate chat can start while this request joins old cleanup.
                await state.harness.create_session("ngn-independent-root")
                async with asyncio.timeout(HANG_GUARD):
                    other = await client.post(
                        "/api/sessions/ngn-independent-root/tasks",
                        headers=headers,
                        json={"message_id": str(uuid4()), "agent": "reviewer", "text": "other"},
                    )
                assert other.status_code == 200 and other.json()["task"]["session_id"]
                used = state.executions.owners[root].tasks._used
                retry = asyncio.create_task(client.post(f"{path}/{task_id}/messages", headers=headers, json=note))
                release_cleanup.set()
                async with asyncio.timeout(HANG_GUARD):
                    accepted, repeated = await asyncio.gather(request, retry)
                assert accepted.status_code == repeated.status_code == 200
                assert accepted.json()["message_id"] == repeated.json()["message_id"] == note["message_id"]
                await until(lambda: state.run_for(root) is None)
                receipt = await state.tasks.store.duplicate(root, task_id, note["message_id"], note["text"])
                assert receipt["status"] == "picked_up" and receipt["owner_run_id"] != old_runs[0].id
                assert state.executions.owners[root].tasks._used == used + 1
                assert seen.count("follow") == 1
            finally:
                release_parent.set()
                release_insert.set()
                release_cleanup.set()
                await request

    asyncio.run(scenario())


def test_child_followup_joins_post_run_web_claim_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        finishing, release_claim, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            if provider.index == 0 and messages[-1].role == "user" and prompt == "to_agent":
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "first", "agent": "reviewer"})
            else:
                if prompt == "follow":
                    assert root not in state.channels.work_tasks
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            root_message_id = str(uuid4())
            finish_work, wait_for_claim = state.channels.store.finish_work, state.tasks.wait_for_claim

            async def held_finish(work: Work, status: str) -> None:
                if work.message_id == root_message_id:
                    assert state.run_for(root) is None
                    finishing.set()
                    await release_claim.wait()
                await finish_work(work, status)

            async def observed_wait(task: asyncio.Task[None], deadline: float) -> None:
                assert task is state.channels.work_tasks[root]
                waiting.set()
                await wait_for_claim(task, deadline)

            monkeypatch.setattr(state.channels.store, "finish_work", held_finish)
            monkeypatch.setattr(state.tasks, "wait_for_claim", observed_wait)
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={"session_id": root, "message_id": root_message_id, "prompt": "to_agent"},
            )
            assert response.status_code == 200, response.text
            async with asyncio.timeout(HANG_GUARD):
                await finishing.wait()
            task_id = state.executions.owners[root].tasks.list()[0].session_id
            note = {"message_id": str(uuid4()), "text": "follow"}
            request = asyncio.create_task(
                client.post(f"/api/sessions/{root}/tasks/{task_id}/messages", headers=headers, json=note)
            )
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await waiting.wait()
                assert not request.done()
                assert await state.tasks.store.duplicate(root, task_id, note["message_id"], note["text"]) == {}
            finally:
                release_claim.set()
            response = await request
            assert response.status_code == 200, response.text
            await until(lambda: state.run_for(root) is None)
            receipt = await state.tasks.store.duplicate(root, task_id, note["message_id"], note["text"])
            assert receipt["status"] == "picked_up"

    asyncio.run(scenario())


def test_root_inbox_uuid_does_not_own_unbound_child_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            ordinary_id, task_id = str(uuid4()), str(uuid4())
            await state.tasks.store.validate(root)
            await state.tasks.store.insert(root, "retained", ordinary_id, "waiting for admission", "s1")
            await state.tasks.store.insert(root, "profile:reviewer", task_id, "initial task input")
            await state.tasks.finish(Run(root, server_owned=True, message_id=ordinary_id))
            unbound = await state.tasks.store.duplicate(root, "retained", ordinary_id, "waiting for admission")
            assert unbound["status"] == "queued", "A normal root inbox UUID is not authority over a task receipt"
            await state.tasks.finish(Run(root, server_owned=True, message_id=task_id, task_message_id=task_id))
            owned = await state.tasks.store.duplicate(root, "profile:reviewer", task_id, "initial task input")
            assert owned["status"] == "error"
            unbound = await state.tasks.store.duplicate(root, "retained", ordinary_id, "waiting for admission")
            assert unbound["status"] == "queued"

    asyncio.run(scenario())
