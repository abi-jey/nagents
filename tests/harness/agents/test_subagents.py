"""Process-local recursive tasks, using controlled providers and offline demo."""

from __future__ import annotations

import asyncio
import copy
import gc
import inspect
import json
import re
from types import ModuleType
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.extensions import AgentPlugin
from nagents.harness.agents import subagents
from nagents.harness.agents.subagent_settings import SubagentLimits
from nagents.harness.agents.subagents import MAX_RESULT
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.runtime import Harness
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage
from nagents.harness.types import TaskStarted
from nagents.provider.codex.openai import OpenAIProvider
from nagents.types import Message
from nagents.types import ToolCall
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import assert_balanced
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Iterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import HarnessEvent

    Script = Callable[["FakeProvider", list[Message]], AsyncIterator[Event]]


async def set_limits(harness: Harness, limits: SubagentLimits) -> None:
    current = await harness.tasks.root.settings_host.read("workspace")
    await harness.tasks.root.settings_host.save("workspace", current.revision, limits)
    await harness.tasks.refresh_limits()


# Controlled providers still run through real harness initialization and file guards.
pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("custom_prompt", ["", "replacement instructions", "append"])
def test_child_preserves_custom_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom_prompt: str
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        original = harness.agent.system_prompt or ""
        expected = original + "\nCustom appended instruction" if custom_prompt == "append" else custom_prompt
        harness.agent.system_prompt = expected
        child = harness.tasks._create_child("assistant")
        try:
            assert child._parent_instructions == expected
            if expected:
                assert expected in (child.agent.system_prompt or "")
            child.refresh_instructions()
            assert child._parent_instructions == expected
        finally:
            await child.close()
            await harness.close()

    asyncio.run(scenario())


def test_nested_children_inherit_authored_context_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        harness.config.agent = "author"
        harness.config.profiles["author"] = AgentProfile(instructions="Root authored instruction")
        harness.config.profiles["audit"] = AgentProfile(mode="reviewer", instructions="Child authored instruction")
        harness.instructions["AGENTS.md"] = "Root project instruction"
        harness.instructions["nested/AGENTS.md"] = "Nested project instruction"
        harness.refresh_instructions()
        child = harness.tasks._create_child("audit")
        grandchild = child.tasks._create_child("author")
        try:
            for current in (child, grandchild):
                prompt = current.agent.system_prompt or ""
                for instruction in (
                    "Root authored instruction",
                    "Root project instruction",
                    "Nested project instruction",
                    "Child authored instruction",
                    "Inspect before changing.",
                ):
                    assert prompt.count(instruction) == 1, f"{current.config.agent}: {instruction}"
                assert current.mode == "reviewer"
                assert "mode reviewer" in prompt
                assert current.instructions == harness.instructions
                current.refresh_instructions()
                assert current.agent.system_prompt == prompt
            assert grandchild._parent_instructions == ""
            assert len(grandchild.agent.system_prompt or "") < 2 * len(harness.agent.system_prompt or "")
        finally:
            await grandchild.close()
            await child.close()
            await harness.close()

    asyncio.run(scenario())


@pytest.fixture(autouse=True)
def collect_released_resources() -> Iterator[None]:
    yield
    gc.collect()


