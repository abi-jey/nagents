"""Native header hover treatments preserve semantics, previews and geometry."""

import pytest
from examples import tui_tool_transitions
from examples.tui_tool_transitions import LiveRow
from examples.tui_tool_transitions import TransitionStudy
from rich.style import Style
from rich.text import Text
from textual import events


def assert_hover_style(row: LiveRow, option: int, *, hovered: bool) -> None:
    title = row.query_one("CollapsibleTitle")
    assert row.has_class("tint-hover") == (option in (0, 2))
    assert row.has_class("bold-hover") == (option == 1)
    assert row.has_class("name-bold-hover") == (option == 2)
    assert (title.styles.background.a > 0) == (hovered and option in (0, 2))
    if hovered and option in (0, 2):
        assert title.styles.background.rgb == (129, 157, 176)
        assert title.styles.background.a == pytest.approx(0.12)
    assert bool(title.styles.text_style.bold) == (hovered and option == 1)
    assert not title.styles.text_style.reverse
    assert not title.styles.text_style.underline
    text = Text.from_markup(row.title)
    for index in range(len(text.plain)):
        style = text.get_style_at_offset(row.app.console, index)
        assert bool(style.bold) == (option == 2 and row.hover_level > 0 and 3 <= index < 3 + len(row.fixture.name))
        assert style.bgcolor is None
        assert not style.reverse
        assert not style.underline


@pytest.mark.asyncio
@pytest.mark.parametrize("option", [0, 1, 2])
async def test_hover_first_frame_after_scenario_ends(option: int, monkeypatch: pytest.MonkeyPatch) -> None:
    app = TransitionStudy()
    async with app.run_test(size=(120, 60)) as pilot:
        app.elapsed = 26
        app.draw_frame()
        app.action_hover(option)
        app.set_focus(None)
        await pilot.pause()
        row = app.query_one(LiveRow)
        before = row.title
        frame = app.frame
        with monkeypatch.context() as patch:

            def unexpected_redraw() -> None:
                pytest.fail("A finished scenario must not redraw all rows")

            patch.setattr(app, "draw_frame", unexpected_redraw)
            app.tick()
        assert app.playing and app.elapsed == 26 and app.frame == frame
        row.on_enter(events.Enter(row))
        assert row.hover_level == 0.6
        assert row.hover_target == 1
        text = Text.from_markup(row.title)
        assert bool(text.get_style_at_offset(app.console, 3).bold) == (option == 2)
        assert text.plain == Text.from_markup(before).plain
        row.on_leave(events.Leave(row))
        assert row.hover_level == 0
        assert row.hover_target == 0
        assert row.title == before


