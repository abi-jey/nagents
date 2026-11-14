"""Exercise the actual built-in write path, not just a test callable."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from nagents.harness import ApprovalRequest
from nagents.harness import Harness
from nagents.harness.tools import timing
from nagents.types import ToolCall
from tests.harness.tools.test_tool_timing import Clock
from tests.harness.tools.test_tool_timing import unused
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from pathlib import Path


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
