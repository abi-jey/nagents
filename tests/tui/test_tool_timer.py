"""Tool durations occupy stable columns at the right of each header."""

from __future__ import annotations

import asyncio

import pytest
from rich.text import Text
from textual.app import App

from nagents.tui import themes
from nagents.tui.widgets import ToolCard


@pytest.mark.parametrize("outcome", ["complete", "failed", "cancelled", "unmounted"])
def test_live_timer_updates_and_stops(outcome: str) -> None:
    async def scenario() -> None:
        app = App[None](css_path=themes.__file__.replace("themes.py", "app.tcss"))
        themes.configure_theme(app, "terminal")
        card = ToolCard("live", "shell", {"command": "check", "timeout": 30}, default_timeout=600)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.mount(card)
            async with asyncio.timeout(10):
                while card._duration in {"", "0ms", "0.00s"}:
                    await pilot.pause()
            first_duration = card._duration
            async with asyncio.timeout(10):
                while card._duration == first_duration:
                    await pilot.pause()
            assert card._duration != first_duration
            assert card.title.endswith(f"{card._duration:>8} / {'30s':>8}")
            assert "running" in card.title
            outcome_column = Text(card.title[: card.title.index("running")]).cell_len
            timeout_column = Text(card.title[: card.title.index(" / ")]).cell_len
            assert Text(card.title).cell_len + 2 == card._title.content_size.width
            card.set_arguments({"timeout": 0})
            assert card.title.endswith(f"{card._duration:>8} / {'10m':>8}")
            assert Text(card.title[: card.title.index("running")]).cell_len == outcome_column
            assert Text(card.title[: card.title.index(" / ")]).cell_len == timeout_column
            card.set_arguments({"timeout_ms": 1500})
            assert card.title.endswith(f"{card._duration:>8} / {'1.5s':>8}")
            assert Text(card.title[: card.title.index(" / ")]).cell_len == timeout_column
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            assert Text(card.title).cell_len + 2 == card._title.content_size.width
            if outcome == "unmounted":
                await card.remove()
            elif outcome == "cancelled":
                card.cancel()
            else:
                card.finish({}, "failed" if outcome == "failed" else None, 1234)
                assert card._duration == "1.23s"
                assert card.title.endswith(f"{'1.23s':>8} / {'1.5s':>8}")
            frozen = card.title
            await pilot.pause(0.15)
            assert card.title == frozen
            assert card._elapsed_timer is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "duration_ms,expected",
    [
        (0, ""),
        (0.4, "0ms"),
        (1.6, "2ms"),
        (999.4, "999ms"),
        (999.5, "1s"),
        (1000, "1s"),
        (1250, "1.25s"),
        (1500, "1.5s"),
        (9990, "9.99s"),
        (10000, "10s"),
        (12340, "12.3s"),
        (59900, "59.9s"),
        (60000, "1m"),
        (61400, "1m 1s"),
        (61600, "1m 2s"),
        (3599600, "1h"),
        (3661000, "1h 1m"),
        (86399600, "1d"),
        (90061000, "1d 1h"),
    ],
)
def test_finished_duration_is_compact_and_unpadded(duration_ms: float, expected: str) -> None:
    card = ToolCard("compact", "custom", {})
    card.finish({}, None, duration_ms)
    assert card._duration == expected
    assert card.title.endswith(f"{expected:>8}" + " " * 11)


@pytest.mark.parametrize("value", [None, False, -1, "30", float("inf"), float("nan")])
def test_invalid_timeouts_are_not_displayed(value: object) -> None:
    card = ToolCard("invalid", "custom", {"timeout": value})
    assert card._timeout == 0


@pytest.mark.parametrize("width", [40, 80, 120])
@pytest.mark.parametrize("tool", ["read_file", "工具" * 20])
def test_timer_tracks_header_right_edge(width: int, tool: str) -> None:
    async def scenario() -> None:
        app = App[None](css_path=themes.__file__.replace("themes.py", "app.tcss"))
        themes.configure_theme(app, "terminal")
        card = ToolCard("timer", tool, {"path": "long/path/" * 20})
        async with app.run_test(size=(width, 24)) as pilot:
            await app.mount(card)
            await pilot.pause()
            card.finish({}, None, 1234)
            await pilot.pause()
            timing = f"{'1.23s':>8}" + " " * 11
            assert len(timing) == 19
            assert card._duration == "1.23s"
            assert card.title.endswith(timing)
            assert "complete" in card.title
            assert Text(card.title).cell_len + 2 == card._title.content_size.width
            assert card._title.size.height == 1
            await pilot.resize_terminal(width + 10, 24)
            await pilot.pause()
            assert Text(card.title).cell_len + 2 == card._title.content_size.width
            card.collapsed = False
            await pilot.pause()
            assert card.title.endswith(timing)
            assert Text(card.title).cell_len + 2 == card._title.content_size.width

    asyncio.run(scenario())