def test_tool_free_waiting_turn_resumes_synthesis_without_premature_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        child_started, yielded, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                child_started.set()
                await release.wait()
                yield TextDoneEvent(text="Review finding")
            elif notifications(messages):
                assert "Review finding" in str(notifications(messages)[0].content)
                yield TextDoneEvent(text="Verified synthesis")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="review", name="to_agent", arguments={"prompt": "Review independently"})
            else:
                yield TextDoneEvent(text="Waiting for the independent review")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        observed: list[HarnessEvent] = []

        async def consume() -> None:
            async for event in harness.run("Review and synthesize"):
                observed.append(event)
                if isinstance(event, TextDoneEvent) and event.text == "Waiting for the independent review":
                    yielded.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(child_started.wait(), HANG_GUARD)
            await asyncio.wait_for(yielded.wait(), HANG_GUARD)
            assert not task.done()
            assert len(providers[0].requests) == 2
            assert not any(isinstance(event, DoneEvent) for event in observed)
            release.set()
            await asyncio.wait_for(task, HANG_GUARD)
            assert len(providers[0].requests) == 3
            completions = [index for index, event in enumerate(observed) if isinstance(event, TaskCompleted)]
            done = [(index, event) for index, event in enumerate(observed) if isinstance(event, DoneEvent)]
            assert len(completions) == len(done) == 1
            assert completions[0] < done[0][0]
            assert done[0][1].final_text == "Verified synthesis"
            assert_balanced(await harness.history())
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [2, 3])
def test_children_are_concurrent_parent_keeps_working_and_late_results_wake_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    async def scenario() -> None:
        started = [asyncio.Event() for _ in range(count)]
        release = asyncio.Event()
        productive, preliminary = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                started[provider.index - 1].set()
                await release.wait()
                yield TextDoneEvent(text=f"child finding {provider.index}")
            elif notifications(messages):
                yield TextDoneEvent(text="Combined child findings")
            elif len(provider.requests) == 1:
                for index in range(count):
                    yield ToolCallEvent(
                        id=f"delegate-{index}", name="to_agent", arguments={"prompt": f"Review part {index}"}
                    )
                yield ToolCallEvent(id="own", name="own_work")
            else:
                yield TextDoneEvent(text="Parent preliminary response")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def own_work() -> str:
            """A trusted local parent-only operation."""
            productive.set()
            return "parent continued work"

        harness.agent.register_tool(own_work)
        harness.tools.builtins["own_work"] = own_work
        observed: list[HarnessEvent] = []

        async def consume() -> None:
            async for event in harness.run("Review independent parts"):
                observed.append(event)
                if isinstance(event, TextDoneEvent) and event.text == "Parent preliminary response":
                    preliminary.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), HANG_GUARD)
            await asyncio.wait_for(productive.wait(), HANG_GUARD)
            await asyncio.wait_for(preliminary.wait(), HANG_GUARD)
            assert not task.done()
            assert len([event for event in observed if isinstance(event, TaskStarted)]) == count
            acks = [event for event in observed if isinstance(event, ToolResultEvent) and event.name == "to_agent"]
            assert len(acks) == count
            assert not any(isinstance(event, TaskCompleted | DoneEvent) for event in observed)
            names: set[str] = set()
            for ack in acks:
                assert isinstance(ack.result, dict)
                assert set(ack.result) == {"session_id", "status"}
                assert ack.result["status"] == "running"
                assert re.fullmatch(r"s-[0-9a-f]{8}", ack.result["session_id"])
                names.add(ack.result["session_id"])
            assert len(names) == count
            assert all(not provider.closed for provider in providers)
            release.set()
            await asyncio.wait_for(task, HANG_GUARD)
            assert len([event for event in observed if isinstance(event, DoneEvent)]) == 1
            assert isinstance(observed[-1], DoneEvent) and observed[-1].final_text == "Combined child findings"
            completions = [event for event in observed if isinstance(event, TaskCompleted)]
            assert len(completions) == count and not any(event.error for event in completions)
            history = await harness.history()
            assert_balanced(history)
            notes = notifications(history)
            for completion in completions:
                assert sum(str(note.content).count(completion.session_id) for note in notes) == 1
            assert (
                len([message for message in history if message.role == "tool" and message.name == "to_agent"]) == count
            )
            assert all("untrusted" in str(note.content) and not note.tool_call_id for note in notes)
            for child in providers[1:]:
                assert child.closed and child.harness_config.agent == "assistant"
                assert child.harness_config.model == harness.agent.provider.model
                assert all(
                    "to_agent" in schema and "own_work" not in schema and "shell" in schema for schema in child.schemas
                )
            assert all(info.status == "completed" for info in harness.tasks.list())
            assert "fake-secret-never-in-task-data" not in repr(observed) + repr(history) + repr(harness.tasks.list())
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_ready_results_are_batched_and_custom_context_hooks_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        parent_waiting = asyncio.Event()
        continue_parent = asyncio.Event()
        hook_inputs: list[list[Message]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                yield TextDoneEvent(text=f"finding {provider.index}")
            elif notifications(messages):
                yield TextDoneEvent(text="Synthesis")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "First"})
                yield ToolCallEvent(id="b", name="to_agent", arguments={"prompt": "Second"})
            else:
                parent_waiting.set()
                await continue_parent.wait()
                yield TextDoneEvent(text="Ready")

        class Context(AgentPlugin):
            async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
                hook_inputs.append(copy.deepcopy(request.messages))
                request.messages.insert(0, Message(role="system", content="ephemeral context"))
                return request

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.agent.plugins.append(Context())
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for(parent_waiting.wait(), HANG_GUARD)
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), HANG_GUARD)
            continue_parent.set()
            events = await asyncio.wait_for(task, HANG_GUARD)
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 2
            notes = notifications(await harness.history())
            assert len(notes) == 1
            data = json.loads(str(notes[0].content).split("\n", 1)[1])
            assert len(data["sessions"]) == 2
            assert any(notifications(messages) for messages in hook_inputs)
            assert all(messages[0].content == "ephemeral context" for messages in providers[0].requests)
            assert not any(message.content == "ephemeral context" for message in await harness.history())
            assert len(hook_inputs) == len(providers[0].requests)  # Parent plugins were not copied into children.
        finally:
            continue_parent.set()
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "event", "timeout"])
def test_child_failure_is_delivered_once_without_exception_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                if failure == "exception":
                    raise RuntimeError("fake-secret-never-in-task-data")
                if failure == "timeout":
                    raise TimeoutError("fake-secret-never-in-task-data")
                yield ErrorEvent(message="fake-secret-never-in-task-data")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Review"})
            else:
                yield TextDoneEvent(text="Failure acknowledged")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            completed = [event for event in events if isinstance(event, TaskCompleted)]
            assert len(completed) == 1 and completed[0].error and not completed[0].result
            assert len(notifications(await harness.history())) == 1
            assert harness.tasks.list()[0].status == "failed"
            assert "fake-secret-never-in-task-data" not in repr(events) + repr(await harness.history())
            if failure == "timeout":
                assert completed[0].error == (
                    "Subagent operation timed out during setup or execution; no successful result is available."
                )
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["cancel", "aclose", "close"])
def test_parent_interruption_awaits_children_and_does_not_close_shared_auth_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    async def scenario() -> None:
        child_started, parent_waiting = asyncio.Event(), asyncio.Event()
        auth_closes: list[bool] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                child_started.set()
                await asyncio.Event().wait()
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Wait"})
            else:
                parent_waiting.set()
                await asyncio.Event().wait()

        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def close_auth() -> None:
            auth_closes.append(True)

        monkeypatch.setattr(harness.openai_auth, "close", close_auth)
        stream = harness.run("Delegate a waiting review")
        task: asyncio.Task[list[HarnessEvent]] | None = None
        try:
            if method == "aclose":
                async for event in stream:
                    if isinstance(event, ToolResultEvent) and event.name == "to_agent":
                        await asyncio.wait_for(child_started.wait(), HANG_GUARD)
                        await stream.aclose()
                        break
            else:

                async def consume() -> list[HarnessEvent]:
                    return [event async for event in stream]

                task = asyncio.create_task(consume())
                await asyncio.wait_for(child_started.wait(), HANG_GUARD)
                await asyncio.wait_for(parent_waiting.wait(), HANG_GUARD)
                if method == "cancel":
                    task.cancel()
                else:
                    await harness.close()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, HANG_GUARD)
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert all(provider.closed for provider in providers[1:])
            assert harness.tasks.list()[0].status == "cancelled"
            assert harness.tasks.list()[0].error
            assert not notifications(await harness.agent.session.get_history(harness.session_id))
            assert auth_closes == ([True] if method == "close" else [])
            assert not [
                task for task in asyncio.all_tasks() if task.get_name().startswith("ngn-subagent-") and not task.done()
            ]
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await stream.aclose()
            await harness.close()
        assert auth_closes == [True]

    asyncio.run(scenario())


def test_full_ui_queue_cancellation_does_not_deadlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        child_started = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                child_started.set()
                await asyncio.Event().wait()
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Wait"})
            else:
                for _ in range(200):
                    yield TextChunkEvent(chunk="flood")
                yield TextDoneEvent(text="flood")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        stream = harness.run("flood")
        try:
            async for event in stream:
                if isinstance(event, TextChunkEvent):
                    break
            await asyncio.wait_for(child_started.wait(), HANG_GUARD)

            async def full() -> None:
                while harness._queue is not None and not harness._queue.full():
                    await asyncio.sleep(0)

            await asyncio.wait_for(full(), HANG_GUARD)
            assert harness._queue is not None and harness._queue.full()
            await asyncio.wait_for(stream.aclose(), HANG_GUARD)
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert providers[1].closed
        finally:
            await stream.aclose()
            await harness.close()

    asyncio.run(scenario())


