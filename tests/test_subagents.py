"""Process-local recursive tasks, using controlled providers and offline demo."""

from __future__ import annotations

import asyncio
import copy
import gc
import importlib
import inspect
import json
import re
import secrets
import uuid
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
from nagents.harness import runtime
from nagents.harness import subagents
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.harness.subagents import MAX_RESULT
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage
from nagents.harness.types import TaskStarted
from nagents.provider.codex import CodexProvider
from nagents.types import Message
from nagents.types import ToolCall

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
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition

    Script = Callable[["FakeProvider", list[Message]], AsyncIterator[Event]]


# Controlled providers still run through real harness initialization and file guards.
pytestmark = pytest.mark.requires_posix


@pytest.fixture(autouse=True)
def collect_released_resources() -> Iterator[None]:
    yield
    gc.collect()


class FakeProvider(HarnessProvider):
    def __init__(self, config: HarnessConfig, index: int, script: Script) -> None:
        super().__init__(config)
        self.index = index
        self.script = script
        self.requests: list[list[Message]] = []
        self.schemas: list[list[str]] = []
        self.closed = False
        self.api_key = "fake-secret-never-in-task-data"

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        assert_balanced(messages)
        self.requests.append(copy.deepcopy(messages))
        self.schemas.append([tool.name for tool in tools or []])
        async for event in self.script(self, messages):
            yield event

    async def close(self) -> None:
        self.closed = True
        await super().close()


def assert_balanced(messages: list[Message]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message.role == "tool":
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
        else:
            assert not pending, "A notification must never split a pending tool block"
            pending.update(call.id for call in message.tool_calls)
    assert not pending


def setup_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Script, *, agent: str = "build"
) -> tuple[Harness, list[FakeProvider]]:
    providers: list[FakeProvider] = []

    def provider_factory(config: HarnessConfig) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(runtime, "HarnessProvider", provider_factory)
    config = HarnessConfig(
        workspace=tmp_path, data_dir=tmp_path / "state", auth="api-key", model="fake-model", agent=agent
    )
    harness = Harness(config)
    harness.agent.compactor = None
    return harness, providers


async def collect(harness: Harness, prompt: str = "review this") -> list[HarnessEvent]:
    return [event async for event in harness.run(prompt)]


