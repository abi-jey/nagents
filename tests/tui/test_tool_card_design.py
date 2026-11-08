"""Approved ToolCard design exercised on production widgets and configured themes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from typing import Literal

import pytest
from rich.cells import cell_len
from textual.app import App
from textual.content import Content
from textual.geometry import Size
from textual.widgets import Static

from nagents.harness.config import THEME_NAMES
from nagents.tui import themes
from nagents.tui import widgets
from nagents.tui.widgets import ToolCard

if TYPE_CHECKING:
    from textual.widget import Widget


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,level", [(True, "full"), (False, "full"), (True, "none")])
async def test_shimmer_covers_entire_header_and_respects_motion(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, level: Literal["none", "full"]
) -> None:
    app = design_app("terminal")
    app.animation_level = level
    card = ToolCard("shimmer", "mcp.catalog.lookup.with.long.name", {"path": "target.py"}, animations=enabled)
    async with app.run_test(size=(120, 30)) as pilot:
        await app.mount(card)
        await pilot.pause()
        card._stop_elapsed()
        samples = []
        rendered = []
        for age in (0.5, 0.9):
            monkeypatch.setattr(widgets, "monotonic", lambda age=age: card._started_at + age)
            card._update_summary()
            samples.append(card._summary_text.copy())
            label = card._title.label
            content = card._title.content
            assert isinstance(label, Content)
            assert isinstance(content, Content)
            rendered.append(tuple(label.spans))
            assert content.plain.endswith(card.title)
        assert samples[0].plain == samples[1].plain
        animated = enabled and level != "none"
        assert bool(samples[0].spans) == animated
        assert (samples[0].spans != samples[1].spans) == animated
        assert bool(rendered[0]) == animated
        assert (rendered[0] != rendered[1]) == animated
        if animated:
            for text in samples:
                assert {span.start for span in text.spans} == set(range(len(text)))
                assert all(span.end == span.start + 1 for span in text.spans)
            assert "target.py" in samples[0].plain
            assert "running" in samples[0].plain
            assert card._duration in samples[0].plain
        card.finish({}, None, 1000)
        assert not card._summary_text.spans
        assert card._elapsed_timer is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,level", [(True, "full"), (False, "full"), (True, "none")])
async def test_streamed_preview_shimmers_between_argument_updates(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, level: Literal["none", "full"]
) -> None:
    app = design_app("terminal")
    app.animation_level = level
    card = ToolCard("preview", "shell", preview=True, animations=enabled)
    async with app.run_test(size=(120, 30)) as pilot:
        await app.mount(card)
        await pilot.pause()
        timer = card._elapsed_timer
        assert timer is not None
        started = card._started_at
        for ready in (False, True):
            card.set_preview("shell", '{"command": "echo', ready=ready)
            assert card._elapsed_timer is timer
            assert card._started_at == started
            frames = []
            for age in (0.5, 0.9):
                monkeypatch.setattr(widgets, "monotonic", lambda age=age: started + age)
                card._tick_elapsed()
                label = card._title.label
                assert not isinstance(label, str)
                frames.append(tuple(label.spans))
            assert (frames[0] != frames[1]) == (enabled and level != "none")
            assert card._duration == ""
            assert card._outcome in card.title
            assert "0.00s" not in card.title
        card.start_call("running", "shell", {"command": "echo hello"})
        assert card._duration == "0.00s"
        assert card._elapsed_timer is not None
        assert card._elapsed_timer is not timer
        card.cancel()
        assert card._elapsed_timer is None
        assert not card._summary_text.spans


CSS_PATH = Path(themes.__file__).with_name("app.tcss")
OUTCOMES = ("running", "complete", "failed", "cancelled", "exit 7", "timed out")


def design_app(theme: str) -> App[None]:
    app = App[None](css_path=CSS_PATH)
    themes.configure_theme(app, theme)
    return app


def settle(card: ToolCard, outcome: str) -> None:
    if outcome == "cancelled":
        card.cancel()
    elif outcome == "exit 7":
        card.finish({"exit_code": 7}, None, 1250)
    elif outcome == "timed out":
        card.finish({"timed_out": True}, None, 1250)
    elif outcome != "running":
        card.finish({"output": "Result"}, "Failure details" if outcome == "failed" else None, 1250)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [80, 160])
@pytest.mark.parametrize("outcome", OUTCOMES)
async def test_tool_columns_align_with_and_without_timeout(width: int, outcome: str) -> None:
    app = design_app("terminal")
    cards = [
        ToolCard("plain", "read_file", {"path": "src/example.py"}, animations=False),
        ToolCard("short", "shell", {"command": "echo hello", "timeout": 60}, animations=False),
        ToolCard("long", "shell", {"command": "echo hello", "timeout": 600}, animations=False),
    ]
    async with app.run_test(size=(width, 24)) as pilot:
        await app.mount(*cards)
        await pilot.pause()
        for card in cards:
            card._stop_elapsed()
            settle(card, outcome)
            card._duration = "1.25s"
            card._update_summary()
        assert len({cell_len(card.title.split(card._outcome)[0]) for card in cards}) == 1
        assert len({cell_len(card.title.split("1.25s")[0]) for card in cards}) == 1
        assert len({cell_len(card.title) for card in cards}) == 1
        assert " / " not in cards[0].title
        assert " / 60s" in cards[1].title
        assert " / 600s" in cards[2].title


def assert_transparent(widget: Widget) -> None:
    assert widget.styles.background.a == 0
    assert widget.styles.background_tint.a == 0


def assert_unframed(card: ToolCard) -> None:
    for widget in (card, card.query_one("CollapsibleTitle")):
        for side in ("border_top", "border_bottom", "border_left", "border_right"):
            assert getattr(widget.styles, side)[0] in {"", "none"}
        assert_transparent(widget)


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", THEME_NAMES)
@pytest.mark.parametrize("outcome", OUTCOMES)
async def test_native_hover_and_focus_keep_closed_rows_stationary(theme: str, outcome: str) -> None:
    app = design_app(theme)
    card = ToolCard("design", "shell", {"command": "printf '[red]literal[/red]'", "timeout": 30})
    neighbour = ToolCard("neighbour", "read_file", {"path": "src/example.py"})
    async with app.run_test(size=(100, 32)) as pilot:
        await app.mount(Static("Move pointer here", id="away"), card, neighbour)
        settle(card, outcome)
        app.set_focus(None)
        await pilot.hover("#away")
        await pilot.pause()
        title = card.query_one("CollapsibleTitle", Static)
        regions = (card.region, title.region, neighbour.region)
        foreground = title.styles.color
        plain = card.title
        assert outcome in plain
        assert card.collapsed and card.size.height == title.size.height == 1
        assert card.region.bottom == neighbour.region.y
        assert not title.styles.text_style.bold
        assert not title.styles.text_style.underline
        assert_unframed(card)

        await pilot.hover(title)
        await pilot.pause()
        assert title.styles.text_style.bold  # Native CSS, not rewritten title markup.
        assert not title.styles.text_style.reverse
        assert not title.styles.text_style.underline
        assert title.styles.color == foreground
        assert_unframed(card)
        assert (card.region, title.region, neighbour.region) == regions
        # A running timer may advance; the target and status must not change.
        assert card.title.split(outcome)[0] == plain.split(outcome)[0]

        title.focus()
        await pilot.pause()
        assert title.styles.text_style.bold and title.styles.text_style.underline
        assert_unframed(card)
        await pilot.hover("#away")
        await pilot.pause()
        assert not title.styles.text_style.bold
        assert title.styles.text_style.underline
        assert_unframed(card)
        app.set_focus(None)
        await pilot.pause()
        assert not title.styles.text_style.bold
        assert not title.styles.text_style.underline
        assert (card.region, title.region, neighbour.region) == regions
        assert card.collapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", THEME_NAMES)
@pytest.mark.parametrize("outcome", OUTCOMES)
async def test_click_and_enter_expand_real_tool_contents(theme: str, outcome: str) -> None:
    app = design_app(theme)
    card = ToolCard("inspect", "shell", {"command": "printf result"})
    async with app.run_test(size=(80, 40)) as pilot:
        await app.mount(card)
        card.append_output("Streamed evidence\n")
        settle(card, outcome)
        await pilot.pause()
        title = card.query_one("CollapsibleTitle", Static)
        header = title.region
        assert card.collapsed and card.size.height == 1
        assert card.output.region.height == 0
        await pilot.click(title)
        await pilot.pause()
        assert not card.collapsed
        assert app.focused is title
        assert card.output.region.height > 0
        assert card.arguments.region.height > 0
        assert card.size.height > 1
        assert title.region == header
        assert "Streamed evidence" in card.output_text
        # Pilot.click dispatches clicks but does not establish mouse hover.
        await pilot.hover(title)
        await pilot.pause()
        assert title.styles.text_style.bold and title.styles.text_style.underline
        assert_unframed(card)
        await pilot.press("enter")
        await pilot.pause()
        assert card.collapsed and card.size.height == 1
        assert title.region == header
        assert card.output.region.height == 0
        await pilot.press("enter")
        await pilot.pause()
        assert not card.collapsed and card.output.region.height > 0
        assert title.region == header
        assert_unframed(card)


@pytest.mark.parametrize("width", [48, 100, 60])
@pytest.mark.parametrize("outcome", ["running", "complete", "cancelled"])
def test_resize_allocation_wins_over_stale_compositor_region(width: int, outcome: str) -> None:
    card = ToolCard("resize", "shell", {"command": "工具/" * 60, "timeout": 30})
    # An unmounted card has no compositor region, reproducing the stale/empty
    # region during allocation, before the queued Resize message is delivered.
    assert card.content_size.width == 0
    for allocated in (120, width):
        size = Size(allocated, 1)
        card._size_updated(size, size, size, layout=False)
        assert cell_len(card.title) == allocated - card.styles.gutter.width - 4
    if outcome == "complete":
        card.finish({}, None, 1250)
    elif outcome == "cancelled":
        card.cancel()
    else:
        card._tick_elapsed()
    # Later timer/result updates must not restore the 72-column fallback.
    assert cell_len(card.title) == width - card.styles.gutter.width - 4
    assert outcome in card.title


def right_columns(card: ToolCard, status: str, timing: str) -> tuple[int, int]:
    """Measure terminal cells, not string offsets (targets may contain wide glyphs)."""
    assert card.title.rstrip().endswith(timing)
    status_index = card.title.rindex(status)
    timing_index = card.title.rindex(timing)
    title = card.query_one("CollapsibleTitle")
    # Collapsible's native disclosure prefix occupies two content cells.
    assert cell_len(card.title) + 2 == title.content_size.width
    return cell_len(card.title[:status_index]), cell_len(card.title[:timing_index]) + cell_len(timing)


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", THEME_NAMES)
@pytest.mark.parametrize("width", [48, 100])
@pytest.mark.parametrize("outcome", OUTCOMES[1:])
async def test_status_and_timer_columns_survive_phases_and_resize(
    theme: str, width: int, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr("nagents.tui.widgets.monotonic", lambda: clock[0])
    app = design_app(theme)
    card = ToolCard("columns", "shell", {"command": "工具/" * 60, "timeout": 30})
    neighbour = ToolCard("next", "read_file")
    async with app.run_test(size=(width, 40)) as pilot:
        await app.mount(card, neighbour)
        app.set_focus(None)
        await pilot.pause()
        regions = (card.region, neighbour.region)
        initial = right_columns(card, "running", "0.00s / 30s")
        clock[0] = 112.25
        card._tick_elapsed()
        await pilot.pause()
        assert right_columns(card, "running", "12.25s / 30s") == initial
        card.set_arguments({"command": "Short target", "timeout": 30})
        card.append_output("Output must not expand a closed card\n" * 3)
        await pilot.pause()
        assert right_columns(card, "running", "12.25s / 30s") == initial
        settle(card, outcome)
        await pilot.pause()
        timing = "12.25s / 30s" if outcome == "cancelled" else "1.25s / 30s"
        assert right_columns(card, outcome, timing) == initial
        assert (card.region, neighbour.region) == regions
        assert card.collapsed and card.size.height == 1
        assert_unframed(card)
        frozen = card.title
        clock[0] = 200.0
        await pilot.pause(0.15)
        assert card.title == frozen
        assert card._elapsed_timer is None

        title = card.query_one("CollapsibleTitle", Static)
        title.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert not card.collapsed
        assert right_columns(card, outcome, timing) == initial
        await pilot.resize_terminal(width + 12, 40)
        await pilot.pause()
        resized = right_columns(card, outcome, timing)
        assert resized == (initial[0] + 12, initial[1] + 12)
        await pilot.press("enter")
        await pilot.pause()
        assert card.collapsed and card.size.height == 1
        assert right_columns(card, outcome, timing) == resized
        assert_unframed(card)
