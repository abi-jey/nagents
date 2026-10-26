"""Task lifecycle progress is distinct from boundary-only model notifications."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.agents.subagents import SubagentManager
from nagents.harness.runtime import Harness
from nagents.harness.types import Notice
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskNotification
from nagents.harness.types import TaskStarted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.agents.subagents import TaskInfo
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
                    yield ToolCallEvent(id="grandchild", name="to_agent", arguments={"prompt": "Nested review"})
                elif notifications(messages):
                    yield TextDoneEvent(text="Child evaluated grandchild")
                else:
                    yield TextDoneEvent(text="Child preliminary response")
            else:
                yield TextDoneEvent(text="Independent child evidence")
        elif notifications(messages):
            yield TextDoneEvent(text="Parent synthesized child evidence")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Independent review"})
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
            assert completed[0].parent_session_id == completed[1].session_id
            assert completed[0].root_session_id == completed[1].root_session_id
        assert completed[-1].parent_session_id == ""
        assert completed[-1].root_session_id == harness.session_id
        gate_result = next(
            index for index, event in enumerate(observed) if isinstance(event, ToolResultEvent) and event.id == "gate"
        )
        assert all(observed.index(event) < gate_result for event in completed)
        assert not task.done()
        assert not harness.tasks._observed
        assert not any(isinstance(event, TaskNotification) and event.recipient_session_id == "" for event in observed)
        assert not notifications(await harness.history())
        assert len(providers[0].requests) == 2
        assert all(not notifications(messages) for messages in providers[0].requests)

        release.set()
        await asyncio.wait_for(task, HANG_GUARD)
        assert [event for event in observed if isinstance(event, TaskCompleted)] == completed
        delivered = [event for event in observed if isinstance(event, TaskNotification)]
        assert len(delivered) == len(completed)
        root_delivery = [event for event in delivered if event.recipient_session_id == ""]
        assert len(root_delivery) == 1 and root_delivery[0].source_session_id == completed[-1].session_id
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
@pytest.mark.parametrize("during_first_token", [False, True])
async def test_child_completion_binds_only_the_current_parent_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_first_token: bool
) -> None:
    (
        parent_start_observed,
        parent_generation_started,
        parent_chunk_seen,
        release_child,
        child_completed,
        release_parent,
    ) = (asyncio.Event() for _ in range(6))
    completions: list[TaskCompleted] = []
    observed: list[HarnessEvent] = []
    original_publish = SubagentManager._publish

    def record_publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
        original_publish(manager, info, event)
        if isinstance(event, TaskCompleted):
            completions.append(event)
            child_completed.set()

    monkeypatch.setattr(SubagentManager, "_publish", record_publish)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            await parent_generation_started.wait()
            await release_child.wait()
            yield TextDoneEvent(text="Child finished during the parent generation")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Independent review"})
        elif len(provider.requests) == 2:
            await parent_start_observed.wait()
            parent_generation_started.set()
            if during_first_token:
                yield TextChunkEvent(chunk="Parent response has started")
            await release_parent.wait()
            yield TextDoneEvent(text="Parent generation finished")
        else:
            yield TextDoneEvent(text="Parent handled the child result")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    parent_starts: list[ResponseMetadataEvent] = []

    async def consume() -> None:
        async for event in harness.run("Start independent work"):
            observed.append(event)
            if isinstance(event, ResponseMetadataEvent) and event.round_number == 1 and event.status == "thinking":
                parent_starts.append(event)
                parent_start_observed.set()
            elif (
                during_first_token
                and isinstance(event, TextChunkEvent)
                and event.extra.get("response_id") == (parent_starts[0].response_id if parent_starts else None)
            ):
                parent_chunk_seen.set()

    run = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(parent_generation_started.wait(), HANG_GUARD)
        assert len(parent_starts) == 1
        parent_response_id = parent_starts[0].response_id
        assert parent_response_id
        assert harness.agent._active_generation_id(harness.session_id) == parent_response_id
        if during_first_token:
            await asyncio.wait_for(parent_chunk_seen.wait(), HANG_GUARD)
            assert harness.agent._active_generation_id(harness.session_id) == parent_response_id

        release_child.set()
        await asyncio.wait_for(child_completed.wait(), HANG_GUARD)
        assert len(completions) == 1
        completion = completions[0]
        assert completion.parent_session_id == ""
        assert completion.parent_response_id == parent_response_id
        assert completion.parent_response_session_id == harness.session_id
        assert completion.completed_at > 0
        assert harness.tasks.list()[0].parent_response_id == parent_response_id
        assert harness.tasks.list()[0].completed_at == completion.completed_at
        assert harness.agent._active_generation_id(harness.session_id) == parent_response_id

        release_parent.set()
        await asyncio.wait_for(run, HANG_GUARD)
        assert harness.agent._active_generation_id(harness.session_id) == ""
        assert providers[1].closed
        assert any(isinstance(event, DoneEvent) for event in observed)

        previous_response_id = completion.parent_response_id
        resumed = [event async for event in harness.continue_task(completion.session_id, "Resume the child")]
        resumed_completion = next(event for event in resumed if isinstance(event, TaskCompleted))
        assert resumed_completion.activation == completion.activation + 1
        assert resumed_completion.parent_response_id == ""
        assert resumed_completion.parent_response_session_id == ""
        assert resumed_completion.parent_response_id != previous_response_id
        assert harness.tasks.list()[0].parent_response_id == ""
    finally:
        release_child.set()
        release_parent.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_child_completion_during_parent_tool_execution_has_no_generation_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_tool_entered, release_child, child_completed, release_parent_tool = (asyncio.Event() for _ in range(4))
    completions: list[TaskCompleted] = []
    original_publish = SubagentManager._publish

    def record_publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
        original_publish(manager, info, event)
        if isinstance(event, TaskCompleted):
            completions.append(event)
            child_completed.set()

    monkeypatch.setattr(SubagentManager, "_publish", record_publish)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            await parent_tool_entered.wait()
            await release_child.wait()
            yield TextDoneEvent(text="Child finished while the parent tool was running")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Independent review"})
            yield ToolCallEvent(id="hold", name="hold_parent_tool", arguments={})
        else:
            yield TextDoneEvent(text="Parent continues after its tool")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def hold_parent_tool() -> str:
        assert harness.agent._active_generation_id(harness.session_id) == ""
        parent_tool_entered.set()
        await release_parent_tool.wait()
        return "Parent tool finished"

    harness.agent.register_tool(hold_parent_tool)
    harness.tools.builtins["hold_parent_tool"] = hold_parent_tool
    run = asyncio.create_task(collect(harness))
    try:
        await asyncio.wait_for(parent_tool_entered.wait(), HANG_GUARD)
        release_child.set()
        await asyncio.wait_for(child_completed.wait(), HANG_GUARD)
        assert len(completions) == 1
        assert completions[0].parent_response_id == ""
        assert completions[0].parent_response_session_id == ""
        assert harness.tasks.list()[0].parent_response_id == ""
        release_parent_tool.set()
        await asyncio.wait_for(run, HANG_GUARD)
        assert providers[1].closed
    finally:
        release_child.set()
        release_parent_tool.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_nested_completion_uses_immediate_parent_generation_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_generation_started, child_chunk_sent, release_grandchild, grandchild_completed, release_child = (
        asyncio.Event() for _ in range(5)
    )
    completions: list[TaskCompleted] = []
    original_publish = SubagentManager._publish

    def record_publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
        original_publish(manager, info, event)
        if isinstance(event, TaskCompleted):
            completions.append(event)
            if info.depth == 2:
                grandchild_completed.set()

    monkeypatch.setattr(SubagentManager, "_publish", record_publish)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index == 0:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Build an independent branch"})
            else:
                yield TextDoneEvent(text="Root is ready")
        elif provider.index == 1:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="grandchild", name="to_agent", arguments={"prompt": "Verify the branch"})
            elif len(provider.requests) == 2:
                child_generation_started.set()
                yield TextChunkEvent(chunk="Child parent response started")
                child_chunk_sent.set()
                await release_child.wait()
                yield TextDoneEvent(text="Child integrated grandchild result")
            else:
                yield TextDoneEvent(text="Child final")
        else:
            await child_generation_started.wait()
            await child_chunk_sent.wait()
            await release_grandchild.wait()
            yield TextDoneEvent(text="Grandchild evidence")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    run = asyncio.create_task(collect(harness))
    try:
        await asyncio.wait_for(child_generation_started.wait(), HANG_GUARD)
        child_info = next(info for info in harness.tasks.list() if not info.parent_session_id)
        immediate_parent = harness.tasks.child(child_info.session_id)
        expected_response_id = immediate_parent.agent._active_generation_id(immediate_parent.session_id)
        assert expected_response_id
        assert harness.agent._active_generation_id(harness.session_id) != expected_response_id

        release_grandchild.set()
        await asyncio.wait_for(grandchild_completed.wait(), HANG_GUARD)
        grandchild = next(event for event in completions if event.depth == 2)
        assert grandchild.parent_session_id == child_info.session_id
        assert grandchild.parent_response_id == expected_response_id
        assert grandchild.parent_response_session_id == child_info.session_id

        release_child.set()
        await asyncio.wait_for(run, HANG_GUARD)
        assert immediate_parent.agent._active_generation_id(immediate_parent.session_id) == ""
        assert providers[1].closed and providers[2].closed
    finally:
        release_grandchild.set()
        release_child.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
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
            yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Independent review"})
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


@pytest.mark.asyncio
async def test_completed_task_index_retains_last_provider_context_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.events import Usage
    from tests.support.providers import collect

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            yield TextDoneEvent(text="Child result", usage=Usage(321, 12, 333))
        elif notifications(messages):
            yield TextDoneEvent(text="Parent integrated result")
        elif len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Review this"})
        else:
            yield TextDoneEvent(text="Waiting for review")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        await asyncio.wait_for(collect(harness), HANG_GUARD)
        tasks = harness.tasks.list()
        assert len(tasks) == 1
        info = tasks[0]
        assert info.status == "completed"
        assert info.context["observed_prompt_tokens"] == 321
        assert info.context["total_tokens"] != 321
        assert providers[1].requests
    finally:
        await harness.close()
