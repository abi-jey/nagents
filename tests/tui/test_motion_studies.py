"""Interactive design studies keep motion and scheduler state independent."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime

import pytest
from examples.tui_motion import DESIGNS
from examples.tui_motion import DemoState
from examples.tui_motion import MotionStudies
from examples.tui_motion import motion_frame
from rich.cells import cell_len
from textual.widgets import Static


def test_due_wakeup_waits_for_next_main_turn_and_delivers_once() -> None:
    state = DemoState(started=100, wall_started=datetime(2026, 10, 31, tzinfo=UTC))
    state.schedule(100)
    state.advance(119.1)
    assert str(state.wake) == "pending"
    assert state.wake_text(119.1) == "in 00:01"
    assert state.due_text() == "00:00:20 UTC"
    state.advance(120)
    assert str(state.wake) == "queued"
    assert str(state.main) == "thinking"
    state.main = "idle"
    state.advance(121)
    assert str(state.wake) == "delivered"
    assert str(state.main) == "thinking"
    assert state.main_since == 121
    assert len(state.events) == 3
    state.advance(122)
    assert len(state.events) == 3
    assert "+ 21.000s" in state.events[-1]


def test_motion_frames_have_constant_cell_width_and_pause_without_losing_state() -> None:
    for design in DESIGNS:
        frames = {motion_frame(design, tick / 8, "thinking") for tick in range(32)}
        assert len(frames) > 1
        for phase in ("thinking", "tool", "responding", "idle", "waiting", "done"):
            for tick in range(16):
                assert cell_len(motion_frame(design, tick / 8, phase)) == 7
                assert "\\" not in motion_frame(design, tick / 8, phase)
                assert motion_frame(design, tick, phase, animate=False) == motion_frame(design, 0, phase, animate=False)


@pytest.mark.parametrize("design", DESIGNS)
@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_demo_controls_and_provider_placement(design: str, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        selected = next(option for option in DESIGNS if option == design)
        app = MotionStudies(selected, theme="graphite", animations=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert not app.query_one("#splash").display
            model = app.query_one("#provider-model", Static)
            assert model.region.y >= app.query_one("#message").region.bottom
            assert model.region.bottom <= size[1]
            assert "openai · gpt-6.1-sol" in str(model.content)
            await pilot.press("2", "s", "d")
            await pilot.pause()
            assert app.design == "orbit"
            assert app.state.child == "waiting"
            assert str(app.state.wake) == "queued"
            assert "queued" in str(app.query_one("#wake-detail", Static).content)
            await pilot.press("m", "m", "m")
            await pilot.pause()
            assert str(app.state.wake) == "delivered"
            await pilot.press("l")
            assert app.query_one("#events").display
            await pilot.press("x")
            assert str(app.state.wake) == "off"
            await pilot.press("q")

    asyncio.run(scenario())
