"""Approval latency is not tool execution time, on any executor outcome."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.harness import ApprovalRequest
from nagents.harness import Harness
from nagents.harness.tools import timing
from nagents.harness.types import ToolApprovalTiming
from nagents.types import Message
from nagents.types import ToolCall
from tests.support.providers import FakeProvider
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class Clock:
    now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def unused(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="Unused")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "denied", "approval_error", "tool_error"])
@pytest.mark.parametrize("builtin", [True, False])
async def test_multiple_approvals_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, builtin: bool
) -> None:
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused)
    clock = Clock()
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=clock.monotonic))
    decisions = 0

    async def approve(request: ApprovalRequest) -> bool:
        nonlocal decisions
        decisions += 1
        clock.advance(50)
        if outcome == "approval_error":
            raise RuntimeError("approval failed")
        return outcome != "denied"

    async def measured() -> str:
        clock.advance(2)
        await harness.approve("measured", {}, "first")
        clock.advance(3)
        await harness.approve("measured", {}, "second")
        clock.advance(4)
        if outcome == "tool_error":
            raise RuntimeError("tool failed")
        return "ok"

    harness.approval_handler = approve
    try:
        await harness.initialize()
        harness.config.demo = False
        harness.agent.tool_registry.register(measured)
        if builtin:
            harness.tools.builtins["measured"] = measured
        result = await harness.agent.tool_executor.execute(ToolCall("call", "measured", {}))
        failed_gate = outcome in {"denied", "approval_error"}
        assert result.duration_ms == (2000 if builtin and failed_gate else 0 if failed_gate else 9000)
        assert bool(result.error) == (outcome != "success")
        assert decisions == (1 if failed_gate else 2 if builtin else 3)
        assert timing.current_clock(harness) is None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_concurrent_lock_queue_is_excluded_without_pausing_other_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused)
    clock = Clock()
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=clock.monotonic))
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events: list[ToolApprovalTiming] = []

    async def emit(event: object) -> None:
        if isinstance(event, ToolApprovalTiming):
            events.append(event)
            if event.call_id == "second" and event.waiting:
                queued.set()

    async def approve(request: ApprovalRequest) -> bool:
        if request.id == "first":
            entered.set()
            await release.wait()
        clock.advance(30)
        return True

    async def measured() -> str:
        current = timing.current_clock(harness)
        assert current is not None
        await harness.approve("measured", {}, "gate", call_id=current.call_id)
        clock.advance(2)
        return "ok"

    harness.approval_handler = approve
    monkeypatch.setattr(harness, "emit", emit)
    try:
        await harness.initialize()
        harness.config.demo = False
        harness.agent.tool_registry.register(measured)
        harness.tools.builtins["measured"] = measured
        first = asyncio.create_task(harness.agent.tool_executor.execute(ToolCall("first", "measured", {})))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        except TimeoutError:
            assert not first.done(), first.result()
            raise
        second = asyncio.create_task(harness.agent.tool_executor.execute(ToolCall("second", "measured", {})))
        await queued.wait()
        clock.advance(100)

        # An unrelated invocation stays active while the first two await approval.
        async def active() -> str:
            clock.advance(5)
            return "active"

        harness.agent.tool_registry.register(active)
        harness.tools.builtins["active"] = active
        other = await harness.agent.tool_executor.execute(ToolCall("active", "active", {}))
        assert other.duration_ms == 5000
        release.set()
        results = await asyncio.gather(first, second)
        assert [result.duration_ms for result in results] == [2000, 2000]
        assert [(event.call_id, event.waiting) for event in events] == [
            ("first", True),
            ("second", True),
            ("first", False),
            ("second", False),
        ]
    finally:
        release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_cancellation_and_inherited_context_do_not_leak_clock() -> None:
    owner = object()
    with timing.invocation(owner, "parent") as parent:

        async def inherited() -> None:
            assert timing.current_clock(owner) is None
            with timing.invocation(owner, "child") as child:
                assert timing.current_clock(owner) is child
            assert timing.current_clock(owner) is None

        await asyncio.create_task(inherited())
        with pytest.raises(asyncio.CancelledError), parent.pause():
            raise asyncio.CancelledError
        assert parent.depth == 0
        assert timing.current_clock(owner) is parent
    assert timing.current_clock(owner) is None
