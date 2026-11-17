"""Compact duration formatting and stable timer columns across unit changes."""

from __future__ import annotations

import pytest

from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import format_duration


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-1, "0ms"),
        (0, "0ms"),
        (0.042, "42ms"),
        (0.999, "999ms"),
        (0.9999, "1s"),
        (1, "1s"),
        (1.25, "1.25s"),
        (9.999, "10s"),
        (42.78, "42.8s"),
        (59.99, "1m"),
        (60, "1m"),
        (102.94, "1m 43s"),
        (600, "10m"),
        (3599.9, "1h"),
        (3723, "1h 2m"),
        (86399.9, "1d"),
        (183600, "2d 3h"),
        (86400 * 9999 + 3600 * 23, "9999d"),
        (86400 * 10000000, ">999999d"),
        (float("inf"), "unknown"),
    ],
)
def test_compact_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected
    assert len(expected) <= 8


def test_timer_fields_remain_fixed_across_units(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("nagents.tui.widgets.monotonic", lambda: clock[0])
    card = ToolCard("units", "shell", {"timeout": 600})
    card._allocated_width = 100
    columns = []
    for elapsed in (0.042, 1.25, 42.78, 102.94, 3723, 183600):
        clock[0] = 100 + elapsed
        card._tick_elapsed()
        assert card.title.endswith(f"{format_duration(elapsed):>8} / {'10m':>8}")
        columns.append((card.title.index("running"), card.title.index(" / "), len(card.title)))
    assert len(set(columns)) == 1
    card.finish({}, None, 102940)
    assert card.title.endswith("  1m 43s /      10m")
    assert card._elapsed_timer is None
