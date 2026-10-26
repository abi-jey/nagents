"""Child inspection and human input use real isolated histories and run boundaries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.runtime.followups import RunInput
from nagents.harness.tools.coding import HarnessExecutor
from nagents.harness.types import TaskActivity
from nagents.harness.types import TaskMessage
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import assert_balanced
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.events import ToolResultEvent
    from nagents.harness.tools.execution import ExecutionBridge
    from nagents.harness.types import HarnessEvent
    from nagents.types import Message
    from nagents.types import ToolCall

pytestmark = pytest.mark.requires_posix


def test_busy_child_notes_wait_for_entire_tool_batch_and_keep_history_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        first_started, release_first, second_started, release_second = (asyncio.Event() for _ in range(4))
        notes_entered: list[str] = []
        events: list[HarnessEvent] = []
        original_execute = HarnessExecutor._execute_guarded

        async def execute(executor: HarnessExecutor, call: ToolCall, bridge: ExecutionBridge | None) -> ToolResultEvent:
            if executor.harness._is_subagent:
                if call.id == "one":
                    first_started.set()
                    await release_first.wait()
                elif call.id == "two":
                    second_started.set()
                    await release_second.wait()
            return await original_execute(executor, call, bridge)

        monkeypatch.setattr(HarnessExecutor, "_execute_guarded", execute)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="spawn", name="to_agent", arguments={"prompt": "Child original"})
                else:
                    yield TextDoneEvent(text="Main synthesis" if notifications(messages) else "Waiting")
            elif len(provider.requests) == 1:
                yield ReasoningChunkEvent(chunk="PRIVATE REASONING MUST NEVER BE INSPECTED")
                yield TextChunkEvent(
                    chunk="Public child progress",
                    extra={"task_id": "legacy-forged", "session_id": "s999", "authorization": "secret"},
                )
                yield ToolCallEvent(id="one", name="list_files", arguments={"path": "."})
                yield ToolCallEvent(id="two", name="list_files", arguments={"path": "."})
            else:
                yield TextDoneEvent(text="Acknowledged the direction")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def consume() -> None:
            events.extend(await collect(harness))

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(first_started.wait(), HANG_GUARD)
            info = harness.tasks.list()[0]

            def note(text: str) -> RunInput:
                receipt_id = f"note-{text.split()[0].lower()}"

                @asynccontextmanager
                async def admitted() -> AsyncIterator[None]:
                    current, child = harness.tasks.input_child(receipt_id)
                    assert current.session_id == info.session_id and child.session_id == info._storage_id
                    notes_entered.append(text)
                    yield

                return RunInput(text, identifier=receipt_id, message_id=receipt_id, context=admitted)

            assert harness.tasks.submit_message(info.session_id, note("First note"))
            assert notes_entered == []
            release_first.set()
            await asyncio.wait_for(second_started.wait(), HANG_GUARD)
            assert harness.tasks.submit_message(info.session_id, note("Second note"))
            assert notes_entered == []
            release_second.set()
            await asyncio.wait_for(task, HANG_GUARD)
            assert notes_entered == ["First note", "Second note"]
            history = await harness.task_history(info.session_id)
            assert_balanced(history)
            first_note = next(index for index, item in enumerate(history) if item.content == "First note")
            assert len([item for item in history[:first_note] if item.role == "tool"]) == 2
            assert [item.content for item in history if item.role == "user"] == [
                "Child original",
                "First note",
                "Second note",
            ]
            assert harness.tasks._used == 3
            assert harness.tasks.list()[0].followups == 2
            assert len(providers) == 2 and len(providers[1].requests) == 3
            assert all(
                item.content not in {"First note", "Second note"}
                for item in await harness.history()
                if isinstance(item.content, str)
            )
            activity = [item for item in events if isinstance(item, TaskActivity)]
            assert activity and all(
                item.session_id == info.session_id and item.root_session_id == harness.session_id for item in activity
            )
            assert [item.sequence for item in activity] == sorted({item.sequence for item in activity})
            assert "PRIVATE REASONING" not in str(activity) and "authorization" not in str(activity)
            assert any(item.payload.get("chunk") == "Public child progress" for item in activity)
            assert {item.message_id for item in events if isinstance(item, TaskMessage)} == {
                "note-first",
                "note-second",
            }
            assert "First note" not in str(providers[0].requests)
            assert "Second note" not in str(providers[0].requests)
            assert any(item.payload.get("text") == "First note" for item in activity)
            assert not harness.tasks.submit_message(info.session_id, note("Idle note"))
            assert harness.tasks._used == 3
        finally:
            release_first.set()
            release_second.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_profile_start_and_idle_continuation_preserve_bound_input_and_accepted_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text=f"Response {provider.index}")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        bound: list[str] = []

        def note(text: str) -> RunInput:
            @asynccontextmanager
            async def admitted() -> AsyncIterator[None]:
                info, child = harness.tasks.input_child(text)
                assert child.session_id == info._storage_id
                bound.append(info.session_id)
                yield

            return RunInput(text, identifier=text, message_id=text, context=admitted)

        try:
            events = [event async for event in harness.start_task("Inspect directly", message=note("Inspect directly"))]
            info = harness.tasks.list()[0]
            assert bound == [info.session_id]
            assert info.status == "completed" and info.phase == "completed"
            assert info.model == "fake-model" and info.started_at <= info.completed_at
            assert "Inspect directly" not in [item.content for item in await harness.history() if item.role == "user"]
            assert any(isinstance(item, TaskActivity) for item in events)
            await harness.set_model("new-main-model")
            _ = [
                event
                async for event in harness.continue_task(
                    info.session_id, "Continue directly", message=note("Continue directly")
                )
            ]
            assert bound == [info.session_id, info.session_id]
            assert providers[-1].model == "fake-model"
            stats = await harness.tasks.context_stats(info.session_id)
            assert stats["model"] == "fake-model" and stats["compaction"] == {"trigger": "off"}
            assert isinstance(stats["total_tokens"], int) and stats["total_tokens"] > 0
            assert [item.content for item in await harness.task_history(info.session_id) if item.role == "user"] == [
                "Inspect directly",
                "Continue directly",
            ]
            with pytest.raises(RuntimeError, match="producer"):
                harness.tasks.input_child("Continue directly")
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_child_waiting_for_descendant_picks_up_note_before_descendant_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        grandchild_started, release_grandchild, note_seen = (asyncio.Event() for _ in range(3))

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            original = next(item.content for item in messages if item.role == "user")
            if original == "Root":
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="parent", name="to_agent", arguments={"prompt": "Parent child"})
                else:
                    yield TextDoneEvent(text="Root result")
            elif original == "Parent child":
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="nested", name="to_agent", arguments={"prompt": "Grandchild"})
                else:
                    if messages[-1].content == "Note while waiting":
                        note_seen.set()
                    yield TextDoneEvent(text="Child result")
            else:
                grandchild_started.set()
                await release_grandchild.wait()
                yield TextDoneEvent(text="Grandchild result")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        running = asyncio.create_task(collect(harness, "Root"))
        try:
            await asyncio.wait_for(grandchild_started.wait(), HANG_GUARD)
            info = next(item for item in harness.tasks.list() if item.depth == 1)
            assert harness.tasks.submit_message(info.session_id, RunInput("Note while waiting", message_id="waiting"))
            await asyncio.wait_for(note_seen.wait(), HANG_GUARD)
            assert not release_grandchild.is_set()
            release_grandchild.set()
            await asyncio.wait_for(running, HANG_GUARD)
            history = await harness.task_history(info.session_id)
            assert_balanced(history)
            assert any("Grandchild result" in str(item.content) for item in notifications(history))
            root_notes = notifications(await harness.history())
            assert not any('"depth": 2' in str(item.content) for item in root_notes)
        finally:
            release_grandchild.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_native_delegate_accepts_model_and_compaction_overrides_without_mutating_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="spawn",
                    name="to_agent",
                    arguments={
                        "prompt": "Inspect",
                        "agent": "reviewer",
                        "model": "child-override",
                        "compact_trigger": "tokens",
                        "compact_tokens": 50000,
                    },
                )
            else:
                yield TextDoneEvent(text="Finished")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            assert info.status == "completed", info.error
            assert info.model == "child-override"
            assert info.compaction == {"trigger": "tokens", "token_budget": 50000, "token_threshold": 35000}
            assert harness.agent.provider.model == "fake-model"
            assert harness.agent.compaction_strategy is None
            assert harness.config.profile("reviewer").model == ""
            _ = [event async for event in harness.continue_task(info.session_id, "Inspect again")]
            assert harness.tasks.list()[0].model == "child-override"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_saved_child_settings_apply_only_on_next_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nagents.harness.runtime.task_activity import TaskOptions

    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="spawn", name="to_agent", arguments={"prompt": "Inspect"})
            elif provider.index == 1 and len(provider.requests) == 1:
                started.set()
                await release.wait()
                yield TextDoneEvent(text="Initial result")
            else:
                yield TextDoneEvent(text="Finished")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        running = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for(started.wait(), HANG_GUARD)
            info = harness.tasks.list()[0]
            saved = harness.tasks.configure_task(
                info.session_id, TaskOptions(model="next-child-model", compact_trigger="messages", compact_messages=25)
            )
            assert saved.model == "fake-model" and saved.next_settings["model"] == "next-child-model"
            assert providers[1].model == "fake-model"
            assert harness.tasks.submit_message(info.session_id, RunInput("Current note"))
            release.set()
            await asyncio.wait_for(running, HANG_GUARD)
            assert providers[1].model == "fake-model"
            _ = [event async for event in harness.continue_task(info.session_id, "Next activation")]
            updated = harness.tasks.list()[0]
            assert updated.model == "next-child-model" and updated.next_settings == {}
            assert updated.compaction == {"trigger": "messages", "message_threshold": 25}
            assert harness.agent.provider.model == "fake-model"
        finally:
            release.set()
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_full_activity_queue_coalesces_fragments_to_one_history_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nagents.harness.agents.subagents import TaskInfo
    from nagents.harness.types import Notice

    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            info = TaskInfo("s1", "Worker", root_session_id=harness.session_id, _storage_id="private-child")
            harness.tasks._infos[info.session_id] = info
            harness._queue = asyncio.Queue(maxsize=1)
            harness._queue.put_nowait(Notice("Occupied by required output"))
            # None of these observational events may await the consumer.
            for index in range(1000):
                await asyncio.wait_for(
                    harness.tasks._emit_activity(info, {"type": "text_chunk", "chunk": str(index)}), 1
                )
            assert harness._queue.qsize() == 1
            assert harness.tasks._activity_gaps == {info.session_id}
            assert info.activity_sequence == 1000
            assert isinstance(harness._queue.get_nowait(), Notice)
            harness.tasks.drain_activity()
            marker = harness._queue.get_nowait()
            assert isinstance(marker, TaskActivity)
            assert marker.session_id == info.session_id and marker.sequence == 1001
            assert marker.payload == {"type": "resync_required", "phase": "starting"}
            assert not harness.tasks._activity_gaps
            assert harness._queue.empty()
        finally:
            harness._queue = None
            await harness.close()

    asyncio.run(scenario())
