"""Process-local wake contracts and immediate-parent delivery, without provider I/O."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import AgentPlugin
from nagents.harness import subagents
from nagents.harness.runtime import Harness
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage
from nagents.harness.types import TaskNotification
from nagents.harness.types import TaskStarted
from nagents.types import ToolCall
from tests.test_subagents import FakeProvider
from tests.test_subagents import assert_balanced
from tests.test_subagents import collect
from tests.test_subagents import notifications
from tests.test_subagents import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.extensions import RunContext
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import HarnessEvent
    from nagents.types import JsonValue
    from nagents.types import Message


pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("alias", ["schedule_wakeup", "wake_up_in"])
@pytest.mark.parametrize("mode", ["build", "reviewer"])
@pytest.mark.parametrize("child", [False, True])
def test_native_scheduler_callback_acknowledges_without_holding_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str, mode: str, child: bool
) -> None:
    async def scenario() -> None:
        calls: list[tuple[str, float, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                if child and not provider.index:
                    yield ToolCallEvent(id="delegate", name="delegate", arguments={"prompt": "Child"})
                else:
                    yield ToolCallEvent(
                        id="schedule",
                        name=alias,
                        arguments={"seconds": 0.5, "minutes": 2, "hours": 1, "days": 1, "reason": " Check progress "},
                    )
            else:
                yield TextDoneEvent(text="Acknowledged; not waiting for the timer")

        harness, providers = setup_harness(tmp_path, monkeypatch, script, agent=mode)

        async def schedule(owner: str, seconds: float, reason: str) -> dict[str, str]:
            assert harness.tasks._active and harness._busy == "run"
            calls.append((owner, seconds, reason))
            return {"wakeup_id": "accepted", "status": "scheduled"}

        harness.wakeup_handler = schedule
        try:
            events = await asyncio.wait_for(collect(harness), 5)
            assert calls == [(harness.tasks.list()[0].id if child else "", 90120.5, "Check progress")]
            assert all({"schedule_wakeup", "wake_up_in"} <= set(schema) for p in providers for schema in p.schemas)
            history = await harness.task_history(calls[0][0]) if child else await harness.history()
            assert_balanced(history)
            results = [message for message in history if message.role == "tool" and message.name == alias]
            assert len(results) == 1 and "accepted" in str(results[0].content)
            assert len([event for event in events if isinstance(event, DoneEvent)]) == 1
            assert not any(isinstance(event, TaskNotification) and event.cause == "wakeup" for event in events)
            assert all(worker.done() for worker in harness.tasks._workers.values())
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"seconds": 0},
        {"seconds": -1},
        {"days": 7, "seconds": 0.01},
        {"seconds": True},
        {"seconds": "1"},
        {"seconds": None},
        {"seconds": float("nan")},
        {"minutes": float("inf")},
        {"hours": -float("inf")},
        {"days": 10**100},
        {"seconds": 1, "reason": ""},
        {"seconds": 1, "reason": " \n "},
        {"seconds": 1, "reason": "x" * 2001},
        {"seconds": 1, "reason": True},
    ],
)
def test_invalid_schedule_never_calls_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, JsonValue]
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)

        async def schedule(owner: str, seconds: float, reason: str) -> dict[str, str]:
            pytest.fail("Invalid scheduling arguments reached the lifecycle owner")

        harness.wakeup_handler = schedule
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            result = await harness.agent.tool_executor.execute(
                ToolCall("schedule", "schedule_wakeup", {"reason": "Check", **arguments})
            )
            assert result.error and result.result is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_scheduler_unavailable_failure_demo_and_active_run_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        calls: list[float] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        harness.config.demo = True
        call = ToolCall("schedule", "wake_up_in", {"days": 7, "reason": "Check"})

        async def schedule(owner: str, seconds: float, reason: str) -> dict[str, str]:
            calls.append(seconds)
            return {"status": "scheduled"}

        async def fail(owner: str, seconds: float, reason: str) -> dict[str, str]:
            raise RuntimeError("SECRET-callback-body")

        try:
            unavailable = await harness.agent.tool_executor.execute(call)
            assert unavailable.error and "unavailable" in unavailable.error
            harness.wakeup_handler = schedule
            inactive = await harness.agent.tool_executor.execute(call)
            assert inactive.error and "active parent run" in inactive.error and not calls
            await harness.initialize()
            harness.tasks.begin(harness.session_id)
            accepted = await harness.agent.tool_executor.execute(call)
            assert not accepted.error and calls == [604800.0]
            harness.wakeup_handler = fail
            failed = await harness.agent.tool_executor.execute(call)
            assert failed.error and "could not be scheduled" in failed.error and "SECRET" not in failed.error
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_root_wake_without_children_preserves_budget_and_normal_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        before: list[str] = []
        after: list[str] = []

        class Hooks(AgentPlugin):
            async def before_run(self, context: RunContext, message: Message) -> Message:
                before.append(str(message.content))
                return message

            async def after_run(self, context: RunContext) -> None:
                after.append(context.session_id)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="Root resumed")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.agent.plugins.append(Hooks())
        try:
            await collect(harness, "Root work")
            harness.tasks._used = subagents.MAX_TASKS
            events = [event async for event in harness.wake("Root reminder")]
            assert harness.tasks._used == subagents.MAX_TASKS
            assert harness.tasks.list() == [] and len(providers) == 1
            notes = [event for event in events if isinstance(event, TaskNotification)]
            assert len(notes) == 1
            assert (notes[0].source_task_id, notes[0].recipient_task_id) == ("", "")
            assert (notes[0].source_name, notes[0].recipient_name, notes[0].cause) == ("Main", "Main", "wakeup")
            assert before[-1].startswith("BACKGROUND TASK NOTIFICATION:") and "untrusted" in before[-1]
            assert "Root reminder" in before[-1] and after == [harness.session_id] * 2
            assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "Root resumed"
            assert_balanced(await harness.history())
        finally:
            await harness.close()

    asyncio.run(scenario())


async def tree_script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    original = next(str(message.content) for message in messages if message.role == "user")
    note = str(messages[-1].content)
    if original == "ROOT" and len(provider.requests) == 1:
        yield ToolCallEvent(id="A", name="delegate", arguments={"prompt": "A"})
        yield ToolCallEvent(id="sibling", name="delegate", arguments={"prompt": "SIBLING"})
    elif original == "A" and note == "A":
        yield ToolCallEvent(id="B", name="delegate", arguments={"prompt": "B"})
    elif original == "B":
        yield TextDoneEvent(text="B_WAKE_PRIVATE" if '"wakeups"' in note else "B_INITIAL_PRIVATE")
    elif original == "A":
        yield TextDoneEvent(text="A_WAKE_SYNTHESIS" if "B_WAKE_PRIVATE" in note else "A_INITIAL_SYNTHESIS")
    else:
        yield TextDoneEvent(text=f"{original}_RESULT")


@pytest.mark.parametrize("failed_parent", [False, True])
def test_closed_grandchild_wakes_same_immediate_parent_then_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_parent: bool
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if failed_parent and "B_INITIAL_PRIVATE" in str(messages[-1].content):
                yield ErrorEvent(message="Initial parent failed", recoverable=False)
            else:
                async for event in tree_script(provider, messages):
                    yield event

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            initial = await asyncio.wait_for(collect(harness, "ROOT"), 5)
            infos = {info.prompt: info for info in harness.tasks.list()}
            a, b, sibling = infos["A"], infos["B"], infos["SIBLING"]
            assert a.status == ("failed" if failed_parent else "completed")
            assert all(child._closed for child in harness.tasks._children.values())
            assert harness.tasks._used == 3
            sibling_history = await harness.task_history(sibling.id)
            events = [event async for event in harness.wake("Wake B", task_id=b.id)]
            updated = {info.prompt: info for info in harness.tasks.list()}
            assert harness.tasks._used == 5 and len(providers) == 6
            for old in (a, b):
                current = updated[old.prompt]
                assert (current.id, current.child_session_id, current.parent_task_id, current.parent_session_id) == (
                    old.id,
                    old.child_session_id,
                    old.parent_task_id,
                    old.parent_session_id,
                )
                assert current.status == "completed" and current.activation == 1 and current.followups == 0
            assert updated["A"].trigger == "notification" and updated["B"].trigger == "wakeup"
            assert await harness.task_history(sibling.id) == sibling_history
            assert not any(isinstance(event, TaskMessage) for event in events)
            starts = [event for event in events if isinstance(event, TaskStarted)]
            assert [(event.task_id, event.activation, event.trigger) for event in starts] == [
                (b.id, 1, "wakeup"),
                (a.id, 1, "notification"),
            ]
            deliveries = [event for event in events if isinstance(event, TaskNotification)]
            assert [(event.source_task_id, event.recipient_task_id, event.cause) for event in deliveries] == [
                (b.id, b.id, "wakeup"),
                (b.id, a.id, "completion"),
                (a.id, "", "completion"),
            ]
            assert len({event.notification_id for event in deliveries}) == 3
            assert any(
                isinstance(event, TaskCompleted) and event.task_id == b.id and event.result == "B_WAKE_PRIVATE"
                for event in events
            )
            assert any(
                isinstance(event, TaskNotification) and event.source_task_id == b.id and event.recipient_task_id == a.id
                for event in initial
            )
            root_history, parent_history = await harness.history(), await harness.task_history(a.id)
            assert "B_WAKE_PRIVATE" in str(notifications(parent_history))
            assert "B_WAKE_PRIVATE" not in str(root_history) and "B_INITIAL_PRIVATE" not in str(root_history)
            assert "A_WAKE_SYNTHESIS" in str(notifications(root_history))
            for history in (root_history, parent_history, await harness.task_history(b.id)):
                assert_balanced(history)
            assert len([event for event in events if isinstance(event, DoneEvent)]) == 1
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_parent_reactivation_quota_failure_does_not_fall_back_to_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness, providers = setup_harness(tmp_path, monkeypatch, tree_script)
        try:
            await collect(harness, "ROOT")
            b = next(info for info in harness.tasks.list() if info.prompt == "B")
            requests = len(providers[0].requests)
            monkeypatch.setattr(subagents, "MAX_TASKS", 4)
            events = [event async for event in harness.wake("Wake B", task_id=b.id)]
            assert harness.tasks._used == 4 and len(providers[0].requests) == requests
            assert any(isinstance(event, ErrorEvent) and "quota exhausted" in event.message for event in events)
            assert not any(isinstance(event, TaskNotification) and event.cause == "completion" for event in events)
            assert "B_WAKE_PRIVATE" not in str(await harness.history())
            assert next(info for info in harness.tasks.list() if info.prompt == "A").activation == 0
            assert all(worker.done() for worker in harness.tasks._workers.values())
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["missing", "cancelled", "missing-handle"])
def test_parent_becomes_unavailable_during_wake_never_receives_or_reroutes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    async def scenario() -> None:
        waiting, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if '"wakeups"' in str(messages[-1].content):
                waiting.set()
                await release.wait()
            async for event in tree_script(provider, messages):
                yield event

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        task: asyncio.Task[list[HarnessEvent]] | None = None
        try:
            await collect(harness, "ROOT")
            infos = {info.prompt: info for info in harness.tasks.list()}
            a, b = infos["A"], infos["B"]
            requests = len(providers[0].requests)

            async def wake() -> list[HarnessEvent]:
                return [event async for event in harness.wake("Wake B", task_id=b.id)]

            task = asyncio.create_task(wake())
            await asyncio.wait_for(waiting.wait(), 5)
            if invalid == "missing":
                harness.tasks._infos[b.id].parent_task_id = "missing"
            elif invalid == "cancelled":
                harness.tasks._infos[a.id].status = "cancelled"
            else:
                harness.tasks._children.pop(a.id)
            release.set()
            events = await asyncio.wait_for(task, 5)
            assert any(isinstance(event, ErrorEvent) and "not forwarded to Main" in event.message for event in events)
            assert any(isinstance(event, TaskCompleted) and event.task_id == b.id for event in events)
            assert not any(isinstance(event, TaskNotification) and event.cause == "completion" for event in events)
            assert len(providers[0].requests) == requests and "B_WAKE_PRIVATE" not in str(await harness.history())
        finally:
            release.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["build", "reviewer"])
def test_woken_child_keeps_permissions_and_activation_scoped_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async def scenario() -> None:
        approvals: list[ApprovalRequest] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].role == "user" and '"wakeups"' in str(messages[-1].content):
                yield ToolCallEvent(id="write", name="write", arguments={"path": "forbidden.txt", "content": "no"})
                yield ToolCallEvent(id="shell", name="shell", arguments={"command": "printf forbidden"})
            else:
                async for event in tree_script(provider, messages):
                    yield event

        async def deny(request: ApprovalRequest) -> bool:
            approvals.append(request)
            return False

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.approval_handler = deny
        try:
            await collect(harness, "ROOT")
            b = next(info for info in harness.tasks.list() if info.prompt == "B")
            await harness.set_agent(mode)
            _ = [event async for event in harness.wake("Try tools", task_id=b.id)]
            assert len(approvals) == (2 if mode == "build" else 0)
            assert all(request.task_id == b.id and request.activation == 1 for request in approvals)
            assert not (tmp_path / "forbidden.txt").exists()
            assert all(
                ("shell" in schema) == (mode == "build") for provider in providers[4:] for schema in provider.schemas
            )
            history = await harness.task_history(b.id)
            denied = [message for message in history if message.role == "tool" and message.name in {"write", "shell"}]
            assert len(denied) == 2 and all(
                "denied" in str(message.content) or "denies" in str(message.content) for message in denied
            )
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["cancel", "aclose"])
def test_wake_cancellation_joins_children_without_reactivating_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    async def scenario() -> None:
        waiting = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if '"wakeups"' in str(messages[-1].content):
                waiting.set()
                await asyncio.Event().wait()
            async for event in tree_script(provider, messages):
                yield event

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            await collect(harness, "ROOT")
            b = next(info for info in harness.tasks.list() if info.prompt == "B")
            async with aclosing(harness.wake("Wait", task_id=b.id)) as stream:
                if stop == "aclose":
                    async for event in stream:
                        if isinstance(event, TaskNotification) and event.cause == "wakeup":
                            await asyncio.wait_for(waiting.wait(), 5)
                            break
                else:

                    async def consume() -> list[HarnessEvent]:
                        return [event async for event in stream]

                    task = asyncio.create_task(consume())
                    await asyncio.wait_for(waiting.wait(), 5)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 5)
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert len(providers) == 5 and providers[-1].closed
            assert next(info for info in harness.tasks.list() if info.prompt == "A").activation == 0
            assert next(info for info in harness.tasks.list() if info.prompt == "B").status == "cancelled"
            assert not harness.tasks._pending and not harness.tasks._observed
            with pytest.raises(RuntimeError, match="cancellation"):
                _ = [event async for event in harness.wake("Never replay", task_id=b.id)]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_completion_observation_precedes_immediate_parent_delivery_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        paused, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events: list[HarnessEvent] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            original = next(str(message.content) for message in messages if message.role == "user")
            if original == "A" and messages[-1].role == "tool":
                paused.set()
                await release.wait()
            async for event in tree_script(provider, messages):
                yield event

        harness, _ = setup_harness(tmp_path, monkeypatch, script)

        async def consume() -> None:
            async for event in harness.run("ROOT"):
                events.append(event)
                if isinstance(event, TaskCompleted) and event.depth == 2:
                    observed.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(asyncio.gather(paused.wait(), observed.wait()), 5)
            b = next(info for info in harness.tasks.list() if info.prompt == "B")
            a = next(info for info in harness.tasks.list() if info.prompt == "A")
            assert not any(isinstance(event, TaskNotification) and event.source_task_id == b.id for event in events)
            assert not notifications(await harness.task_history(a.id))
            release.set()
            await asyncio.wait_for(task, 5)
            notes = [event for event in events if isinstance(event, TaskNotification) and event.source_task_id == b.id]
            assert len(notes) == 1 and notes[0].recipient_task_id == a.id
            assert "B_INITIAL_PRIVATE" not in str(await harness.history())
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_automatic_activations_do_not_consume_human_followups_or_reset_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness, providers = setup_harness(tmp_path, monkeypatch, tree_script)
        try:
            await collect(harness, "ROOT")
            b = next(info for info in harness.tasks.list() if info.prompt == "B")
            monkeypatch.setattr(subagents, "MAX_FOLLOWUPS", 0)
            _ = [event async for event in harness.wake("Automatic", task_id=b.id)]
            assert harness.tasks._used == 5
            with pytest.raises(ValueError, match="human follow-ups"):
                _ = [event async for event in harness.continue_task(b.id, "Human")]
            monkeypatch.setattr(subagents, "MAX_FOLLOWUPS", 8)
            events = [event async for event in harness.continue_task(b.id, "Human")]
            starts = [event for event in events if isinstance(event, TaskStarted)]
            assert [(event.activation, event.followup, event.trigger) for event in starts] == [
                (2, 1, "human"),
                (2, 0, "notification"),
            ]
            assert harness.tasks._used == 7
            monkeypatch.setattr(subagents, "MAX_TASKS", 7)
            count = len(providers)
            with pytest.raises(ValueError, match="budget exhausted"):
                _ = [event async for event in harness.wake("No eighth execution", task_id=b.id)]
            assert harness.tasks._used == 7 and len(providers) == count
            _ = [event async for event in harness.wake("Main still can synthesize")]
            assert harness.tasks._used == 7
            await collect(harness, "New external root turn")
            assert harness.tasks._used == 0
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["missing-parent", "cancelled-parent", "depth", "other-session", "restart"])
def test_wake_admission_rejects_invalid_ancestry_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    async def scenario() -> None:
        harness, providers = setup_harness(tmp_path, monkeypatch, tree_script)
        restored: Harness | None = None
        try:
            await collect(harness, "ROOT")
            a = next(info for info in harness.tasks.list() if info.prompt == "A")
            b = next(info for info in harness.tasks.list() if info.prompt == "B")
            owner = harness
            if invalid == "missing-parent":
                harness.tasks._infos[b.id].parent_task_id = "missing"
            elif invalid == "cancelled-parent":
                harness.tasks._infos[a.id].status = "cancelled"
            elif invalid == "depth":
                harness.config.max_subagent_depth = 1
            elif invalid == "other-session":
                await harness.new_session()
            else:
                restored = Harness(harness.config)
                await restored.resume(harness.session_id)
                owner = restored
            calls = sum(len(provider.requests) for provider in providers)
            with pytest.raises((ValueError, RuntimeError, PermissionError)):
                _ = [event async for event in owner.wake("Rejected", task_id=b.id)]
            assert harness.tasks._infos[b.id].activation == 0 and harness.tasks._used == 3
            assert sum(len(provider.requests) for provider in providers) == calls
        finally:
            if restored is not None:
                await restored.close()
            await harness.close()

    asyncio.run(scenario())


def test_busy_root_wake_rejects_overlap_and_cancellation_runs_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        waiting = asyncio.Event()
        cleaned: list[str] = []

        class Hooks(AgentPlugin):
            async def after_run(self, context: RunContext) -> None:
                cleaned.append(context.session_id)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            waiting.set()
            await asyncio.Event().wait()
            yield TextDoneEvent(text="unreachable")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.agent.plugins.append(Hooks())

        async def wake() -> list[HarnessEvent]:
            return [event async for event in harness.wake("Wait")]

        task = asyncio.create_task(wake())
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            with pytest.raises(RuntimeError, match="busy"):
                _ = [event async for event in harness.wake("Overlap")]
            with pytest.raises(RuntimeError, match="busy"):
                await harness.new_session()
            assert len(providers[0].requests) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert cleaned == [harness.session_id]
            assert not harness._busy and harness._worker is None
            assert_balanced(await harness.history())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_closed_deep_tree_reactivates_each_parent_not_all_ancestors_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            original = next(str(message.content) for message in messages if message.role == "user")
            if messages[-1].content == original and original != "C":
                yield ToolCallEvent(
                    id=original, name="delegate", arguments={"prompt": {"ROOT": "A", "A": "B", "B": "C"}[original]}
                )
            else:
                yield TextDoneEvent(text=f"{original}_SYNTHESIS")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        harness.config.max_subagent_depth = 3
        try:
            await collect(harness, "ROOT")
            infos = {info.prompt: info for info in harness.tasks.list()}
            events = [event async for event in harness.wake("Leaf", task_id=infos["C"].id)]
            assert harness.tasks._used == 6
            assert [
                (event.source_task_id, event.recipient_task_id)
                for event in events
                if isinstance(event, TaskNotification)
            ] == [
                (infos["C"].id, infos["C"].id),
                (infos["C"].id, infos["B"].id),
                (infos["B"].id, infos["A"].id),
                (infos["A"].id, ""),
            ]
            assert all(info.activation == 1 and info.followups == 0 for info in harness.tasks.list())
            root_history = str(await harness.history())
            assert (
                "A_SYNTHESIS" in root_history
                and "B_SYNTHESIS" not in root_history
                and "C_SYNTHESIS" not in root_history
            )
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_followup_rejects_starting_ancestor_without_losing_original_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        initialized, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events: list[HarnessEvent] = []
        harness, providers = setup_harness(tmp_path, monkeypatch, tree_script)
        task: asyncio.Task[None] | None = None
        try:
            await collect(harness, "ROOT")
            infos = {info.prompt: info for info in harness.tasks.list()}
            a, b = infos["A"], infos["B"]
            original_initialize = Harness.initialize

            async def pause_initialized_parent(child: Harness) -> None:
                await original_initialize(child)
                if child._task_id == a.id and child._activation == 1 and not child.tasks._active:
                    initialized.set()
                    await release.wait()

            monkeypatch.setattr(Harness, "initialize", pause_initialized_parent)

            async def wake() -> None:
                async for event in harness.wake("Wake B", task_id=b.id):
                    events.append(event)
                    if isinstance(event, TaskStarted) and event.task_id == a.id:
                        started.set()

            task = asyncio.create_task(wake())
            await asyncio.wait_for(asyncio.gather(started.wait(), initialized.wait()), 5)
            parent = harness.tasks._children[a.id]
            assert parent._initialized and not parent.tasks._active
            assert harness.tasks._infos[a.id].status == "running"
            assert not harness.tasks._workers[a.id].done()
            assert harness.tasks._workers[b.id].done()
            retained = harness.tasks.list()
            workers = dict(harness.tasks._workers)
            budget = harness.tasks._used
            accepted: list[HarnessEvent] = []
            with pytest.raises(RuntimeError, match="Ancestor is starting or stopping"):
                async for event in harness.continue_task(b.id, "Second human follow-up"):
                    accepted.append(event)
            assert not accepted
            assert harness.tasks.list() == retained and harness.tasks._workers == workers
            assert harness.tasks._used == budget == 5 and len(providers) == 6
            release.set()
            await asyncio.wait_for(task, 5)
            deliveries = [event for event in events if isinstance(event, TaskNotification)]
            assert [(event.source_task_id, event.recipient_task_id, event.cause) for event in deliveries] == [
                (b.id, b.id, "wakeup"),
                (b.id, a.id, "completion"),
                (a.id, "", "completion"),
            ]
            assert deliveries[1].text == "B_WAKE_PRIVATE" and deliveries[2].text == "A_WAKE_SYNTHESIS"
            parent_history = await harness.task_history(a.id)
            root_history = await harness.history()
            assert sum("B_WAKE_PRIVATE" in str(note.content) for note in notifications(parent_history)) == 1
            assert "B_WAKE_PRIVATE" not in str(root_history) and "A_WAKE_SYNTHESIS" in str(root_history)
            assert "Second human follow-up" not in str(await harness.task_history(b.id))
            assert not any(isinstance(event, ErrorEvent | TaskMessage) for event in events)
            assert all(info.status == "completed" for info in harness.tasks.list())
            assert isinstance(events[-1], DoneEvent)
            assert_balanced(parent_history)
            assert_balanced(root_history)
        finally:
            release.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("child", [False, True])
def test_scheduler_suppressing_cancellation_cannot_restart_model_after_single_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child: bool
) -> None:
    async def scenario() -> None:
        scheduling, suppressed, parent_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        unexpected_request, release_unexpected = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if child and not provider.index:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "Schedule"})
                else:
                    parent_waiting.set()
                    yield TextDoneEvent(text="Waiting for child")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="schedule", name="schedule_wakeup", arguments={"seconds": 1, "reason": "Check"})
            else:
                unexpected_request.set()
                await release_unexpected.wait()
                yield TextDoneEvent(text="Must not request another response after cancellation")

        async def schedule(owner: str, seconds: float, reason: str) -> dict[str, str]:
            scheduling.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                suppressed.set()
            return {"status": "scheduled"}

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        harness.config.demo = True
        harness.wakeup_handler = schedule
        consumer = asyncio.create_task(collect(harness, "Schedule"))
        try:
            await asyncio.wait_for(scheduling.wait(), 5)
            if child:
                await asyncio.wait_for(parent_waiting.wait(), 5)
            requests = [len(provider.requests) for provider in providers]
            producer = harness._worker
            assert producer is not None
            consumer.cancel()
            # asyncio.wait does not issue another cancellation when its timeout expires.
            done, _ = await asyncio.wait((consumer,), timeout=5)
            assert consumer in done, "A single consumer cancellation must finish owned cleanup"
            with pytest.raises(asyncio.CancelledError):
                await consumer
            assert consumer.cancelling() == 1 and suppressed.is_set()
            assert not unexpected_request.is_set()
            assert [len(provider.requests) for provider in providers] == requests
            assert producer.done() and harness._worker is None and not harness._busy
            assert all(worker.done() for worker in harness.tasks._workers.values())
            assert all(provider.closed for provider in providers[1:])
            assert not harness.tasks._pending and not harness.tasks._observed
            assert_balanced(await harness.history())
            if child:
                info = harness.tasks.list()[0]
                assert info.status == "cancelled"
                assert_balanced(await harness.task_history(info.id))
        finally:
            release_unexpected.set()
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())
