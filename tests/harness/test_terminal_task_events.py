"""Completed task evidence survives a terminal parent error without auto-resume."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.subagents import SubagentManager
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
    from nagents.harness.subagents import TaskInfo
    from nagents.harness.types import TaskMessage
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("failure", ["provider_error", "round_limit"])
def test_terminal_parent_error_flushes_completed_lifecycle_without_waiting_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool, failure: str
) -> None:
    async def scenario() -> None:
        completed, running = asyncio.Event(), asyncio.Event()
        never = asyncio.Event()
        gate_calls = 0
        original_publish = SubagentManager._publish

        def publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
            original_publish(manager, info, event)
            if isinstance(event, TaskCompleted) and event.result == "retained completed result":
                completed.set()

        monkeypatch.setattr(SubagentManager, "_publish", publish)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "review"})
                    if not nested:
                        yield ToolCallEvent(id="running-child", name="delegate", arguments={"prompt": "wait"})
                    yield ToolCallEvent(id="gate", name="publication_gate")
                elif len(provider.requests) == 2:
                    yield ErrorEvent(message="original terminal provider error", recoverable=False)
                else:
                    yield TextDoneEvent(text="new human turn")
            elif provider.index == (2 if nested else 1):
                yield TextDoneEvent(text="retained completed result")
            else:
                if nested and len(provider.requests) == 1:
                    yield ToolCallEvent(id="grandchild", name="delegate", arguments={"prompt": "review"})
                else:
                    running.set()
                    await never.wait()

        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def publication_gate() -> str:
            nonlocal gate_calls
            gate_calls += 1
            await asyncio.gather(completed.wait(), running.wait())
            return "one operation, with one completed and one still-running child"

        harness.agent.register_tool(publication_gate)
        harness.tools.builtins["publication_gate"] = publication_gate
        if failure == "round_limit":
            # Exercise the real root budget without shrinking the child's budget.
            harness.agent.max_tool_rounds = 1
        try:
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            errors = [event for event in events if isinstance(event, ErrorEvent)]
            expected = (
                "Max tool rounds (1) exceeded" if failure == "round_limit" else "original terminal provider error"
            )
            assert any(event.message == expected for event in errors)
            assert len(providers[0].requests) == (1 if failure == "round_limit" else 2)
            assert gate_calls == 1
            assert not any(isinstance(event, TaskNotification) for event in events)
            assert not notifications(await harness.history())
            tasks = harness.tasks.list()
            finished = next(info for info in tasks if info.status == "completed")
            cancelled = next(info for info in tasks if info.status == "cancelled")
            assert finished.result == "retained completed result"
            assert finished.parent_task_id == (cancelled.id if nested else "")
            completions = [event for event in events if isinstance(event, TaskCompleted)]
            assert len(completions) == 1
            assert completions[0].task_id == finished.id
            assert completions[0].parent_task_id == finished.parent_task_id
            assert completions[0].parent_session_id == finished.parent_session_id
            assert all(provider.closed for provider in providers[1:])
            assert await harness.task_history(finished.id)

            # A fresh human turn can inspect retained data, but never replays the
            # failed turn's queued model notification or completed operation.
            providers[0].script = fresh_turn
            harness.agent.max_tool_rounds = 30
            later = await asyncio.wait_for(collect(harness, "a new human request"), HANG_GUARD)
            assert not any(isinstance(event, TaskCompleted | TaskNotification) for event in later)
            assert gate_calls == 1
            assert not notifications(await harness.history())
        finally:
            await harness.close()

    async def fresh_turn(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="new human turn")

    asyncio.run(scenario())


def test_child_fatal_error_does_not_consume_root_lifecycle_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                yield ErrorEvent(message="child failed", recoverable=False)
            elif notifications(messages):
                yield TextDoneEvent(text="parent evaluated failed child")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "review"})
            else:
                yield TextDoneEvent(text="parent preliminary response")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            completions = [event for event in events if isinstance(event, TaskCompleted)]
            delivered = [event for event in events if isinstance(event, TaskNotification)]
            assert len(completions) == len(delivered) == 1
            assert completions[0].status == "failed" and completions[0].error
            assert delivered[0].source_task_id == completions[0].task_id
            assert delivered[0].recipient_task_id == ""
            assert len(providers[0].requests) == 3
        finally:
            await harness.close()

    asyncio.run(scenario())
