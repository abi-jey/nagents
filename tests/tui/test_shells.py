"""Idle shell controls never require a model activation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from nagents.tui.shells import ShellsModal
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


def test_idle_jobs_and_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        shells = AsyncMock()
        shells.list.return_value = [{"shell_id": "shell-1", "state": "running", "command": "sleep 30"}]
        backend.shells = shells  # type: ignore[attr-defined]
        app = make_app(backend)
        async with app.run_test(size=(120, 35)) as pilot:
            await idle(app, pilot)
            app.command("/shells")
            await pilot.pause()
            assert isinstance(app.screen, ShellsModal)
            assert "shell-1" in app.screen.text
            await pilot.press("escape")
            app.command("/shells stop shell-1")
            await pilot.pause()
            shells.stop.assert_awaited_once_with("shell-1", session_id=backend.session_id)
            assert backend.prompts == []
            assert isinstance(app.screen, ShellsModal)
            await pilot.press("escape")
            await app._shell_work_pending("ngn-other")
            await pilot.pause()
            assert "ngn-other" in app._shell_pending
            assert backend.prompts == []

    asyncio.run(scenario())