@pytest.mark.asyncio
@pytest.mark.parametrize("option", [0, 1, 2])
@pytest.mark.parametrize("width", [48, 120])
async def test_pilot_hover_preserves_geometry_and_preview(option: int, width: int) -> None:
    app = TransitionStudy()
    async with app.run_test(size=(width, 60)) as pilot:
        app.elapsed = 26
        app.draw_frame()
        app.action_hover(option)
        app.set_focus(None)
        await pilot.pause()
        rows = list(app.query(LiveRow))
        assert all(row.collapsed and row.size.height == 1 for row in rows)
        row = rows[3]
        # Newly revealed rows need their allocated geometry before a snapshot.
        app.draw_frame()
        preview = row.preview.content
        before = Text.from_markup(row.title)
        regions = [item.region for item in rows]
        assert_hover_style(row, option, hovered=False)
        await pilot.hover(row.query_one("CollapsibleTitle"))
        await pilot.pause(0.3)
        assert row.hover_level > 0.9
        assert_hover_style(row, option, hovered=True)
        hovered = Text.from_markup(row.title)
        assert before.plain[:3] == "✓  "
        assert hovered.plain == before.plain
        assert hovered.cell_len == before.cell_len == row.content_size.width - 2
        assert "[" not in hovered.plain and "]" not in hovered.plain
        assert row.preview.content is preview
        assert row.preview.styles.opacity == 0.7
        assert [item.region for item in rows] == regions
        assert all(item.collapsed for item in rows)
        snapshot = row.title
        await pilot.pause(0.45)
        assert row.title != snapshot  # Existing name shimmer is independent of the option.
        assert_hover_style(row, option, hovered=True)
        assert app.playing and app.elapsed == 26
        await pilot.hover("#heading")
        await pilot.pause()
        assert row.hover_level == 0
        assert_hover_style(row, option, hovered=False)
        assert row.preview.content is preview
        assert [item.region for item in rows] == regions
        assert Text.from_markup(row.title).plain == before.plain
        await pilot.click(row.query_one("CollapsibleTitle"))
        await pilot.pause()
        assert not row.collapsed
        assert row.size.height > 1
        assert all(item.collapsed for item in rows if item is not row)
        app.elapsed = 27
        app.draw_frame()
        assert not row.collapsed
        await pilot.hover("#heading")
        row.query_one("CollapsibleTitle").focus()
        await pilot.pause()
        assert row.query_one("CollapsibleTitle").styles.text_style.underline
        await pilot.press("enter")
        await pilot.pause()
        assert row.collapsed
        assert row.size.height == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [0.0, 0.01, 0.5, 1.0])
async def test_name_bold_uses_positive_hover_level_without_full_line_changes(
    level: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = TransitionStudy()
    async with app.run_test(size=(120, 60)) as pilot:
        app.elapsed = 26
        app.draw_frame()
        await pilot.press("m")
        app.set_focus(None)
        await pilot.pause()
        row = app.query_one(LiveRow)
        monkeypatch.setattr(tui_tool_transitions, "monotonic", lambda: 100.0)
        before = Text.from_markup(row.title)
        for option in range(3):
            app.action_hover(option)
            row.hover_level = level
            row.update_frame(*row.current_frame)
            text = Text.from_markup(row.title)
            assert text.plain == before.plain
            for index in range(len(text.plain)):
                style = text.get_style_at_offset(app.console, index)
                baseline = before.get_style_at_offset(app.console, index)
                name_bold = option == 2 and level > 0 and 3 <= index < 3 + len(row.fixture.name)
                assert style == (baseline + Style(bold=True) if name_bold else baseline)


@pytest.mark.asyncio
@pytest.mark.parametrize("option", [0, 1, 2])
async def test_reduced_motion_hover_is_static_and_preserves_failure(option: int) -> None:
    app = TransitionStudy()
    async with app.run_test(size=(120, 60)) as pilot:
        app.elapsed = 26
        app.draw_frame()
        app.action_hover(option)
        await pilot.press("m")
        app.set_focus(None)
        await pilot.pause()
        row = list(app.query(LiveRow))[-1]
        before = row.title
        preview = row.preview.content
        regions = [item.region for item in app.query(LiveRow)]
        await pilot.hover(row.query_one("CollapsibleTitle"))
        await pilot.pause()
        assert_hover_style(row, option, hovered=True)
        hovered = Text.from_markup(row.title)
        assert hovered.plain[:3] == "✕  "
        assert "failed" in hovered.plain
        assert row.has_class("failed")
        failure = hovered.plain.rindex("failed")
        for index in (0, failure):
            color = hovered.get_style_at_offset(app.console, index).color
            assert color is not None
            red, green, blue = color.get_truecolor()
            assert red > green and red > blue
        snapshot = row.title
        await pilot.pause(0.45)
        assert row.title == snapshot
        assert app.playing and app.elapsed == 26
        assert row.preview.content is preview
        assert row.preview.styles.opacity == 0.7
        assert row.collapsed and row.size.height == 1
        assert [item.region for item in app.query(LiveRow)] == regions
        await pilot.hover("#heading")
        await pilot.pause()
        assert row.hover_level == 0
        assert_hover_style(row, option, hovered=False)
        assert row.title == before
