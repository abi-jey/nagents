"""Performance diagnostics stay TUI-owned and usable during active runs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.widgets import Static

from nagents.tui.performance import PerformancePanel
from nagents.tui.screens import DetailModal
from nagents.tui.widgets import Composer
from tests.support.hang_guard import HANG_GUARD
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


def test_performance_panel_and_persistent_commands(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))

    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.tui_performance = True
        app = make_app(backend)
        async with app.run_test(size=(140, 45)) as pilot:
            await idle(app, pilot)
            panel = app.query_one(PerformancePanel)
            assert panel.display
            async with asyncio.timeout(HANG_GUARD):
                while not app._performance.snapshot().sample_count:
                    await pilot.pause()
            assert app._performance.snapshot().sample_count > 0
            assert "PERFORMANCE" in panel.render().plain.upper()
            app.command("/perf hide")
            assert not panel.display
            assert app._performance.enabled
            app.command("/perf off global")
            assert not app._performance.enabled
            assert app._performance_timer is None
            assert (tmp_path / "settings/ngn/tui.json").is_file()
            app.command("/perf on local")
            assert panel.display and app._performance.enabled
            assert "saved local" in str(app.query_one("#status", Static).content)
            await pilot.resize_terminal(80, 24)
            app.command("/perf detail")
            assert isinstance(app.screen, DetailModal)
            app.pop_screen()
            await pilot.pause()
            # The local command must not be queued behind an active run.
            app._active = asyncio.create_task(asyncio.sleep(30))
            app.submit(Composer.Submitted("/perf off"))
            assert not app._queued_prompts
            assert not app._performance.enabled
            app._active.cancel()
            await asyncio.gather(app._active, return_exceptions=True)
        assert app._performance_timer is None

    asyncio.run(scenario())
