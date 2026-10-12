"""Designer admission reserves a real lifecycle owner before publishing traces."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from nagents.designer.runtime import DesignProvider
from nagents.designer.runtime import DesignedHarness
from nagents.designer.schema import STARTER
from nagents.events import TextDoneEvent
from nagents.web.designer import Designer
from nagents.web.designer import TestRun as DesignerInput
from nagents.web.executions import MAX_PARALLEL_RUNS
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event


def test_rejected_designer_admission_has_no_trace_or_runtime_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def generate(self: DesignProvider, *args: object, **kwargs: object) -> AsyncIterator[Event]:
            await release.wait()
            yield TextDoneEvent(text="Completed")

        monkeypatch.setattr(DesignProvider, "generate", generate)
        closed: list[DesignedHarness] = []
        original_close = DesignedHarness.close

        async def close(self: DesignedHarness) -> None:
            closed.append(self)
            await original_close(self)

        monkeypatch.setattr(DesignedHarness, "close", close)
        async with client_app(tmp_path) as (app, _, _, _):
            designer = Designer(app.state.web)
            accepted = [
                await designer.run(DesignerInput(source=STARTER, prompt="Wait")) for _ in range(MAX_PARALLEL_RUNS)
            ]
            before = await designer.traces.runs()
            assert len(before) == MAX_PARALLEL_RUNS
            for previous in ("", accepted[0]["run_id"]):
                with pytest.raises(HTTPException) as conflict:
                    await designer.run(DesignerInput(source=STARTER, prompt="Other", previous_run=previous))
                assert conflict.value.status_code == 409
                assert await designer.traces.runs() == before
            assert len(closed) == 2 and all(harness._closed for harness in closed)
            assert len(app.state.web.executions.runs) == MAX_PARALLEL_RUNS
            for run in tuple(app.state.web.executions.runs.values()):
                await app.state.web.stop(run)
            assert len(closed) == MAX_PARALLEL_RUNS + 2
            assert len({id(harness) for harness in closed}) == len(closed)
            assert not app.state.web.executions.runs

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_designer_trace_start_unwinds_reserved_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        async with client_app(tmp_path) as (app, _, _, _):
            designer = Designer(app.state.web)
            original_start = designer.traces.start
            original_close = DesignedHarness.close
            closed: list[DesignedHarness] = []

            async def start(run_id: str, session_id: str, agent: str, design: str) -> None:
                await original_start(run_id, session_id, agent, design)
                entered.set()
                if cancel:
                    await release.wait()
                else:
                    raise OSError("Trace startup failed after commit")

            async def close(self: DesignedHarness) -> None:
                closed.append(self)
                await original_close(self)

            monkeypatch.setattr(designer.traces, "start", start)
            monkeypatch.setattr(DesignedHarness, "close", close)
            execute = AsyncMock()
            monkeypatch.setattr(designer, "execute", execute)
            pending = asyncio.create_task(designer.run(DesignerInput(source=STARTER, prompt="Never dispatched")))
            async with asyncio.timeout(HANG_GUARD):
                await entered.wait()
                if cancel:
                    pending.cancel()
                    release.set()
                with pytest.raises(asyncio.CancelledError if cancel else OSError):
                    await pending
            execute.assert_not_awaited()
            assert not app.state.web.executions.runs
            assert await designer.traces.runs() == []
            assert len(closed) == 1 and closed[0]._closed

    asyncio.run(scenario())
