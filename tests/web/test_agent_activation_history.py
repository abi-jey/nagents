"""Durable web history records every child activation without exposing storage IDs."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import TypeGuard

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.session_identity import SessionIdentityStore
from nagents.harness.subagents import TaskInfo
from tests.support.providers import notifications
from tests.support.web import submit_and_wait
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


def is_activation_list(value: object) -> TypeGuard[list[dict[str, object]]]:
    if not isinstance(value, list):
        return False
    return all(
        isinstance(record, dict)
        and isinstance(record.get("session_id"), str)
        and type(record.get("activation")) is int
        and isinstance(record.get("title"), str)
        and isinstance(record.get("status"), str)
        for record in value
    )


@pytest.mark.requires_posix
def test_activation_history_survives_restart_and_stays_root_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        did_agent_continue = False
        script_requests: list[tuple[int, int, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            nonlocal did_agent_continue
            script_requests.append((provider.index, len(provider.requests), provider.model))
            if provider.index:
                yield TextDoneEvent(text=f"Child result {provider.index}")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="activation-one",
                    name="to_agent",
                    arguments={
                        "prompt": "Initial assignment",
                        "agent": "reviewer",
                        "title": "Original title",
                    },
                )
            elif notifications(messages) and not did_agent_continue:
                did_agent_continue = True
                yield ToolCallEvent(
                    id="activation-two",
                    name="to_agent",
                    arguments={
                        "prompt": "Continue the same child",
                        "session_id": "s1",
                        "title": "Updated title",
                        "model": "updated-model",
                    },
                )
            else:
                yield TextDoneEvent(text="Parent complete")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, providers, _):
            root = state.selected_session_id
            events = await submit_and_wait(app, headers, root, "Start and continue the child")
            assert any(event.get("event") == "task_started" for event in events)
            index = await state.tasks.index(root)
            activations_value = index.get("activations")
            assert is_activation_list(activations_value)
            activations = activations_value
            assert len(activations) == 2
            first, second = activations
            alias = str(first["session_id"])
            assert alias == second["session_id"] == "s1"
            assert [first["activation"], second["activation"]] == [0, 1]
            assert first["title"] == "Original title" and second["title"] == "Updated title"
            assert first["mode"] == second["mode"] == "reviewer"
            assert first["mode"] == second["mode"] == "reviewer"
            assert first["status"] == second["status"] == "completed"
            assert first["result"] == "Child result 1" and second["result"] == "Child result 2"
            assert first["activation_call_id"] == "activation-one"
            assert second["activation_call_id"] == "activation-two"
            assert first["activation_history_id"] and second["activation_history_id"]
            assert first["activation_history_id"] != second["activation_history_id"]
            assert first["origin_history_id"] == second["origin_history_id"]
            await state.tasks.store.record_activation(
                root,
                {
                    "event": "task_completed",
                    "session_id": alias,
                    "root_session_id": root,
                    "activation": 0,
                    "title": "Stale completion",
                    "status": "failed",
                    "result": "must not overwrite the first completion",
                    "error": "stale failure",
                },
            )
            unchanged_value = (await state.tasks.index(root)).get("activations")
            assert is_activation_list(unchanged_value)
            unchanged = unchanged_value
            assert unchanged[0]["title"] == "Original title"
            assert unchanged[0]["status"] == "completed" and unchanged[0]["result"] == "Child result 1"
            assert unchanged[1]["result"] == "Child result 2"

            details = (await client.get(f"/api/sessions/{root}/tasks/{alias}", headers=headers)).json()
            detail_activations = details.get("retained_activations")
            assert is_activation_list(detail_activations)
            assert [item["activation"] for item in detail_activations] == [0, 1]
            cold = await state.snapshot(root)
            cold_activations = cold.get("retained_activations")
            assert is_activation_list(cold_activations)
            assert [item["activation"] for item in cold_activations] == [0, 1]
            private_storage_id = state.executions.owners[root].tasks._lookup(alias)._storage_id
            assert private_storage_id not in json.dumps(index)
            assert private_storage_id not in json.dumps(details)
            assert private_storage_id not in json.dumps(cold)
            assert providers[-1].model == "updated-model"

            incomplete_identity = await SessionIdentityStore(state.history.db_path).reserve(root, "", "Interrupted")
            incomplete = TaskInfo(
                incomplete_identity.session_id,
                "Interrupted",
                root_session_id=root,
                _storage_id=incomplete_identity.storage_id,
                status="running",
            )
            await state.history.get_or_create_session(incomplete_identity.storage_id, "harness")
            await state.tasks.store.save(root, asdict(incomplete), incomplete_identity.storage_id)
            await state.tasks.store.record_activation(
                root,
                {
                    "event": "task_started",
                    "session_id": incomplete_identity.session_id,
                    "root_session_id": root,
                    "activation": 0,
                    "title": "Interrupted",
                    "status": "running",
                    "timestamp": 1.0,
                },
            )
            other_root = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            assert (await client.get(f"/api/sessions/{other_root}/tasks/{alias}", headers=headers)).status_code == 404

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, _):
            resumed = await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            assert resumed.status_code == 200, resumed.text
            restarted_index = await state.tasks.index(root)
            retained_value = restarted_index.get("activations")
            assert is_activation_list(retained_value)
            retained = retained_value
            assert [item["activation"] for item in retained if item["session_id"] == alias] == [0, 1]
            assert [item["title"] for item in retained if item["session_id"] == alias] == [
                "Original title",
                "Updated title",
            ]
            interrupted = next(item for item in retained if item["session_id"] == incomplete_identity.session_id)
            assert interrupted["status"] == "cancelled"
            assert interrupted["can_message"] is False and interrupted["can_start_children"] is False
            assert interrupted["error"] == "Interrupted by server restart."
            detail = (await client.get(f"/api/sessions/{root}/tasks/{alias}", headers=headers)).json()
            detail_activations = detail.get("retained_activations")
            assert is_activation_list(detail_activations)
            assert [item["activation"] for item in detail_activations if item["session_id"] == alias] == [0, 1]
            tasks_value = restarted_index.get("tasks")
            assert is_activation_list(tasks_value)
            latest = next(item for item in tasks_value if item["session_id"] == alias)
            assert latest["can_message"] is False and latest["status"] == "completed"
            assert root not in state.executions.owners
            assert not any(provider.requests for provider in providers)

            deleted = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True})
            assert deleted.status_code == 200, deleted.text
            remaining = await state.history.store._transaction(
                lambda db: db.execute(
                    "SELECT COUNT(*) FROM ngn_web_agent_activations WHERE root = ?", (root,)
                ).fetchone()[0]
            )
            assert remaining == 0

    asyncio.run(scenario())
