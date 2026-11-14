"""Idle shell controls never require a model activation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from nagents.tui.shell_jobs import ShellJobsModal
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


def test_idle_jobs_and_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        jobs = AsyncMock()
        jobs.list.return_value = [{"job_id": "job-1", "state": "running", "command": "sleep 30"}]
        backend.shell_jobs = jobs  # type: ignore[attr-defined]
        app = make_app(backend)
        async with app.run_test(size=(120, 35)) as pilot:
            await idle(app, pilot)
            app.command("/jobs")
            await pilot.pause()
            assert isinstance(app.screen, ShellJobsModal)
            assert "job-1" in app.screen.text
            await pilot.press("escape")
            app.command("/jobs stop job-1")
            await pilot.pause()
            jobs.stop.assert_awaited_once_with("job-1", session_id=backend.session_id)
            assert backend.prompts == []
            assert isinstance(app.screen, ShellJobsModal)
            await pilot.press("escape")
            await app._shell_work_pending("ngn-other")
            await pilot.pause()
            assert "ngn-other" in app._shell_pending
            assert backend.prompts == []

    asyncio.run(scenario())