def notifications(messages: list[Message]) -> list[Message]:
    return [
        message
        for message in messages
        if message.role == "user" and str(message.content).startswith("BACKGROUND TASK NOTIFICATION:")
    ]


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
                        id=f"delegate-{index}", name="delegate", arguments={"prompt": f"Review part {index}"}
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
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 3)
            await asyncio.wait_for(productive.wait(), 3)
            await asyncio.wait_for(preliminary.wait(), 3)
            assert not task.done()
            assert len([event for event in observed if isinstance(event, TaskStarted)]) == count
            acks = [event for event in observed if isinstance(event, ToolResultEvent) and event.name == "delegate"]
            assert len(acks) == count
            assert not any(isinstance(event, TaskCompleted | DoneEvent) for event in observed)
            names: set[str] = set()
            for ack in acks:
                assert isinstance(ack.result, dict)
                assert ack.result["status"] == "running"
                uuid.UUID(ack.result["task_id"])
                assert re.fullmatch(r"[a-z]+ [a-z]+", ack.result["name"])
                names.add(ack.result["name"])
            assert len(names) == count
            assert all(not provider.closed for provider in providers)
            release.set()
            await asyncio.wait_for(task, 5)
            assert len([event for event in observed if isinstance(event, DoneEvent)]) == 1
            assert isinstance(observed[-1], DoneEvent) and observed[-1].final_text == "Combined child findings"
            completions = [event for event in observed if isinstance(event, TaskCompleted)]
            assert len(completions) == count and not any(event.error for event in completions)
            history = await harness.history()
            assert_balanced(history)
            notes = notifications(history)
            for completion in completions:
                assert sum(str(note.content).count(completion.task_id) for note in notes) == 1
            assert (
                len([message for message in history if message.role == "tool" and message.name == "delegate"]) == count
            )
            assert all("untrusted" in str(note.content) and not note.tool_call_id for note in notes)
            for child in providers[1:]:
                assert child.closed and child.harness_config.agent == "agent"
                assert child.harness_config.model == harness.agent.provider.model
                assert all(
                    "delegate" in schema and "own_work" not in schema and "shell" in schema for schema in child.schemas
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "First"})
                yield ToolCallEvent(id="b", name="delegate", arguments={"prompt": "Second"})
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
            await asyncio.wait_for(parent_waiting.wait(), 3)
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), 3)
            continue_parent.set()
            events = await asyncio.wait_for(task, 3)
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 2
            notes = notifications(await harness.history())
            assert len(notes) == 1
            data = json.loads(str(notes[0].content).split("\n", 1)[1])
            assert len(data["tasks"]) == 2
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
                    await asyncio.Event().wait()
                yield ErrorEvent(message="fake-secret-never-in-task-data")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})
            else:
                yield TextDoneEvent(text="Failure acknowledged")

        if failure == "timeout":
            monkeypatch.setattr(subagents, "CHILD_TIMEOUT", 0.05)
        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), 5)
            completed = [event for event in events if isinstance(event, TaskCompleted)]
            assert len(completed) == 1 and completed[0].error and not completed[0].result
            assert len(notifications(await harness.history())) == 1
            assert harness.tasks.list()[0].status == "failed"
            assert "fake-secret-never-in-task-data" not in repr(events) + repr(await harness.history())
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Wait"})
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
                    if isinstance(event, ToolResultEvent) and event.name == "delegate":
                        await asyncio.wait_for(child_started.wait(), 3)
                        await stream.aclose()
                        break
            else:

                async def consume() -> list[HarnessEvent]:
                    return [event async for event in stream]

                task = asyncio.create_task(consume())
                await asyncio.wait_for(child_started.wait(), 3)
                await asyncio.wait_for(parent_waiting.wait(), 3)
                if method == "cancel":
                    task.cancel()
                else:
                    await harness.close()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Wait"})
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
            await asyncio.wait_for(child_started.wait(), 3)

            async def full() -> None:
                while harness._queue is not None and not harness._queue.full():
                    await asyncio.sleep(0)

            await asyncio.wait_for(full(), 3)
            assert harness._queue is not None and harness._queue.full()
            await asyncio.wait_for(stream.aclose(), 3)
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        stream = harness.run("Stop immediately")
        try:
            async for event in stream:
                if isinstance(event, TaskStarted):
                    await stream.aclose()
                    break
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert harness.tasks.list()[0].status == "cancelled"
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
        monkeypatch.setattr(subagents, "MAX_CONCURRENT", 1)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            await harness.tasks.delegate("Review")
            await asyncio.wait_for(closing.wait(), 3)
            with pytest.raises(ValueError, match="concurrently"):
                await harness.tasks.delegate("Cannot reuse a slot before cleanup")
            stop = asyncio.create_task(harness.tasks.end())
            await asyncio.sleep(0)
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
            assert not providers[1].closed
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, 3)
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
            ack = await harness.tasks.delegate("Review")
            harness.tasks._workers[ack["task_id"]].cancel()
            await harness.tasks.end()
            info = harness.tasks.list()[0]
            assert info.status == "cancelled" and info.error
            info.name = "mutated snapshot"
            assert harness.tasks.list()[0].name == ack["name"]
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})
            else:
                await child_started.wait()
                yield ErrorEvent(message="parent failed", recoverable=False)

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), 3)
            assert any(isinstance(event, ErrorEvent) for event in events)
            assert len(providers[0].requests) == 2
            assert harness.tasks.list()[0].status == "cancelled"
            assert not notifications(await harness.history())
            assert providers[1].closed
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_task_limits_names_and_shared_recursive_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            await release.wait()
            yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        monkeypatch.setattr(secrets, "choice", lambda values: values[0])
        await harness.initialize()
        try:
            with pytest.raises(RuntimeError, match="active parent run"):
                await harness.tasks.delegate("Outside a run")
            harness.tasks.begin(harness.session_id)
            acks = await asyncio.gather(*(harness.tasks.delegate(f"Review {i}") for i in range(3)))
            assert len({ack["name"] for ack in acks}) == 3
            assert inspect.iscoroutinefunction(harness.tasks.delegate)
            with pytest.raises(ValueError, match="concurrently"):
                await harness.tasks.delegate("Fourth")
            with pytest.raises(ValueError, match="concurrently"):
                await harness.tasks.delegate("Build", agent="build")
            child = harness.tasks._create_child("reviewer")
            try:
                child.tasks.begin(child.session_id)
                with pytest.raises(ValueError, match="concurrently"):
                    await child.tasks.delegate("Nested")
                assert "delegate" in child.agent.tool_registry.names()
                assert child.openai_auth is harness.openai_auth and child.agent.provider is not harness.agent.provider
            finally:
                await child.close()
            release.set()
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), 3)
            monkeypatch.setattr(subagents, "MAX_TASKS", 3)
            with pytest.raises(ValueError, match="budget exhausted"):
                await harness.tasks.delegate("New wave must not reset total budget")
            await harness.tasks.notification()
            with pytest.raises(ValueError, match="budget exhausted"):
                await harness.tasks.delegate("Nor after notification delivery")
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
                    id=f"spawn-{len(provider.requests)}", name="delegate", arguments={"prompt": "Review again"}
                )

        monkeypatch.setattr(subagents, "MAX_TASKS", 2)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), 5)
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review", "agent": "audit"})
            else:
                yield TextDoneEvent(text="done")

        class Escalate(AgentPlugin):
            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                if call.name == "delegate":
                    call.arguments["agent"] = "build"
                return call

        harness, providers = setup_harness(tmp_path, monkeypatch, script, agent="reviewer")
        harness.config.profiles["audit"] = AgentProfile(
            mode="reviewer", instructions="Look for races", model="ignored-child-model"
        )
        harness.agent.plugins.append(Escalate())
        try:
            events = await collect(harness)
            assert len(providers) == 2
            assert any(isinstance(event, TaskCompleted) for event in events)
            assert providers[1].harness_config.agent == "build"
            assert all("write" not in schema and "shell" not in schema for schema in providers[1].schemas)
            assert any(
                message.role == "tool" and "reviewer" in str(message.content)
                for message in await harness.task_history(harness.tasks.list()[0].id)
            )
            harness.agent.plugins.clear()
            harness.tasks.begin(harness.session_id)
            child = harness.tasks._create_child("audit")
            try:
                assert child.mode == "reviewer"
                assert child.agent.provider.model == harness.agent.provider.model
                assert "Look for races" in (child.agent.system_prompt or "")
                child.config.profiles["audit"].mode = "build"
                denied = await child.agent.tool_executor.execute(
                    ToolCall("write", "write", {"path": "forbidden", "content": "bad"})
                )
                assert denied.error and "reviewer" in denied.error
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})

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
        setups: list[Harness] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index or len(provider.requests) > 1:
                yield TextDoneEvent(text="done")
            else:
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})

        module = ModuleType("subagent_test_plugin")

        async def setup(harness: Harness) -> None:
            setups.append(harness)
            await asyncio.sleep(0)
            harness.commands.register("review-note", "Review notes", prompt="Review $ARGUMENTS")

        module.setup = setup  # type: ignore[attr-defined]
        monkeypatch.setattr(importlib, "import_module", lambda name: module)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.config.plugins = ("subagent_test_plugin:setup",)
        try:
            await collect(harness)
            assert setups == [harness]
            command = harness.commands.get("review-note")
            assert command is not None and command.source == "plugin:subagent_test_plugin"
            assert providers[1].harness_config.plugins == ()
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_codex_child_shares_auth_not_provider_or_close_ownership(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, auth="api-key", data_dir=tmp_path / "state"))
        harness.agent.provider = CodexProvider(harness.openai_auth.credentials)
        child = harness.tasks._create_child("reviewer")
        try:
            await child.initialize()  # Local only: model verification and credentials aren't requested.
            assert isinstance(child.agent.provider, CodexProvider)
            assert child.agent.provider is not harness.agent.provider
            assert child.agent.provider._credentials == harness.openai_auth.credentials
            assert child.openai_auth is harness.openai_auth and not child._owns_auth
            assert "delegate" in child.agent.tool_registry.names()
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
            ack = await harness.tasks.delegate("Inspect the workspace read-only")
            assert ack["status"] == "running"
            assert harness.tasks.list()[0].status == "running"
            notification = await asyncio.wait_for(harness.tasks.notification(), 5)
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
            assert len({event.name for event in started}) == 3
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
                yield ToolCallEvent(id=f"nested-{provider.index}", name="delegate", arguments={"prompt": "Nested work"})
            else:
                yield TextDoneEvent(text=f"Finished {provider.index}")

        harness, providers = setup_harness(tmp_path, monkeypatch, script, agent=mode)
        harness.config.max_subagent_depth = depth
        try:
            events = await asyncio.wait_for(collect(harness), 5)
            infos = harness.tasks.list()
            assert len(infos) == min(depth, subagents.MAX_CONCURRENT)
            assert [info.depth for info in infos] == list(range(1, len(infos) + 1))
            assert len({info.name for info in infos}) == len(infos)
            assert all(info.status == "completed" and info.mode == mode and info.profile == "agent" for info in infos)
            assert len([event for event in events if isinstance(event, TaskStarted)]) == len(infos)
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == len(infos)
            assert harness.tasks._used == len(infos)
            for index, info in enumerate(infos):
                assert info.parent_task_id == (infos[index - 1].id if index else "")
                assert info.parent_session_id == (infos[index - 1].child_session_id if index else harness.session_id)
                assert info.child_session_id != info.session_id == harness.session_id
                assert providers[index + 1].closed
                assert all(("shell" in schema) == (mode == "build") for schema in providers[index + 1].schemas)
                assert_balanced(await harness.task_history(info.id))
            leaf_history = await harness.task_history(infos[-1].id) if infos else await harness.history()
            denial = "concurrently" if depth > subagents.MAX_CONCURRENT else "depth limit"
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
                    name="delegate",
                    arguments={"prompt": "Next job"},
                )
            else:
                yield TextDoneEvent(text="finished")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), 10)
            assert len(providers) == subagents.MAX_TASKS + 1
            assert harness.tasks._used == subagents.MAX_TASKS
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == subagents.MAX_TASKS
            child = harness.tasks.list()[0]
            assert any("budget exhausted" in str(message.content) for message in await harness.task_history(child.id))
            again = await collect(harness, "An unrelated root turn")
            assert not any(isinstance(event, TaskStarted | TaskCompleted) for event in again)
            assert harness.tasks._used == 0
            assert len(harness.tasks.list()) == subagents.MAX_TASKS
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
            await harness.tasks.delegate("Create a file")
            await harness.tasks.delegate("Run a command")
            await harness.tools.write("parent.txt", "parent")
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), 5)
            assert peak == 1 and len(requests) == 3
            assert (tmp_path / "child.txt").read_text() == "child"
            assert (tmp_path / "parent.txt").read_text() == "parent"
            assert len([request for request in requests if request.task_id and request.depth == 1]) == 2
            assert all(request.task_name in request.description for request in requests if request.task_id)
            assert all(info.status == "completed" for info in harness.tasks.list())
            child = harness.tasks._create_child("agent")
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
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Original child request"})
            else:
                yield TextDoneEvent(text=f"Response from worker {provider.index}")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.config.base_url = "https://gateway.example/v1"
        harness.config.api_key_env = "CHILD_TEST_KEY"
        harness.config.api = "responses"
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            with pytest.raises(ValueError, match="Unknown task"):
                _ = [event async for event in harness.continue_task("", "Must not run on the parent")]
            old_history = await harness.task_history(info.id)
            old_history[0].content = "mutated detached history"
            assert (await harness.task_history(info.id))[0].content == "Original child request"
            await harness.set_model("new-parent-model")
            events = [event async for event in harness.continue_task(info.id, "Human follow-up: inspect the result")]
            updated = harness.tasks.list()[0]
            assert (updated.id, updated.name, updated.child_session_id) == (info.id, info.name, info.child_session_id)
            assert updated.followups == 1 and updated.status == "completed"
            assert providers[1].closed and providers[2].closed
            assert providers[2].model == "fake-model" and providers[0].model == "new-parent-model"
            assert providers[2].harness_config.base_url == "https://gateway.example/v1"
            assert providers[2].harness_config.api_key_env == "CHILD_TEST_KEY"
            assert providers[2].harness_config.api == "responses"
            assert harness.tasks._used == 2
            assert len([event for event in events if isinstance(event, TaskMessage)]) == 1
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 1
            task_events = [event for event in events if isinstance(event, TaskMessage | TaskStarted | TaskCompleted)]
            assert [type(event) for event in task_events] == [TaskMessage, TaskStarted, TaskCompleted]
            assert "Human follow-up" in str(providers[0].requests[-1][-1].content)
            assert "Response from worker 2" in str(providers[0].requests[-1][-1].content)
            assert isinstance(events[-1], DoneEvent) and events[-1].session_id == harness.session_id
            history = await harness.task_history(info.id)
            assert [message.content for message in history if message.role == "user"] == [
                "Original child request",
                "Human follow-up: inspect the result",
            ]
            assert_balanced(history)
            notes = notifications(await harness.history())
            assert any(
                "human_messages" in str(note.content) and "Human follow-up" in str(note.content) for note in notes
            )
            assert any("Response from worker 2" in str(note.content) for note in notes)
            assert all("untrusted" in str(note.content) for note in notes)
            assert len(await harness.task_history(info.id, limit=1)) == 1
            with pytest.raises(ValueError, match="History limit"):
                await harness.task_history(info.id, limit=True)
            assert [session.id for session in await harness.list_sessions()] == [harness.session_id]
            await harness.new_session()
            with pytest.raises(ValueError, match="Unknown task"):
                await harness.task_history(info.id)
            with pytest.raises(ValueError, match="Unknown task"):
                _ = [event async for event in harness.continue_task(info.id, "Wrong session")]
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
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Original"})
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
            await asyncio.wait_for(parent_waiting.wait(), 3)
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), 3)
            info = harness.tasks.list()[0]
            monkeypatch.setattr(subagents, "MAX_TASKS", 1)
            with pytest.raises(ValueError, match="budget exhausted"):
                _ = [event async for event in harness.continue_task(info.id, "Must share the root budget")]
            assert harness.tasks.list()[0].followups == 0
            monkeypatch.setattr(subagents, "MAX_TASKS", 8)
            accepted = [event async for event in harness.continue_task(info.id, "Human message while parent busy")]
            assert len(accepted) == 1
            await asyncio.wait_for(child_waiting.wait(), 3)
            with pytest.raises(RuntimeError, match="Child is busy"):
                _ = [event async for event in harness.continue_task(info.id, "Do not silently queue this")]
            assert harness.tasks._used == 2 and harness.tasks.list()[0].followups == 1
            assert not notifications(await harness.history())
            release_child.set()
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), 3)
            release_parent.set()
            events = await asyncio.wait_for(task, 5)
            assert len([event for event in events if isinstance(event, TaskMessage)]) == 1
            assert len([event for event in events if isinstance(event, TaskCompleted)]) == 2
            assert_balanced(await harness.history())
            child_history = await harness.task_history(info.id)
            assert_balanced(child_history)
            assert not any("silently queue" in str(message.content) for message in child_history)
            assert any(
                "Human message while parent busy" in str(note.content)
                for note in notifications(await harness.history())
            )
            assert any("Follow-up result" in str(note.content) for note in notifications(await harness.history()))
        finally:
            release_child.set()
            release_parent.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_manual_descendant_followup_reaches_active_ancestor_and_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        ancestor_waiting, release_ancestor = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index < 2 and len(provider.requests) == 1:
                yield ToolCallEvent(id=f"spawn-{provider.index}", name="delegate", arguments={"prompt": "Nested"})
            else:
                if provider.index == 1 and len(provider.requests) == 2:
                    ancestor_waiting.set()
                    await release_ancestor.wait()
                yield TextDoneEvent(text=f"Result {provider.index}")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for(ancestor_waiting.wait(), 3)
            descendant = next(info for info in harness.tasks.list() if info.depth == 2)
            await asyncio.wait_for(harness.tasks._workers[descendant.id], 3)
            _ = [event async for event in harness.continue_task(descendant.id, "Human to grandchild")]
            await asyncio.wait_for(harness.tasks._workers[descendant.id], 3)
            assert harness.tasks._used == 3
            release_ancestor.set()
            events = await asyncio.wait_for(task, 5)
            ancestor = next(info for info in harness.tasks.list() if info.depth == 1)
            for history in (await harness.history(), await harness.task_history(ancestor.id)):
                assert_balanced(history)
                assert any("Human to grandchild" in str(note.content) for note in notifications(history))
                assert any("Result 3" in str(note.content) for note in notifications(history))
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
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Original"})
            elif provider.index == 2:
                child_waiting.set()
                await asyncio.Event().wait()
            else:
                yield TextDoneEvent(text="Finished")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            harness.config.max_subagent_depth = 0
            with pytest.raises(PermissionError, match="depth limit"):
                _ = [event async for event in harness.continue_task(info.id, "Disabled")]
            harness.config.max_subagent_depth = 2
            monkeypatch.setattr(subagents, "MAX_FOLLOWUPS", 0)
            with pytest.raises(ValueError, match="human follow-ups"):
                _ = [event async for event in harness.continue_task(info.id, "Too many")]
            monkeypatch.setattr(subagents, "MAX_FOLLOWUPS", 8)

            async def consume() -> list[HarnessEvent]:
                return [event async for event in harness.continue_task(info.id, "Explicit continuation")]

            task = asyncio.create_task(consume())
            await asyncio.wait_for(child_waiting.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert providers[2].closed and harness.tasks.list()[0].status == "cancelled"
            with pytest.raises(RuntimeError, match="cancellation"):
                _ = [event async for event in harness.continue_task(info.id, "Never replay")]
            events = await collect(harness, "A new request")
            assert not any(isinstance(event, TaskStarted | TaskCompleted | TaskMessage) for event in events)
            assert len(providers) == 3
            harness.tasks.begin(harness.session_id)
            monkeypatch.setattr(subagents, "MAX_RETAINED_TASKS", 1)
            with pytest.raises(ValueError, match="retention limit"):
                await harness.tasks.delegate("No unbounded retained conversations")
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
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Branch A"})
                yield ToolCallEvent(id="b", name="delegate", arguments={"prompt": "Branch B"})
            elif provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(id="grandchild", name="delegate", arguments={"prompt": "Grandchild"})
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
            await asyncio.wait_for(grandchild_waiting.wait(), 3)
            assert len(harness.tasks.list()) == 3
            stop = asyncio.create_task(harness.close())
            await asyncio.wait_for(closing.wait(), 3)
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
            assert not stop.done()
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, 5)
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


