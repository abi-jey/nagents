"""A successful child retry remains a successful result for its parent."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.runtime import Harness
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskNotification
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.types import HarnessEvent
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.fixture(autouse=True)
def portable_harness_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    # These lifecycle contracts do not read guarded workspace instructions.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recoverable", "terminal_after_retry"),
    [(True, False), (False, False), (True, True)],
    ids=["recovered", "terminal", "retry-then-terminal"],
)
async def test_child_completion_reflects_recovery_and_routes_result_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recoverable: bool,
    terminal_after_retry: bool,
) -> None:
    child_events: list[HarnessEvent] = []
    emit = Harness.emit

    async def observe(harness: Harness, event: HarnessEvent) -> None:
        if harness._is_subagent:
            child_events.append(event)
        await emit(harness, event)

    monkeypatch.setattr(Harness, "emit", observe)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            if len(provider.requests) == 1:
                yield ErrorEvent(message="private first provider error", recoverable=recoverable)
            elif terminal_after_retry:
                yield ErrorEvent(message="private terminal provider error", recoverable=False)
            else:
                yield TextDoneEvent(text="Recovered child result")
        elif notifications(messages):
            yield TextDoneEvent(text="Parent considered child outcome")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Review this fixture"})
        else:
            yield TextDoneEvent(text="Parent preliminary response")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        events = await asyncio.wait_for(collect(harness), HANG_GUARD)
        recovered = recoverable and not terminal_after_retry
        expected_result = "Recovered child result" if recovered else ""
        expected_status = "completed" if recovered else "failed"
        tasks = harness.tasks.list()
        assert len(tasks) == 1
        info = tasks[0]
        assert info.status == expected_status
        assert info.result == expected_result
        assert bool(info.error) is not recovered
        assert len(providers[1].requests) == (2 if recoverable else 1)
        assert providers[1].closed

        # Recovery changes the outcome, not the original observable error event.
        errors = [event for event in child_events if isinstance(event, ErrorEvent)]
        assert [event.recoverable for event in errors] == (
            [recoverable, False] if terminal_after_retry else [recoverable]
        )
        done = [event for event in child_events if isinstance(event, DoneEvent)]
        assert len(done) == 1 and done[0].final_text == expected_result
        history = await harness.task_history(info.id)
        assert [message.content for message in history if message.role == "assistant"] == (
            [expected_result] if recovered else []
        )

        completed = [event for event in events if isinstance(event, TaskCompleted)]
        delivered = [event for event in events if isinstance(event, TaskNotification)]
        assert len(completed) == len(delivered) == 1
        assert completed[0].task_id == info.id == delivered[0].source_task_id
        assert completed[0].status == expected_status
        assert completed[0].result == expected_result
        assert completed[0].error == info.error
        assert completed[0].parent_session_id == harness.session_id
        assert completed[0].child_session_id == info.child_session_id
        assert delivered[0].recipient_task_id == ""
        notes = notifications(await harness.history())
        assert len(notes) == 1
        payload = json.loads(str(notes[0].content).partition("\n")[2])
        assert payload["tasks"][0]["result"] == expected_result
        assert payload["tasks"][0]["status"] == expected_status
        assert "private" not in repr(completed) + repr(notes)
        assert len(providers[0].requests) == 3
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].final_text == "Parent considered child outcome"
    finally:
        await harness.close()