def test_stop_on_start_ack_repairs_tool_history_and_cancels_unstarted_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                await asyncio.Event().wait()
            else:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Review"})

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        stream = harness.run("Stop immediately")
        try:
            async for event in stream:
                if isinstance(event, TaskStarted):
                    await stream.aclose()
                    break
            assert all(worker.done() for worker in harness.tasks._workers.values())
            info = harness.tasks.list()[0]
            worker = harness.tasks._workers[info.session_id]
            assert info.status == "cancelled", (info.error, worker.cancelled(), worker.cancelling())
            assert_balanced(await harness.history())
        finally:
            await stream.aclose()
            await harness.close()

    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_child_provider_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        closing, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="finished")

        original_close = FakeProvider.close

        async def slow_close(provider: FakeProvider) -> None:
            if provider.index:
                closing.set()
                await release.wait()
            await original_close(provider)

        monkeypatch.setattr(FakeProvider, "close", slow_close)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        await set_limits(harness, SubagentLimits(max_concurrent_subagents=1))
        harness.tasks.begin(harness.session_id)
        try:
            await harness.tasks.to_agent("Review")
            await asyncio.wait_for(closing.wait(), HANG_GUARD)
            with pytest.raises(ValueError, match="concurrently"):
                await harness.tasks.to_agent("Cannot reuse a slot before cleanup")
            stop = asyncio.create_task(harness.tasks.end())
            await asyncio.sleep(0)
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
            assert not providers[1].closed
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, HANG_GUARD)
            assert providers[1].closed
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert harness.tasks.list()[0].status == "cancelled"
        finally:
            release.set()
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_cancel_before_worker_first_step_records_terminal_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            await asyncio.Event().wait()
            yield TextDoneEvent(text="unreachable")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            ack = await harness.tasks.to_agent("Review")
            harness.tasks._workers[ack["session_id"]].cancel()
            await harness.tasks.end()
            info = harness.tasks.list()[0]
            assert info.status == "cancelled" and info.error
            info.title = "mutated snapshot"
            assert harness.tasks.list()[0].title == ""
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_parent_failure_does_not_autocontinue_and_cancels_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        child_started = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                child_started.set()
                await asyncio.Event().wait()
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Review"})
            else:
                await child_started.wait()
                yield ErrorEvent(message="parent failed", recoverable=False)

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            assert any(isinstance(event, ErrorEvent) for event in events)
            assert len(providers[0].requests) == 2
            assert harness.tasks.list()[0].status == "cancelled"
            assert not notifications(await harness.history())
            assert providers[1].closed
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_task_limits_identity_and_shared_recursive_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            await release.wait()
            yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        try:
            await set_limits(harness, SubagentLimits(max_concurrent_subagents=3, max_subagent_executions=8))
            with pytest.raises(RuntimeError, match="active parent run"):
                await harness.tasks.to_agent("Outside a run")
            harness.tasks.begin(harness.session_id)
            acks = await asyncio.gather(*(harness.tasks.to_agent(f"Review {i}") for i in range(3)))
            assert len({ack["session_id"] for ack in acks}) == 3
            assert inspect.iscoroutinefunction(harness.tasks.to_agent)
            with pytest.raises(ValueError, match="concurrently"):
                await harness.tasks.to_agent("Fourth")
            with pytest.raises(ValueError, match="concurrently"):
                await harness.tasks.to_agent("Build", agent="build")
            child = harness.tasks._create_child("reviewer")
            try:
                child.tasks.begin(child.session_id)
                with pytest.raises(ValueError, match="concurrently"):
                    await child.tasks.to_agent("Nested")
                assert "to_agent" in child.agent.tool_registry.names()
                # The read-only child registry keeps compact_history and drops
                # every modifying tool.
                names = child.agent.tool_registry.names()
                assert "compact_history" in names
                assert not {"edit", "write", "shell"} & set(names)
                assert child.openai_auth is harness.openai_auth and child.agent.provider is not harness.agent.provider
            finally:
                await child.close()
            release.set()
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), HANG_GUARD)
            await set_limits(harness, SubagentLimits(max_concurrent_subagents=3, max_subagent_executions=3))
            with pytest.raises(ValueError, match="budget exhausted"):
                await harness.tasks.to_agent("New wave must not reset total budget")
            await harness.tasks.notification()
            with pytest.raises(ValueError, match="budget exhausted"):
                await harness.tasks.to_agent("Nor after notification delivery")
        finally:
            release.set()
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_auto_continuations_share_one_total_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                yield TextDoneEvent(text="child result")
            elif messages[-1].role == "tool":
                yield TextDoneEvent(text="Parent settled")
            else:
                yield ToolCallEvent(
                    id=f"spawn-{len(provider.requests)}", name="to_agent", arguments={"prompt": "Review again"}
                )

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            await set_limits(harness, SubagentLimits(max_subagent_executions=2))
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            assert len(providers) == 3
            assert len([event for event in events if isinstance(event, TaskStarted)]) == 2
            assert any(
                isinstance(event, ToolResultEvent) and event.error and "budget exhausted" in event.error
                for event in events
            )
            assert len([event for event in events if isinstance(event, DoneEvent)]) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_reviewer_delegation_and_post_hook_escalation_denial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index and len(provider.requests) == 1:
                yield ToolCallEvent(id="write", name="write", arguments={"path": "forbidden", "content": "bad"})
            elif not provider.index and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Review", "agent": "audit"})
            else:
                yield TextDoneEvent(text="done")

        class Escalate(AgentPlugin):
            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                if call.name == "to_agent":
                    call.arguments["agent"] = "assistant"
                return call

        harness, providers = setup_harness(tmp_path, monkeypatch, script, agent="reviewer")
        harness.config.profiles["audit"] = AgentProfile(
            mode="reviewer", instructions="Look for races", model="configured-child-model"
        )
        harness.agent.plugins.append(Escalate())
        try:
            events = await collect(harness)
            assert len(providers) == 2
            assert any(isinstance(event, TaskCompleted) for event in events)
            assert providers[1].harness_config.agent == "assistant"
            assert all("write" not in schema and "shell" not in schema for schema in providers[1].schemas)
            assert any(
                message.role == "tool" and "Read-only mode denies" in str(message.content)
                for message in await harness.task_history(harness.tasks.list()[0].session_id)
            )
            harness.agent.plugins.clear()
            harness.tasks.begin(harness.session_id)
            child = harness.tasks._create_child("audit")
            try:
                assert child.mode == "reviewer"
                assert child.agent.provider.model == "configured-child-model"
                assert "Look for races" in (child.agent.system_prompt or "")
                child.config.profiles["audit"].mode = "build"
                denied = await child.agent.tool_executor.execute(
                    ToolCall("write", "write", {"path": "forbidden", "content": "bad"})
                )
                assert denied.error and "Read-only mode denies" in denied.error
                assert not (tmp_path / "forbidden").exists()
            finally:
                await child.close()
        finally:
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_sessions_hidden_and_no_cross_session_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index or len(provider.requests) != 1:
                yield TextDoneEvent(text="bounded " + "x" * (MAX_RESULT + 100))
            else:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Review"})

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await collect(harness)
            result = next(event for event in events if isinstance(event, TaskCompleted))
            assert len(result.result) <= MAX_RESULT and "truncated" in result.result
            session = harness.session_id
            assert [entry.id for entry in await harness.list_sessions()] == [session]
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                cursor = await db.execute("SELECT id FROM v2_sessions")
                rows = list(await cursor.fetchall())
                assert len(rows) == 2
            assert len(harness.tasks.list()) == 1
            await harness.new_session()
            assert harness.tasks.list() == []
            assert await harness.history() == []
            with pytest.raises(RuntimeError, match="another session"):
                await harness.tasks.notification()
            await collect(harness, "new prompt")
            assert not notifications(await harness.history())
            await harness.resume(session)
            assert len(notifications(await harness.history())) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_plugin_commands_attributed_and_not_reimported_into_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        marker = tmp_path / "root-setups.txt"

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index or len(provider.requests) > 1:
                yield TextDoneEvent(text="done")
            else:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Review"})

        extension = tmp_path / "subagent_test_plugin.py"
        extension.write_text(
            "from pathlib import Path\n"
            "def setup(harness):\n"
            f"    with Path({str(marker)!r}).open('a') as stream: stream.write('root\\n')\n"
            "    harness.commands.register('review-note', 'Review notes', prompt='Review $ARGUMENTS')\n"
        )
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.config.plugins = (str(extension) + ":setup",)
        try:
            await collect(harness)
            # The parent reloads on each response; children never import it.
            assert len(marker.read_text().splitlines()) == len(providers[0].requests) + 4
            command = harness.commands.get("review-note")
            assert command is not None and command.source == "plugin:subagent_test_plugin.py"
            assert providers[1].harness_config.plugins == ()
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_codex_child_shares_auth_not_provider_or_close_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(
            HarnessConfig(
                workspace=tmp_path,
                providers=connection(auth="api-key"),
                data_dir=tmp_path / "state",
                profiles={"reviewer": AgentProfile(mode="reviewer")},
            )
        )
        harness.agent.provider = OpenAIProvider(harness.openai_auth.credentials)
        child = harness.tasks._create_child("reviewer")
        try:
            await child.initialize()  # Local only: model verification and credentials aren't requested.
            assert isinstance(child.agent.provider, OpenAIProvider)
            assert child.agent.provider is not harness.agent.provider
            assert child.agent.provider._credentials == harness.openai_auth.credentials
            assert child.openai_auth is harness.openai_auth and not child._owns_auth
            assert "to_agent" in child.agent.tool_registry.names()
        finally:
            await child.close()
            await harness.close()

    asyncio.run(scenario())


