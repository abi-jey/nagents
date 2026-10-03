"""Task lifecycle progress is distinct from boundary-only model notifications."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.runtime import Harness
from nagents.harness.subagents import SubagentManager
from nagents.harness.types import Notice
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskNotification
from nagents.harness.types import TaskStarted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.subagents import TaskInfo
    from nagents.harness.types import HarnessEvent
    from nagents.harness.types import TaskMessage
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.fixture(autouse=True)
def portable_harness_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    # These lifecycle contracts do not read guarded workspace instructions.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_completed_tasks_are_visible_at_next_core_event_without_early_model_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    gate_entered, published, advance, held, visible, release = (asyncio.Event() for _ in range(6))
    queued: list[TaskCompleted] = []
    observed: list[HarnessEvent] = []
    publish = SubagentManager._publish

    def record_publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
        publish(manager, info, event)
        if isinstance(event, TaskCompleted):
            queued.append(event)
            if len(queued) == (2 if nested else 1):
                published.set()

    monkeypatch.setattr(SubagentManager, "_publish", record_publish)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            await gate_entered.wait()
            if nested and provider.index == 1:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="grandchild", name="delegate", arguments={"prompt": "Nested review"})
                elif notifications(messages):
                    yield TextDoneEvent(text="Child evaluated grandchild")
                else:
                    yield TextDoneEvent(text="Child preliminary response")
            else:
                yield TextDoneEvent(text="Independent child evidence")
        elif notifications(messages):
            yield TextDoneEvent(text="Parent synthesized child evidence")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Independent review"})
            yield ToolCallEvent(id="gate", name="publication_gate")
        elif len(provider.requests) == 2:
            yield TextChunkEvent(chunk="Parent still working")
            yield ToolCallEvent(id="work", name="parent_work")
        else:
            yield TextDoneEvent(text="Parent own work complete")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def publication_gate() -> str:
        gate_entered.set()
        await advance.wait()
        return "Parent continues independently"

    async def parent_work() -> str:
        held.set()
        await release.wait()
        return "Parent operation completed"

    for tool in (publication_gate, parent_work):
        harness.agent.register_tool(tool)
        harness.tools.builtins[tool.__name__] = tool

    async def consume() -> None:
        async with aclosing(harness.run("Do independent work")) as events:
            async for event in events:
                observed.append(event)
                if isinstance(event, ToolCallEvent) and event.id == "work":
                    visible.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(published.wait(), HANG_GUARD)
        assert all(info.status == "completed" for info in harness.tasks.list())
        assert all(provider.closed for provider in providers[1:])
        # Quiet work does not gain a second observer task or an artificial wake-up.
        assert not any(isinstance(event, TaskCompleted) for event in observed)
        assert len(providers[0].requests) == 1
        advance.set()
        await asyncio.wait_for(asyncio.gather(held.wait(), visible.wait()), HANG_GUARD)
        completed = [event for event in observed if isinstance(event, TaskCompleted)]
        assert completed == queued
        assert [event.depth for event in completed] == ([2, 1] if nested else [1])
        if nested:
            assert completed[0].parent_task_id == completed[1].task_id
            assert completed[0].parent_session_id == completed[1].child_session_id
        assert completed[-1].parent_task_id == ""
        assert completed[-1].parent_session_id == harness.session_id
        gate_result = next(
            index for index, event in enumerate(observed) if isinstance(event, ToolResultEvent) and event.id == "gate"
        )
        assert all(observed.index(event) < gate_result for event in completed)
        assert not task.done()
        assert not harness.tasks._observed
        assert not any(isinstance(event, TaskNotification) and event.recipient_task_id == "" for event in observed)
        assert not notifications(await harness.history())
        assert len(providers[0].requests) == 2
        assert all(not notifications(messages) for messages in providers[0].requests)

        release.set()
        await asyncio.wait_for(task, HANG_GUARD)
        assert [event for event in observed if isinstance(event, TaskCompleted)] == completed
        delivered = [event for event in observed if isinstance(event, TaskNotification)]
        assert len(delivered) == len(completed)
        root_delivery = [event for event in delivered if event.recipient_task_id == ""]
        assert len(root_delivery) == 1 and root_delivery[0].source_task_id == completed[-1].task_id
        assert len(notifications(await harness.history())) == 1
        assert len(providers[0].requests) == 4
        assert isinstance(observed[-1], DoneEvent)
        assert observed[-1].final_text == "Parent synthesized child evidence"
    finally:
        advance.set()
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_closing_while_progress_flush_waits_on_full_queue_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate_entered, release_child, published, flushing = (asyncio.Event() for _ in range(4))
    publish = SubagentManager._publish
    emit = Harness.emit

    def record_publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
        publish(manager, info, event)
        if isinstance(event, TaskCompleted):
            published.set()

    async def record_emit(harness: Harness, event: HarnessEvent) -> None:
        if not harness._is_subagent and isinstance(event, TaskCompleted):
            flushing.set()
        await emit(harness, event)

    monkeypatch.setattr(SubagentManager, "_publish", record_publish)
    monkeypatch.setattr(Harness, "emit", record_emit)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            await release_child.wait()
            yield TextDoneEvent(text="Retained completed evidence")
        else:
            yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Independent review"})
            yield ToolCallEvent(id="gate", name="publication_gate")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def publication_gate() -> str:
        gate_entered.set()
        await published.wait()
        return "Next core result flushes queued lifecycle"

    harness.agent.register_tool(publication_gate)
    harness.tools.builtins["publication_gate"] = publication_gate
    stream = harness.run("Work until stopped")
    try:
        while not isinstance(await asyncio.wait_for(anext(stream), HANG_GUARD), TaskStarted):
            pass
        await asyncio.wait_for(gate_entered.wait(), HANG_GUARD)
        assert harness._queue is not None
        while not harness._queue.full():
            harness._queue.put_nowait(Notice("Slow observer"))
        release_child.set()
        await asyncio.wait_for(flushing.wait(), HANG_GUARD)
        assert harness._queue.full()
        await asyncio.wait_for(stream.aclose(), HANG_GUARD)
        assert harness._queue is None and harness._worker is None
        assert all(worker.done() for worker in harness.tasks._workers.values())
        assert not harness.tasks._observed
        assert providers[1].closed
        info = harness.tasks.list()[0]
        assert info.status == "completed" and info.result == "Retained completed evidence"
        assert not notifications(await harness.history())
    finally:
        release_child.set()
        await stream.aclose()
        await harness.close()
