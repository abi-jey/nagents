"""Exercise the actual built-in write path, not just a test callable."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from nagents.harness import ApprovalRequest
from nagents.harness import Harness
from nagents.harness.tools import timing
from nagents.harness.types import ToolApprovalTiming
from nagents.types import ToolCall
from tests.harness.tools.test_tool_timing import Clock
from tests.harness.tools.test_tool_timing import unused
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_write_excludes_human_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow: bool) -> None:
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused)
    clock = Clock()
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=clock.monotonic))

    async def approve(request: ApprovalRequest) -> bool:
        assert request.tool == "write"
        clock.advance(600)
        return allow

    harness.approval_handler = approve
    try:
        await harness.initialize()
        result = await harness.agent.tool_executor.execute(
            ToolCall("write-call", "write", {"path": "created.txt", "content": "hello"})
        )
        assert result.duration_ms == 0
        assert bool(result.error) is not allow
        assert (tmp_path / "created.txt").exists() is allow
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cancelled_approval_cleans_up_before_next_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused)
    entered = asyncio.Event()

    async def approve(request: ApprovalRequest) -> bool:
        entered.set()
        await asyncio.Future[None]()
        return True

    harness.approval_handler = approve
    try:
        await harness.initialize()
        task = asyncio.create_task(
            harness.agent.tool_executor.execute(
                ToolCall("cancelled", "write", {"path": "cancelled.txt", "content": "no"})
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not harness._approval_lock.locked()
        assert timing.current_clock(harness) is None
        assert not (tmp_path / "cancelled.txt").exists()
        result = await harness.agent.tool_executor.execute(ToolCall("next", "list_files", {}))
        assert result.error is None
        assert result.duration_ms >= 0
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_cancelled_approval_with_full_queue_does_not_wait_for_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool
) -> None:
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused)
    entered = asyncio.Event()

    async def approve(request: ApprovalRequest) -> bool:
        entered.set()
        await asyncio.Future[None]()
        return True

    harness.approval_handler = approve
    try:
        await harness.initialize()
        harness._queue = asyncio.Queue(maxsize=1)
        queue = harness._queue
        if queued:
            await harness._approval_lock.acquire()
        task = asyncio.create_task(
            harness.agent.tool_executor.execute(
                ToolCall("cancelled", "write", {"path": "cancelled.txt", "content": "no"})
            )
        )
        try:
            if queued:
                # The entry snapshot fills the queue before waiting for the lock.
                snapshot = await asyncio.wait_for(queue.get(), 2)
                queue.put_nowait(snapshot)
                assert not entered.is_set()
            else:
                await asyncio.wait_for(entered.wait(), 2)
            assert queue.full()
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=2)
            assert task in done, "Cancellation blocked on a full telemetry queue"
            with pytest.raises(asyncio.CancelledError):
                await task
            snapshot = queue.get_nowait()
            assert isinstance(snapshot, ToolApprovalTiming)
            assert snapshot.waiting
            assert queue.empty()
            assert not (tmp_path / "cancelled.txt").exists()
            assert timing.current_clock(harness) is None
            if not queued:
                assert not harness._approval_lock.locked()
        finally:
            # Drain before joining even on the old, blocking implementation.
            while not queue.empty():
                queue.get_nowait()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if queued:
                harness._approval_lock.release()
            harness._queue = None
    finally:
        await harness.close()