def test_genuine_offline_children_use_existing_demo_provider(tmp_path: Path) -> None:
    skill = tmp_path / ".agents/skills/check"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: check\ndescription: Check facts\n---\nRead without editing.\n")

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            ack = await harness.tasks.to_agent("Inspect the workspace read-only")
            assert ack["status"] == "running"
            assert harness.tasks.list()[0].status == "running"
            notification = await asyncio.wait_for(harness.tasks.notification(), HANG_GUARD)
            assert notification is not None and "OFFLINE DEMO" in notification
            assert harness.tasks.list()[0].status == "completed"
            assert [entry.id for entry in await harness.list_sessions()] == [harness.session_id]
        finally:
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_demo_subagents_run_asynchronously_through_delegate_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        try:
            events = [event async for event in harness.run("demo subagents")]
            started = [event for event in events if isinstance(event, TaskStarted)]
            completed = [event for event in events if isinstance(event, TaskCompleted)]
            assert len(started) == len(completed) == 3
            assert len({event.session_id for event in started}) == 3
            assert all(not event.error for event in completed)
            assert all(task.status == "completed" for task in harness.tasks.list())
            done = [event for event in events if isinstance(event, DoneEvent)]
            assert len(done) == 1 and "coordinator received" in done[0].final_text
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["build", "reviewer"])
@pytest.mark.parametrize("depth", [0, 1, 2, 8])
def test_recursive_tree_depth_permission_ceiling_and_fail_fast_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, depth: int
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id=f"nested-{provider.index}", name="to_agent", arguments={"prompt": "Nested work"})
            else:
                yield TextDoneEvent(text=f"Finished {provider.index}")

        harness, providers = setup_harness(tmp_path, monkeypatch, script, agent=mode)
        try:
            limits = SubagentLimits(max_concurrent_subagents=3, max_subagent_executions=8, max_subagent_depth=depth)
            await set_limits(harness, limits)
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            infos = harness.tasks.list()
            assert len(infos) == min(depth, limits.max_concurrent_subagents)
            assert [info.depth for info in infos] == list(range(1, len(infos) + 1))
            assert len({info.session_id for info in infos}) == len(infos)
            assert all(
                info.status == "completed" and info.mode == mode and info.profile == "assistant" for info in infos
            )
            assert len([event for event in events if isinstance(event, TaskStarted)]) == len(infos)
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == len(infos)
            assert harness.tasks._used == len(infos)
            for index, info in enumerate(infos):
                assert info.parent_session_id == (infos[index - 1].session_id if index else "")
                assert info.root_session_id == harness.session_id
                assert info._storage_id != info.session_id
                if index:
                    assert info._parent_storage_id == infos[index - 1]._storage_id
                assert providers[index + 1].closed
                assert all(("shell" in schema) == (mode == "build") for schema in providers[index + 1].schemas)
                assert_balanced(await harness.task_history(info.session_id))
            leaf_history = await harness.task_history(infos[-1].session_id) if infos else await harness.history()
            denial = "concurrently" if depth > limits.max_concurrent_subagents else "depth limit"
            assert any(message.role == "tool" and denial in str(message.content) for message in leaf_history)
            assert_balanced(await harness.history())
            if depth == 0:
                harness.refresh_instructions()
                assert "Report findings only" not in (harness.agent.system_prompt or "")
                assert harness.mode == mode
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_descendant_notification_waves_do_not_reset_root_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if (provider.index == 0 and len(provider.requests) == 1) or (
                provider.index == 1 and messages[-1].role != "tool"
            ):
                yield ToolCallEvent(
                    id=f"spawn-{provider.index}-{len(provider.requests)}",
                    name="to_agent",
                    arguments={"prompt": "Next job"},
                )
            else:
                yield TextDoneEvent(text="finished")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            # This checks budget continuity across persisted child waves, not latency.
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            execution_limit = SubagentLimits().max_subagent_executions
            assert len(providers) == execution_limit + 1
            assert harness.tasks._used == execution_limit
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == execution_limit
            child = harness.tasks.list()[0]
            assert any(
                "budget exhausted" in str(message.content) for message in await harness.task_history(child.session_id)
            )
            again = await asyncio.wait_for(collect(harness, "An unrelated root turn"), HANG_GUARD)
            assert not any(isinstance(event, TaskStarted | TaskCompleted) for event in again)
            assert harness.tasks._used == 0
            assert len(harness.tasks.list()) == execution_limit
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_general_children_use_root_serialized_approval_and_no_custom_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        requests: list[ApprovalRequest] = []
        active = peak = 0

        async def approve(request: ApprovalRequest) -> bool:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            requests.append(request)
            await asyncio.sleep(0.02)
            active -= 1
            return True

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                if provider.index == 1:
                    yield ToolCallEvent(id="write", name="write", arguments={"path": "child.txt", "content": "child"})
                else:
                    yield ToolCallEvent(id="shell", name="shell", arguments={"command": "printf 'child shell'"})
            else:
                yield TextDoneEvent(text="Child work completed")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        harness.approval_handler = approve
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            await harness.tasks.to_agent("Create a file")
            await harness.tasks.to_agent("Run a command")
            await harness.tools.write("parent.txt", "parent")
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), HANG_GUARD)
            assert peak == 1 and len(requests) == 3
            assert (tmp_path / "child.txt").read_text() == "child"
            assert (tmp_path / "parent.txt").read_text() == "parent"
            assert len([request for request in requests if request.session_id and request.depth == 1]) == 2
            assert all(request.title in request.description for request in requests if request.session_id)
            assert all(info.status == "completed" for info in harness.tasks.list())
            child = harness.tasks._create_child("assistant")
            try:
                called = False

                def custom() -> str:
                    """An unsupported custom child tool."""
                    nonlocal called
                    called = True
                    return "bad"

                child.agent.register_tool(custom)
                denied = await child.agent.tool_executor.execute(ToolCall("custom", "custom", {}))
                assert denied.error and "custom" in denied.error
                assert not called and len(requests) == 3
            finally:
                await child.close()
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_idle_followup_reuses_identity_history_model_and_notifies_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Original child request"})
            else:
                yield TextDoneEvent(text=f"Response from worker {provider.index}")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.config.providers = connection(
            base_url="https://gateway.example/v1", api_key_env="CHILD_TEST_KEY", api="responses"
        )
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            with pytest.raises(ValueError, match="short session_id"):
                _ = [event async for event in harness.continue_task("", "Must not run on the parent")]
            old_history = await harness.task_history(info.session_id)
            old_history[0].content = "mutated detached history"
            assert (await harness.task_history(info.session_id))[0].content == "Original child request"
            await harness.set_model("new-parent-model")
            events = [
                event async for event in harness.continue_task(info.session_id, "Human follow-up: inspect the result")
            ]
            updated = harness.tasks.list()[0]
            assert (updated.session_id, updated.title, updated._storage_id) == (
                info.session_id,
                info.title,
                info._storage_id,
            )
            assert updated.followups == 1 and updated.status == "completed"
            assert providers[1].closed and providers[2].closed
            assert providers[2].model == "fake-model" and providers[0].model == "new-parent-model"
            assert providers[2].harness_config.provider_profile().base_url == "https://gateway.example/v1"
            assert providers[2].harness_config.provider_profile().key_env == "CHILD_TEST_KEY"
            assert providers[2].harness_config.provider_profile().api == "responses"
            assert harness.tasks._used == 2
            assert len([event for event in events if isinstance(event, TaskMessage)]) == 1
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 1
            task_events = [event for event in events if isinstance(event, TaskMessage | TaskStarted | TaskCompleted)]
            assert [type(event) for event in task_events] == [TaskStarted, TaskMessage, TaskCompleted]
            assert "A human message was picked up" in str(providers[0].requests[-1][-1].content)
            assert "Human follow-up: inspect the result" not in str(providers[0].requests)
            assert "Response from worker 2" in str(providers[0].requests[-1][-1].content)
            assert isinstance(events[-1], DoneEvent) and events[-1].session_id == harness.session_id
            history = await harness.task_history(info.session_id)
            assert [message.content for message in history if message.role == "user"] == [
                "Original child request",
                "Human follow-up: inspect the result",
            ]
            assert_balanced(history)
            notes = notifications(await harness.history())
            assert any(
                "human_messages" in str(note.content) and "A human message was picked up" in str(note.content)
                for note in notes
            )
            assert any("Response from worker 2" in str(note.content) for note in notes)
            assert all("untrusted" in str(note.content) for note in notes)
            assert len(await harness.task_history(info.session_id, limit=1)) == 1
            with pytest.raises(ValueError, match="History limit"):
                await harness.task_history(info.session_id, limit=True)
            assert [session.id for session in await harness.list_sessions()] == [harness.session_id]
            await harness.new_session()
            with pytest.raises(ValueError, match="Unknown or unavailable child session"):
                await harness.task_history(info.session_id)
            with pytest.raises(ValueError, match="Unknown or unavailable child session"):
                _ = [event async for event in harness.continue_task(info.session_id, "Wrong session")]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_busy_followup_does_not_split_parent_tool_block_or_drop_busy_child_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        parent_waiting, release_parent = asyncio.Event(), asyncio.Event()
        child_waiting, release_child = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Original"})
                yield ToolCallEvent(id="parent-pause", name="pause")
            elif provider.index == 2:
                child_waiting.set()
                await release_child.wait()
                yield TextDoneEvent(text="Follow-up result")
            else:
                yield TextDoneEvent(text="Finished")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)

        async def pause() -> str:
            """Keep a native parent tool block open while the human messages a child."""
            parent_waiting.set()
            await release_parent.wait()
            return "released"

        harness.agent.register_tool(pause)
        harness.tools.builtins["pause"] = pause
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for(parent_waiting.wait(), HANG_GUARD)
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), HANG_GUARD)
            info = harness.tasks.list()[0]
            await set_limits(harness, SubagentLimits(max_subagent_executions=1))
            with pytest.raises(ValueError, match="budget exhausted"):
                _ = [event async for event in harness.continue_task(info.session_id, "Must share the root budget")]
            assert harness.tasks.list()[0].followups == 0
            await set_limits(harness, SubagentLimits())
            accepted = [
                event async for event in harness.continue_task(info.session_id, "Human message while parent busy")
            ]
            assert len(accepted) == 1
            await asyncio.wait_for(child_waiting.wait(), HANG_GUARD)
            with pytest.raises(RuntimeError, match="Child session is busy"):
                _ = [event async for event in harness.continue_task(info.session_id, "Do not silently queue this")]
            assert harness.tasks._used == 2 and harness.tasks.list()[0].followups == 1
            assert not notifications(await harness.history())
            release_child.set()
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), HANG_GUARD)
            release_parent.set()
            events = await asyncio.wait_for(task, HANG_GUARD)
            assert len([event for event in events if isinstance(event, TaskMessage)]) == 1
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 2
            assert_balanced(await harness.history())
            child_history = await harness.task_history(info.session_id)
            assert_balanced(child_history)
            assert not any("silently queue" in str(message.content) for message in child_history)
            assert any(
                "A human message was picked up" in str(note.content) for note in notifications(await harness.history())
            )
            assert any("Follow-up result" in str(note.content) for note in notifications(await harness.history()))
        finally:
            release_child.set()
            release_parent.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_manual_descendant_followup_reaches_immediate_parent_not_root_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        ancestor_waiting, release_ancestor = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index < 2 and len(provider.requests) == 1:
                yield ToolCallEvent(id=f"spawn-{provider.index}", name="to_agent", arguments={"prompt": "Nested"})
            else:
                if provider.index == 1 and len(provider.requests) == 2:
                    ancestor_waiting.set()
                    await release_ancestor.wait()
                yield TextDoneEvent(text=f"Result {provider.index}")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for(ancestor_waiting.wait(), HANG_GUARD)
            descendant = next(info for info in harness.tasks.list() if info.depth == 2)
            await asyncio.wait_for(harness.tasks._workers[descendant.session_id], HANG_GUARD)
            _ = [event async for event in harness.continue_task(descendant.session_id, "Human to grandchild")]
            await asyncio.wait_for(harness.tasks._workers[descendant.session_id], HANG_GUARD)
            assert harness.tasks._used == 3
            release_ancestor.set()
            events = await asyncio.wait_for(task, HANG_GUARD)
            ancestor = next(info for info in harness.tasks.list() if info.depth == 1)
            parent_history = await harness.task_history(ancestor.session_id)
            root_history = await harness.history()
            assert_balanced(parent_history)
            assert_balanced(root_history)
            pickups = [event for event in events if isinstance(event, TaskMessage)]
            assert len(pickups) == 1 and pickups[0].session_id == descendant.session_id
            assert pickups[0].parent_session_id == ancestor.session_id and pickups[0].message_id
            parent_notices = [str(note.content) for note in notifications(parent_history)]
            assert any("A human message was picked up by this agent." in notice for notice in parent_notices)
            assert all(pickups[0].message_id not in notice for notice in parent_notices)
            assert "Human to grandchild" not in str(parent_history)
            assert any(
                item.role == "user" and item.content == "Human to grandchild"
                for item in await harness.task_history(descendant.session_id)
            )
            assert any("Result 3" in str(note.content) for note in notifications(parent_history))
            assert not any(
                "Human to grandchild" in str(note.content) or "Result 3" in str(note.content)
                for note in notifications(root_history)
            )
            assert pickups[0].message_id not in str(root_history)
            assert any("Result 1" in str(note.content) for note in notifications(root_history))
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 3
        finally:
            release_ancestor.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_followup_and_retention_limits_and_cancelled_children_are_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        child_waiting = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Original"})
            elif provider.index == 2:
                child_waiting.set()
                await asyncio.Event().wait()
            else:
                yield TextDoneEvent(text="Finished")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            await set_limits(harness, SubagentLimits(max_subagent_depth=0))
            with pytest.raises(PermissionError, match="depth limit"):
                _ = [event async for event in harness.continue_task(info.session_id, "Disabled")]
            await set_limits(harness, SubagentLimits(max_subagent_depth=2))
            monkeypatch.setattr(subagents, "MAX_FOLLOWUPS", 0)
            with pytest.raises(ValueError, match="human follow-ups"):
                _ = [event async for event in harness.continue_task(info.session_id, "Too many")]
            monkeypatch.setattr(subagents, "MAX_FOLLOWUPS", 8)

            async def consume() -> list[HarnessEvent]:
                return [event async for event in harness.continue_task(info.session_id, "Explicit continuation")]

            task = asyncio.create_task(consume())
            await asyncio.wait_for(child_waiting.wait(), HANG_GUARD)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert providers[2].closed and harness.tasks.list()[0].status == "cancelled"
            with pytest.raises(RuntimeError, match="cancellation"):
                _ = [event async for event in harness.continue_task(info.session_id, "Never replay")]
            events = await collect(harness, "A new request")
            assert not any(isinstance(event, TaskStarted | TaskCompleted | TaskMessage) for event in events)
            assert len(providers) == 3
            harness.tasks.begin(harness.session_id)
            monkeypatch.setattr(subagents, "MAX_RETAINED_TASKS", 1)
            with pytest.raises(ValueError, match="retention limit"):
                await harness.tasks.to_agent("No unbounded retained conversations")
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_tree_shutdown_under_backpressure_and_repeated_close_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        grandchild_waiting, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_close = FakeProvider.close

        async def slow_close(provider: FakeProvider) -> None:
            if provider.index:
                closing.set()
                await release_close.wait()
            await original_close(provider)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Branch A"})
                yield ToolCallEvent(id="b", name="to_agent", arguments={"prompt": "Branch B"})
            elif provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(id="grandchild", name="to_agent", arguments={"prompt": "Grandchild"})
            elif provider.index == 0:
                await grandchild_waiting.wait()
                for _ in range(200):
                    yield TextChunkEvent(chunk="flood")
            else:
                if provider.index == 3:
                    grandchild_waiting.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(FakeProvider, "close", slow_close)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        stream = harness.run("Recursive tree")
        try:
            async for event in stream:
                if isinstance(event, TextChunkEvent):
                    break
            await asyncio.wait_for(grandchild_waiting.wait(), HANG_GUARD)
            assert len(harness.tasks.list()) == 3
            stop = asyncio.create_task(harness.close())
            await asyncio.wait_for(closing.wait(), HANG_GUARD)
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
            assert not stop.done()
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, HANG_GUARD)
            await harness.close()
            assert all(provider.closed for provider in providers)
            assert all(info.status == "cancelled" for info in harness.tasks.list())
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("ngn-") and not task.done()]
        finally:
            release_close.set()
            await stream.aclose()
            await harness.close()

    asyncio.run(scenario())


