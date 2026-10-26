"""Deleting a root cancels its nested task ownership tree."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.types import Message
from nagents.web.execution.wakeups import Chain
from tests.support.hang_guard import HANG_GUARD
from tests.web.execution.test_web_subscription_lifecycle import until
from tests.web.execution.test_web_wakeups import scheduled_app
from tests.web.test_web_deletion import rows

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from tests.support.providers import FakeProvider


pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("permanent", [False, True])
def test_delete_cancels_nested_tasks_and_quarantines_queued_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, permanent: bool
) -> None:
    async def scenario() -> None:
        child_started = asyncio.Event()
        child_cancelled = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(id="to_agent", name="to_agent", arguments={"prompt": "nested worker"})
                yield TextDoneEvent(text="parent finished")
            elif provider.index == 2 and len(provider.requests) == 1:
                child_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    child_cancelled.set()
            else:
                yield TextDoneEvent(text="completed")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, providers, _):
            root = state.selected_session_id
            lookalike = f"{root}-unowned"
            await state.history.get_or_create_session(lookalike, "harness")
            await state.history.add_message(lookalike, Message(role="user", content="preserve this"))
            started = await client.post(
                f"/api/sessions/{root}/tasks",
                headers=headers,
                json={"message_id": str(uuid4()), "agent": "reviewer", "text": "start nested work"},
            )
            assert started.status_code == 200, started.text
            task_id = started.json()["task"]["session_id"]
            async with asyncio.timeout(HANG_GUARD):
                await child_started.wait()
            nested = next(info for info in state.harness.tasks.list() if info.depth == 2)
            nested_child = nested._storage_id
            state.wakeups.schedule(root, "active-run", Chain(), task_id, 3600, "pending test timer")
            queued_id = str(uuid4())
            queued = await client.post(
                f"/api/sessions/{root}/tasks/{nested.session_id}/messages",
                headers=headers,
                json={"message_id": queued_id, "text": "must not replay"},
            )
            assert queued.status_code == 200 and queued.json()["status"] == "queued", queued.text
            assert state.wakeups.pending
            response = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": permanent}
            )
            assert response.status_code == 200, response.text
            async with asyncio.timeout(HANG_GUARD):
                await child_cancelled.wait()
                await until(lambda: state.run_for(root) is None)
            assert not state.wakeups.pending
            assert all(provider.closed for provider in providers[1:])
            assert await rows(state, "SELECT status FROM ngn_web_agent_inputs WHERE message_id = ?", queued_id) == [
                ("error",)
            ]
            if permanent:
                assert not await rows(state, "SELECT id FROM harness_sessions WHERE id = ?", nested_child)
                assert not await state.history.get_history(nested_child)
            else:
                item = response.json()["trash"]
                restored = await client.post(
                    f"/api/trash/{root}/restore",
                    headers=headers,
                    json={"deletion_id": item["deletion_id"]},
                )
                assert restored.status_code == 200, restored.text
                assert await state.history.get_history(nested_child)
            assert await rows(state, "SELECT content FROM v2_messages WHERE session_id = ?", lookalike) == [
                ("preserve this",)
            ]
            # Neither a queued receipt nor the cancelled child may restart work.
            assert not state.run_for(root)
            if not permanent:
                second = await client.request(
                    "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
                )
                assert second.status_code == 200, second.text
                assert await state.history.get_history(nested_child) == []

    asyncio.run(scenario())
