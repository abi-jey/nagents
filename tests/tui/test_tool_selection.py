"""Tool headers allow drag selection without losing disclosure controls."""

import pytest
from textual.app import App
from textual.widgets import Static

from nagents.tui import themes
from nagents.tui.widgets import SelectableToolTitle
from nagents.tui.widgets import ToolCard


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_drag_select_header_then_click_and_enter(running: bool) -> None:
    app = App[None](css_path=themes.__file__.replace("themes.py", "app.tcss"))
    themes.configure_theme(app, "terminal")
    card = ToolCard("selection", "read_file", {"path": "source.py"})
    async with app.run_test(size=(100, 30)) as pilot:
        await app.mount(Static("Away", id="away"), card)
        if not running:
            card.finish("Selected output", None, 1000)
        await pilot.pause()
        header = card.query_one(SelectableToolTitle)
        assert header.allow_select
        await pilot.mouse_down(header, offset=(3, 0))
        await pilot.hover(header, offset=(12, 0))
        await pilot.mouse_up(header, offset=(12, 0))
        await pilot.pause()
        assert "read_file" in (app.screen.get_selected_text() or "")
        assert card.collapsed
        await pilot.pause(0.15)
        assert "read_file" in (app.screen.get_selected_text() or "")
        assert card.collapsed
        await pilot.click(header, offset=(1, 0))
        await pilot.pause()
        assert not card.collapsed
        assert not app.screen.get_selected_text()
        header.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert card.collapsed