def test_child_timeout_cancels_descendants_but_not_independent_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        grandchild_started = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Branch A"})
                yield ToolCallEvent(id="b", name="delegate", arguments={"prompt": "Branch B"})
            elif provider.index == 1 and len(provider.requests) == 1:
                monkeypatch.setattr(subagents, "CHILD_TIMEOUT", 10.0)
                yield ToolCallEvent(id="nested", name="delegate", arguments={"prompt": "Waiting descendant"})
            elif provider.index in {1, 3}:
                if provider.index == 3:
                    grandchild_started.set()
                await asyncio.Event().wait()
            else:
                if provider.index == 2:
                    await grandchild_started.wait()
                yield TextDoneEvent(text="Independent work finished")

        monkeypatch.setattr(subagents, "CHILD_TIMEOUT", 0.5)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), 5)
            infos = harness.tasks.list()
            assert len(infos) == 3
            assert infos[0].status == "failed" and "time limit" in infos[0].error
            assert infos[1].status == "completed"
            assert infos[2].status == "cancelled"
            assert all(provider.closed for provider in providers[1:])
            completions = [event for event in events if isinstance(event, TaskCompleted)]
            assert len(completions) == 3
            assert any(event.status == "cancelled" for event in completions)
            assert_balanced(await harness.history())
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_continuation_permission_ceiling_and_no_restored_job_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "General work"})
            else:
                yield TextDoneEvent(text="Completed")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            await collect(harness)
            info = harness.tasks.list()[0]
            await harness.set_agent("reviewer")
            _ = [event async for event in harness.continue_task(info.id, "Continue read-only")]
            assert harness.tasks.list()[0].mode == "reviewer"
            assert "shell" not in providers[2].schemas[0]
            await harness.set_agent("build")
            _ = [event async for event in harness.continue_task(info.id, "Do not escalate this child")]
            assert harness.tasks.list()[0].mode == "reviewer"
            assert "shell" not in providers[3].schemas[0]
            restored = Harness(harness.config)
            try:
                await restored.resume(harness.session_id)
                assert restored.tasks.list() == []
                with pytest.raises(ValueError, match="not restored"):
                    _ = [event async for event in restored.continue_task(info.id, "No restored background jobs")]
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
                    yield ToolCallEvent(id=f"child-{index}", name="delegate", arguments={"prompt": "Propose a file"})
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
            await asyncio.wait_for(approving.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
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
                    id=f"child-{provider.index}", name="delegate", arguments={"prompt": "Build descendant"}
                )
            elif provider.index == depth + 2 and len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="write", name="write", arguments={"path": "escalated.txt", "content": "forbidden"}
                )
                yield ToolCallEvent(id="shell", name="shell", arguments={"command": "printf 'forbidden'"})
            else:
                yield TextDoneEvent(text="Completed")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.config.max_subagent_depth = depth
        approvals: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request)
            return True

        harness.approval_handler = approve
        try:
            await asyncio.wait_for(collect(harness), 5)
            infos = harness.tasks.list()
            assert len(infos) == depth and all(info.mode == "build" for info in infos)
            ancestor, descendant = infos[0], infos[-1]
            await harness.set_agent("reviewer")
            _ = [event async for event in harness.continue_task(ancestor.id, "Continue ancestor read-only")]
            assert harness.tasks.list()[0].mode == "reviewer"
            await harness.set_agent("build")
            _ = [event async for event in harness.continue_task(descendant.id, "Respect all retained ancestors")]
            assert harness.mode == "build" and harness.tasks.list()[-1].mode == "reviewer"
            assert all("write" not in schema and "shell" not in schema for schema in providers[-1].schemas)
            history = await harness.task_history(descendant.id)
            denied = [message for message in history if message.role == "tool" and message.name in {"write", "shell"}]
            assert len(denied) == 2 and all("reviewer" in str(message.content) for message in denied)
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
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Wait for cancellation"})
            else:
                yield TextDoneEvent(text="Waiting")

        monkeypatch.setattr(FakeProvider, "close", failing_close)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        task = asyncio.create_task(collect(harness))
        try:
            await asyncio.wait_for((started if phase == "execution" else closing).wait(), 3)
            worker = harness.tasks._workers[harness.tasks.list()[0].id]
            task.cancel()
            async with asyncio.timeout(3):
                while not worker.cancelling():
                    await asyncio.sleep(0)
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            info = harness.tasks.list()[0]
            worker = harness.tasks._workers[info.id]
            assert worker.cancelled() and providers[1].closed
            assert info.status == "cancelled"
            assert "cancelled" in info.error and "cleanup failed" in info.error
            assert "fake-secret-never-in-task-data" not in repr(info)
            histories = await harness.history(), await harness.task_history(info.id)
            # Admission must also fail closed if terminal metadata is stale.
            harness.tasks._infos[info.id].status = "failed"
            with pytest.raises(RuntimeError, match="cancellation"):
                _ = [event async for event in harness.continue_task(info.id, "Never resume cancelled work")]
            assert harness.tasks._workers[info.id] is worker
            assert harness.tasks.list()[0].followups == 0
            assert len(providers) == 2
            assert (await harness.history(), await harness.task_history(info.id)) == histories
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
            if not provider.index and messages[-1].role != "tool" and spawned < subagents.MAX_TASKS:
                spawned += 1
                yield ToolCallEvent(
                    id=f"spawn-{len(provider.requests)}", name="delegate", arguments={"prompt": "Use the shared budget"}
                )
            elif provider.index > subagents.MAX_TASKS and len(provider.requests) == 1:
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
            await asyncio.wait_for(collect(harness), 10)
            assert harness.tasks._used == subagents.MAX_TASKS == 8
            info = harness.tasks.list()[0]
            workers = dict(harness.tasks._workers)
            histories = await harness.history(), await harness.task_history(info.id)
            rejected_events: list[HarnessEvent] = []
            for _ in range(2):
                with pytest.raises(ValueError, match="budget exhausted"):
                    async for event in harness.continue_task(info.id, "Do not launch execution nine"):
                        rejected_events.append(event)
                assert harness.tasks._used == 8
                assert harness.tasks._workers == workers
                assert harness.tasks.list()[0] == info
                assert len(providers) == 9
                assert not approvals and not (tmp_path / "ninth-job.txt").exists()
                assert (await harness.history(), await harness.task_history(info.id)) == histories
            assert not any(
                isinstance(event, TaskMessage | TaskStarted | TaskCompleted | DoneEvent) for event in rejected_events
            )
            events = await collect(harness, "New external root run")
            assert harness.tasks._used == 0
            assert not any(isinstance(event, TaskStarted | TaskCompleted) for event in events)
            assert harness.tasks.list()[0] == info
            events = [event async for event in harness.continue_task(info.id, "Explicit follow-up in the new budget")]
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
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Cannot initialize"})
            else:
                yield TextDoneEvent(text="Completed")

        monkeypatch.setattr(Harness, "initialize", failing_initialize)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await collect(harness)
            info = harness.tasks.list()[0]
            assert info.status == "failed" and providers[1].closed and not providers[1].requests
            assert "fake-secret-never-in-task-data" not in repr(events) + repr(info)
            assert await harness.task_history(info.id) == []
            with pytest.raises(RuntimeError, match="incomplete setup"):
                _ = [event async for event in harness.continue_task(info.id, "No retained conversation")]
            assert len(providers) == 2 and harness.tasks.list()[0].followups == 0
            assert await harness.task_history(info.id) == []
        finally:
            await harness.close()

    asyncio.run(scenario())