def test_indefinitely_waiting_child_stays_active_until_explicit_tree_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        grandchild_started = asyncio.Event()
        sibling_working = asyncio.Event()

        def actor(messages: list[Message]) -> str:
            return str(next(message.content for message in messages if message.role == "user"))

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            branch = actor(messages)
            if branch == "Root task" and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "Branch A"})
                yield ToolCallEvent(id="b", name="to_agent", arguments={"prompt": "Branch B"})
            elif branch == "Branch A" and len(provider.requests) == 1:
                yield ToolCallEvent(id="nested", name="to_agent", arguments={"prompt": "Waiting descendant"})
            elif branch in {"Branch A", "Waiting descendant"}:
                if branch == "Waiting descendant":
                    grandchild_started.set()
                await asyncio.Event().wait()
            else:
                if branch == "Branch B":
                    await grandchild_started.wait()
                    sibling_working.set()
                yield TextDoneEvent(text="Independent work finished")

        def forbidden_timeout(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("Subagent execution must not have an overall timeout")

        local_asyncio = ModuleType("subagent_test_asyncio")
        vars(local_asyncio).update(vars(asyncio))
        monkeypatch.setattr(local_asyncio, "timeout", forbidden_timeout)
        monkeypatch.setattr(subagents, "asyncio", local_asyncio)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            async with asyncio.timeout(HANG_GUARD):
                await harness.tasks.to_agent("Branch A")
                await harness.tasks.to_agent("Branch B")
                await grandchild_started.wait()
                await sibling_working.wait()
                infos = {info.prompt: info for info in harness.tasks.list()}
                await asyncio.wait_for(asyncio.shield(harness.tasks._workers[infos["Branch B"].session_id]), HANG_GUARD)
                while True:
                    infos = {info.prompt: info for info in harness.tasks.list()}
                    if infos.get("Branch B") is not None and infos["Branch B"].status == "completed":
                        break
                    await asyncio.sleep(0)
                assert infos["Branch A"].status == "running"
                assert infos["Waiting descendant"].status == "running"
                assert not harness.tasks._workers[infos["Branch A"].session_id].done()
                assert not harness.tasks._workers[infos["Waiting descendant"].session_id].done()
                # This is the explicit tree-stop boundary; the two indefinitely
                # waiting tasks remain active until the owner stops them.
                await harness.tasks.end()
            infos = {info.prompt: info for info in harness.tasks.list()}
            assert len(infos) == 3
            assert infos["Branch A"].status == "cancelled"
            assert infos["Branch B"].status == "completed"
            assert infos["Waiting descendant"].status == "cancelled"
            assert sorted(actor(provider.requests[0]) for provider in providers[1:]) == sorted(
                ["Branch A", "Waiting descendant", "Branch B"]
            )
            assert all(provider.closed for provider in providers[1:])
            assert_balanced(await harness.history())
        finally:
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_continuation_permission_ceiling_and_no_restored_job_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="to_agent", arguments={"prompt": "General work"})
            else:
                yield TextDoneEvent(text="Completed")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "AGENTS.md").write_text("Retained nested project context")
        harness.instructions["nested/AGENTS.md"] = "Retained nested project context"
        harness.refresh_instructions()
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            retained = harness.tasks._children[info.session_id]
            original_prompt = retained.agent.system_prompt or ""
            assert original_prompt.count("Retained nested project context") == 1
            retained.agent.system_prompt = original_prompt + "\nCustom continuation instruction"
            await harness.set_agent("reviewer")
            _ = [event async for event in harness.continue_task(info.session_id, "Continue read-only")]
            assert harness.tasks.list()[0].mode == "reviewer"
            assert "shell" not in providers[2].schemas[0]
            continued_prompt = harness.tasks._children[info.session_id].agent.system_prompt or ""
            assert original_prompt + "\nCustom continuation instruction" in continued_prompt
            await harness.set_agent("assistant")
            _ = [event async for event in harness.continue_task(info.session_id, "Do not escalate this child")]
            assert harness.tasks.list()[0].mode == "reviewer"
            assert "shell" not in providers[3].schemas[0]
            restored = Harness(harness.config)
            try:
                await restored.resume(harness.session_id)
                assert restored.tasks.list() == []
                with pytest.raises(ValueError, match="not restored"):
                    _ = [
                        event async for event in restored.continue_task(info.session_id, "No restored background jobs")
                    ]
            finally:
                await restored.close()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("swallow_cancel", [False, True])
