"""Lifecycle and keyboard checks for the offline transition study."""

import pytest
from examples.tui_tool_transitions import FIXTURES
from examples.tui_tool_transitions import LiveRow
from examples.tui_tool_transitions import TransitionStudy
from examples.tui_tool_transitions import clock
from examples.tui_tool_transitions import state_at
from rich.syntax import Syntax
from rich.text import Text


@pytest.mark.parametrize(
    ("seconds", "state"), [(0, "writing"), (2.5, "queued"), (4, "waiting"), (6, "running"), (10, "complete")]
)
def test_phases(seconds: float, state: str) -> None:
    assert state_at(seconds)[0] == state


@pytest.mark.asyncio
async def test_hover_options_switch_with_keys_and_redraw() -> None:
    from examples.tui_tool_transitions import HOVER_OPTIONS

    assert HOVER_OPTIONS == ("Subtle background tint", "Bold header", "Tint + bold name")
    app = TransitionStudy()
    assert app.hover_option == 0
    async with app.run_test(size=(120, 60)) as pilot:
        app.elapsed = 26
        app.draw_frame()
        app.set_focus(None)
        await pilot.pause()
        rows = list(app.query(LiveRow))
        assert all(row.hover_option == 0 for row in rows)
        row = rows[0]
        await pilot.hover(row.query_one("CollapsibleTitle"))
        await pilot.pause()
        before = Text.from_markup(row.title)
        preview = row.preview.content
        regions = [item.region for item in rows]
        for key, option in (("2", 1), ("3", 2), ("1", 0)):
            # Do not move the pointer: switching must update the native CSS too.
            await pilot.press(key)
            await pilot.pause()
            assert app.playing and app.elapsed == 26
            assert app.hover_option == option
            assert list(app.query(LiveRow)) == rows
            assert all(item.hover_option == option for item in rows)
            assert all(item.has_class("tint-hover") == (option in (0, 2)) for item in rows)
            assert all(item.has_class("bold-hover") == (option == 1) for item in rows)
            assert all(item.has_class("name-bold-hover") == (option == 2) for item in rows)
            title = row.query_one("CollapsibleTitle")
            assert title.is_mouse_over
            assert (title.styles.background.a > 0) == (option in (0, 2))
            assert bool(title.styles.text_style.bold) == (option == 1)
            text = Text.from_markup(row.title)
            assert text.plain == before.plain
            assert text.plain.startswith("✓  " + row.fixture.name + "   " + row.fixture.target)
            assert "[" not in text.plain and "]" not in text.plain
            assert bool(text.get_style_at_offset(app.console, 3).bold) == (option == 2)
            assert not title.styles.text_style.reverse
            assert row.preview.content is preview
            assert [item.region for item in rows] == regions


@pytest.mark.asyncio
async def test_reduced_motion_keeps_timer_but_stops_indicator() -> None:
    app = TransitionStudy()
    async with app.run_test(size=(100, 40)) as pilot:
        app.playing = False
        await pilot.press("m")
        assert app.reduced_motion
        row = app.query_one(LiveRow)
        titles = []
        for seconds in (6.2, 6.5):
            app.elapsed = seconds
            app.draw_frame()
            titles.append(Text.from_markup(row.title).plain)
        assert titles[0][0] == titles[1][0] == "◌"
        assert titles[0][-7:] != titles[1][-7:]


@pytest.mark.asyncio
async def test_specular_shimmer_changes_only_decorations_and_settles() -> None:
    from rich.console import Console

    app = TransitionStudy()
    async with app.run_test(size=(120, 40)) as pilot:
        app.playing = False
        row = app.query_one(LiveRow)
        await pilot.pause()
        console = Console()
        name_styles = []
        for frame, seconds in enumerate((6.1, 6.8, 8.6)):
            row.update_frame(seconds, frame)
            assert row.current_frame == (seconds, frame)
            sample = Text.from_markup(row.title)
            assert row.collapsed and row.size.height == 1
            assert sample.cell_len == row.content_size.width - 2
            assert "read_file" in sample.plain
            assert not any(glyph in sample.plain for glyph in "•●▏")
            assert "underline" not in sample.markup
            name_styles.append(
                tuple(sample.get_style_at_offset(console, index) for index in range(3, 3 + len(row.fixture.name)))
            )
        assert len(set(name_styles)) > 1
        row.update_frame(11, 0)
        settled = row.title
        row.update_frame(20, 100)
        assert row.title == settled


