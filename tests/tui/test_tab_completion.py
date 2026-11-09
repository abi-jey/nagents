"""Tab completes slash commands without leaving the composer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.tui.widgets import Composer
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


def test_tab_completes_only_open_slash_menu(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            await pilot.press("/", "h", "e", "tab")
            assert composer.text == "/help "
            assert app.focused is composer
            await pilot.press("tab", "shift+tab")
            assert composer.text == "/help "
            assert app.focused is composer
            composer.load_text("ordinary text")
            await pilot.pause()
            await pilot.press("tab", "shift+tab")
            assert composer.text == "ordinary text"
            assert app.focused is composer

    asyncio.run(scenario())
