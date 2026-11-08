"""Responsive production headers at extremely small terminal widths."""

import pytest
from textual.app import App

from nagents.tui import themes
from nagents.tui import widgets
from nagents.tui.terminal_colors import TerminalColors
from nagents.tui.widgets import ToolCard


def test_joined_emoji_survives_header_sanitization() -> None:
    assert ToolCard._plain_line("👩‍💻 é\nname") == "👩‍💻 é name"


@pytest.mark.asyncio
async def test_light_native_terminal_shimmer_uses_dark_ink(monkeypatch: pytest.MonkeyPatch) -> None:
    app = App[None](css_path=themes.__file__.replace("themes.py", "app.tcss"))
    themes.configure_theme(app, "terminal", terminal=TerminalColors("#202020", "#ffffff"))
    app.animation_level = "full"
    card = ToolCard("light", "read_file", {"path": "source.py"})
    async with app.run_test(size=(100, 24)) as pilot:
        await app.mount(card)
        await pilot.pause()
        card._stop_elapsed()
        monkeypatch.setattr(widgets, "monotonic", lambda: card._started_at + 0.8)
        card._update_summary()
        assert card._summary_text.spans
        for index in range(len(card.tool)):
            color = card._summary_text.get_style_at_offset(app.console, index).color
            assert color is not None
            assert max(color.get_truecolor()) < 150


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [12, 20, 28])
async def test_tiny_headers_preserve_failed_outcome(width: int) -> None:
    app = App[None](css_path=themes.__file__.replace("themes.py", "app.tcss"))
    themes.configure_theme(app, "terminal")
    card = ToolCard("tiny", "shell", {"timeout": 30})
    async with app.run_test(size=(width, 24)) as pilot:
        await app.mount(card)
        await pilot.pause()
        card.finish({}, "failure", 1250)
        await pilot.pause()
        assert "failed" in card.title
        assert len(card.title) == card._title.content_size.width - 2
        assert card._title.size.height == 1
        if width >= 20:
            assert card.title.endswith("1.25s")