def test_cancellation_fails_closed_for_shared_approval_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swallow_cancel: bool
) -> None:
    async def scenario() -> None:
        approving = asyncio.Event()
        approvals: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request)
            approving.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow_cancel:
                    raise
            return True

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if not provider.index and len(provider.requests) == 1:
                for index in range(3):
                    yield ToolCallEvent(id=f"child-{index}", name="to_agent", arguments={"prompt": "Propose a file"})
            elif provider.index:
                yield ToolCallEvent(
                    id="write", name="write", arguments={"path": f"child-{provider.index}.txt", "content": "bad"}
                )
            else:
                yield TextDoneEvent(text="Waiting for children")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.approval_handler = approve
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for(approving.wait(), HANG_GUARD)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, HANG_GUARD)
            assert len(approvals) == 1
            assert not list(tmp_path.glob("child-*.txt"))
            assert all(info.status == "cancelled" for info in harness.tasks.list())
            assert all(provider.closed for provider in providers[1:])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("depth", [2, 3])
def test_continuation_intersects_retained_ancestor_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index < depth and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id=f"child-{provider.index}", name="to_agent", arguments={"prompt": "Build descendant"}
                )
            elif provider.index == depth + 2 and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="write", name="write", arguments={"path": "escalated.txt", "content": "forbidden"}
                )
                yield ToolCallEvent(id="shell", name="shell", arguments={"command": "printf 'forbidden'"})
            else:
                yield TextDoneEvent(text="Completed")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        approvals: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request)
            return True

        harness.approval_handler = approve
        try:
            await set_limits(harness, SubagentLimits(max_subagent_depth=depth))
            await asyncio.wait_for(collect(harness), HANG_GUARD)
            infos = harness.tasks.list()
            assert len(infos) == depth and all(info.mode == "build" for info in infos)
            ancestor, descendant = infos[0], infos[-1]
            await harness.set_agent("reviewer")
            _ = [event async for event in harness.continue_task(ancestor.session_id, "Continue ancestor read-only")]
            assert harness.tasks.list()[0].mode == "reviewer"
            await harness.set_agent("assistant")
            _ = [
                event async for event in harness.continue_task(descendant.session_id, "Respect all retained ancestors")
            ]
            assert harness.mode == "build" and harness.tasks.list()[-1].mode == "reviewer"
            assert all("write" not in schema and "shell" not in schema for schema in providers[-1].schemas)
            history = await harness.task_history(descendant.session_id)
            denied = [message for message in history if message.role == "tool" and message.name in {"write", "shell"}]
            assert len(denied) == 2 and all("Read-only mode denies" in str(message.content) for message in denied)
            assert not approvals and not (tmp_path / "escalated.txt").exists()
            assert_balanced(history)
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["execution", "cleanup"])
def test_cancelled_child_with_cleanup_error_cannot_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def scenario() -> None:
        started, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
        if phase == "execution":
            release_close.set()
        original_close = FakeProvider.close

        async def failing_close(provider: FakeProvider) -> None:
            await original_close(provider)
            if provider.index:
                closing.set()
                await release_close.wait()
                raise RuntimeError("fake-secret-never-in-task-data")

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                started.set()
                if phase == "execution":
                    await asyncio.Event().wait()
                yield TextDoneEvent(text="Finished before cleanup")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Wait for cancellation"})
            else:
                yield TextDoneEvent(text="Waiting")

        monkeypatch.setattr(FakeProvider, "close", failing_close)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for((started if phase == "execution" else closing).wait(), HANG_GUARD)
            worker = harness.tasks._workers[harness.tasks.list()[0].session_id]
            task.cancel()
            async with asyncio.timeout(HANG_GUARD):
                while not worker.cancelling():
                    await asyncio.sleep(0)
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, HANG_GUARD)
            info = harness.tasks.list()[0]
            worker = harness.tasks._workers[info.session_id]
            assert worker.cancelled() and providers[1].closed
            assert info.status == "cancelled"
            assert "cancelled" in info.error and "cleanup failed" in info.error
            assert "fake-secret-never-in-task-data" not in repr(info)
            histories = await harness.history(), await harness.task_history(info.session_id)
            # Admission must also fail closed if terminal metadata is stale.
            harness.tasks._infos[info.session_id].status = "failed"
            with pytest.raises(RuntimeError, match="cancellation"):
                _ = [event async for event in harness.continue_task(info.session_id, "Never resume cancelled work")]
            assert harness.tasks._workers[info.session_id] is worker
            assert harness.tasks.list()[0].followups == 0
            assert len(providers) == 2
            assert (await harness.history(), await harness.task_history(info.session_id)) == histories
        finally:
            release_close.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_idle_followup_cannot_reset_exhausted_budget_but_new_root_run_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        spawned = 0

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            nonlocal spawned
            if (
                not provider.index
                and messages[-1].role != "tool"
                and spawned < SubagentLimits().max_subagent_executions
            ):
                spawned += 1
                yield ToolCallEvent(
                    id=f"spawn-{len(provider.requests)}", name="to_agent", arguments={"prompt": "Use the shared budget"}
                )
            elif provider.index > SubagentLimits().max_subagent_executions and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="write", name="write", arguments={"path": "ninth-job.txt", "content": "forbidden"}
                )
            else:
                yield TextDoneEvent(text="Completed")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        approvals: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request)
            return False

        harness.approval_handler = approve
        try:
            await asyncio.wait_for(collect(harness), HANG_GUARD)
            assert harness.tasks._used == SubagentLimits().max_subagent_executions == 8
            info = harness.tasks.list()[0]
            workers = dict(harness.tasks._workers)
            histories = await harness.history(), await harness.task_history(info.session_id)
            rejected_events: list[HarnessEvent] = []
            for _ in range(2):
                with pytest.raises(ValueError, match="budget exhausted"):
                    async for event in harness.continue_task(info.session_id, "Do not launch execution nine"):
                        rejected_events.append(event)
                assert harness.tasks._used == 8
                assert harness.tasks._workers == workers
                assert harness.tasks.list()[0] == info
                assert len(providers) == 9
                assert not approvals and not (tmp_path / "ninth-job.txt").exists()
                assert (await harness.history(), await harness.task_history(info.session_id)) == histories
            assert not any(
                isinstance(event, TaskMessage | TaskStarted | TaskCompleted | DoneEvent) for event in rejected_events
            )
            events = await collect(harness, "New external root run")
            assert harness.tasks._used == 0
            assert not any(isinstance(event, TaskStarted | TaskCompleted) for event in events)
            assert harness.tasks.list()[0] == info
            events = [
                event async for event in harness.continue_task(info.session_id, "Explicit follow-up in the new budget")
            ]
            assert harness.tasks.list()[0].followups == 1 and harness.tasks._used == 1
            assert any(isinstance(event, TaskCompleted) for event in events)
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_incompletely_initialized_child_cannot_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        original_initialize = Harness.initialize

        async def failing_initialize(child: Harness) -> None:
            if child._is_subagent:
                raise RuntimeError("fake-secret-never-in-task-data")
            await original_initialize(child)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if not provider.index and len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Cannot initialize"})
            else:
                yield TextDoneEvent(text="Completed")

        monkeypatch.setattr(Harness, "initialize", failing_initialize)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await collect(harness)
            info = harness.tasks.list()[0]
            assert info.status == "failed" and providers[1].closed and not providers[1].requests
            assert "fake-secret-never-in-task-data" not in repr(events) + repr(info)
            assert await harness.task_history(info.session_id) == []
            with pytest.raises(RuntimeError, match="incomplete setup"):
                _ = [event async for event in harness.continue_task(info.session_id, "No retained conversation")]
            assert len(providers) == 2 and harness.tasks.list()[0].followups == 0
            assert await harness.task_history(info.session_id) == []
        finally:
            await harness.close()

    asyncio.run(scenario())