@pytest.mark.asyncio
async def test_targets_and_arguments_keep_their_color_across_states() -> None:
    from rich.console import Console

    app = TransitionStudy()
    async with app.run_test(size=(120, 40)) as pilot:
        app.playing = False
        await pilot.pause()
        row = app.query_one(LiveRow)
        console = Console()
        styles = []
        for seconds in (0.8, 2.6, 4.5, 6.5, 11):
            row.update_frame(seconds, 0)
            text = Text.from_markup(row.title)
            index = text.plain.index(row.fixture.target)
            styles.append(text.get_style_at_offset(console, index))
            assert row.preview.styles.opacity == 0.7
            if seconds < 5:
                assert isinstance(row.preview.content, Text)
                assert row.preview.content.style == "dim"
        assert len(set(styles)) == 1
        assert styles[0].color is None
        row.update_frame(6.8, 0)
        text = Text.from_markup(row.title)
        colors = [text.get_style_at_offset(console, i).color for i in range(3, 3 + len(row.fixture.name))]
        assert any(color is not None and color.triplet is not None and len(set(color.triplet)) > 1 for color in colors)


def test_terminal_clock_freezes() -> None:
    assert state_at(10) == state_at(100) == ("complete", 4.0)
    assert state_at(100, True) == ("failed", 4.0)
    assert clock(65.3) == "01:05.3"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [48, 120])
async def test_live_rows_keep_identity_and_format_outputs(width: int, monkeypatch: pytest.MonkeyPatch) -> None:
    app = TransitionStudy()
    # This test drives every frame explicitly, including restart at zero.
    monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: None)
    async with app.run_test(size=(width, 36)) as pilot:
        app.playing = False
        row = app.query_one(LiveRow)
        for seconds in (0, 2.5, 4, 6, 10):
            app.elapsed = seconds
            app.draw_frame()
            assert app.query_one(LiveRow) is row
            assert state_at(seconds)[0] in Text.from_markup(row.title).plain
        await pilot.pause()
        app.draw_frame()
        plain = Text.from_markup(row.title)
        assert plain.cell_len == row.content_size.width - 2
        assert plain.plain.endswith("00:04.0")
        assert row.trail.display
        assert row.size.height <= 9
        await pilot.press("a")
        assert len(app.query(LiveRow)) == len(FIXTURES)
        assert sum(item.display for item in app.query(LiveRow)) == 1
        app.elapsed = 4.5
        app.draw_frame()
        assert sum(item.display for item in app.query(LiveRow)) == 3
        app.elapsed = 11
        app.draw_frame()
        assert "complete" in Text.from_markup(app.query_one(LiveRow).title).plain
        assert any("running" in Text.from_markup(item.title).plain for item in app.query(LiveRow) if item.display)
        app.elapsed = 26
        app.draw_frame()
        for item in app.query(LiveRow):
            assert isinstance(item.preview.content, Syntax)
        assert "failed" in Text.from_markup(list(app.query(LiveRow))[-1].title).plain
        assert all(item.collapsed for item in app.query(LiveRow))
        await pilot.press("e")
        assert all(not item.collapsed for item in app.query(LiveRow))
        await pilot.press("e")
        assert all(item.collapsed for item in app.query(LiveRow))
        await pilot.press("n")
        assert len(app.query(LiveRow)) == len(FIXTURES)
        assert app.parallel
        await pilot.press("r")
        assert app.elapsed < 1
        assert app.playing


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [48, 120])
async def test_rows_do_not_reflow_on_state_changes(width: int) -> None:
    app = TransitionStudy()
    async with app.run_test(size=(width, 60)) as pilot:
        app.playing = False
        sizes: list[int] = []
        prefixes: list[str] = []
        for seconds in (0.5, 2.6, 4.5, 6.5, 10.5):
            app.elapsed = seconds
            app.draw_frame()
            await pilot.pause()
            app.draw_frame()
            row = app.query_one(LiveRow)
            sizes.append(row.size.height)
            text = Text.from_markup(row.title).plain
            prefixes.append(text[2:-24].rstrip())
            assert text[1] == " "
            assert Text.from_markup(row.title).cell_len == row.content_size.width - 2
        assert len(set(sizes)) == 1
        assert len(set(prefixes)) == 1
