"""Continuous state transitions and native-background layout for the TUI study."""

from __future__ import annotations

import asyncio
from math import isclose
from time import monotonic

import pytest
from examples.tui_state_field import StateFieldDemo
from rich.cells import cell_len
from textual.widgets import Button

from nagents.tui.activity import STATES
from nagents.tui.activity import StateField


def test_rapid_retarget_keeps_channel_position_velocity_and_particle_phase() -> None:
    field = StateField("working", offset=0.37)
    field.advance(0.24)
    phase = field.phase
    values = tuple(spring.value for spring in field.springs)
    velocities = tuple(spring.velocity for spring in field.springs)
    old_targets = tuple(spring.target for spring in field.springs)

    field.select("delegating")

    assert field.phase == phase
    assert tuple(spring.value for spring in field.springs) == values
    assert tuple(spring.velocity for spring in field.springs) == velocities
    assert tuple(spring.target for spring in field.springs) != old_targets

    field.advance(0.11)
    phase = field.phase
    values = tuple(spring.value for spring in field.springs)
    velocities = tuple(spring.velocity for spring in field.springs)
    field.select("returned")

    assert field.phase == phase
    assert tuple(spring.value for spring in field.springs) == values
    assert tuple(spring.velocity for spring in field.springs) == velocities


def test_spring_color_and_phase_integration_is_frame_rate_independent() -> None:
    single_step = StateField("working", offset=1.25)
    thirty_fps = StateField("working", offset=1.25)
    one_twenty_fps = StateField("working", offset=1.25)
    for field in (single_step, thirty_fps, one_twenty_fps):
        field.select("returned")

    single_step.advance(1.0)
    for _ in range(30):
        thirty_fps.advance(1 / 30)
    for _ in range(120):
        one_twenty_fps.advance(1 / 120)

    for stepped in (thirty_fps, one_twenty_fps):
        assert isclose(single_step.phase, stepped.phase, rel_tol=1e-10, abs_tol=1e-10)
        assert single_step.stops == stepped.stops
        for expected, actual in zip(single_step.springs, stepped.springs, strict=True):
            assert isclose(expected.value, actual.value, rel_tol=1e-10, abs_tol=1e-10)
            assert isclose(expected.velocity, actual.velocity, rel_tol=1e-10, abs_tol=1e-10)


def test_braille_canvas_dimensions_are_fixed_across_all_states() -> None:
    for state in STATES:
        field = StateField("starting", offset=0.7)
        field.select(state, immediate=True)
        rows = field.render(width=16, height=3).plain.splitlines()
        assert len(rows) == 3
        assert all(cell_len(row) == 16 for row in rows)
        assert all(0x2800 <= ord(character) <= 0x28FF for row in rows for character in row)


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_native_background_footer_and_state_controls(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            for selector in (
                "#controls",
                "#conversation",
                "#message",
                "#main-row",
                "#main-detail",
                "#child-row",
                "#provider-model",
                "#help",
            ):
                assert app.query_one(selector).styles.background.ansi == -1, selector
            assert app.screen.styles.background.ansi == -1
            assert all(button.styles.background.ansi == -1 for button in app.query(Button))

            message = app.query_one("#message")
            model = app.query_one("#provider-model")
            assert model.region.y >= message.region.bottom
            assert model.region.bottom <= size[1]

            direct_states = (
                ("0", "starting"),
                ("1", "working"),
                ("2", "tool"),
                ("3", "delegating"),
                ("4", "delegated"),
                ("5", "returned"),
                ("6", "approval"),
                ("7", "sleeping"),
                ("8", "waking"),
                ("9", "responding"),
                ("i", "idle"),
                ("c", "done"),
                ("x", "error"),
            )
            for key, state in direct_states:
                await pilot.press(key)
                assert app.main.state == state

            await pilot.press("s")
            assert app.child.state == "idle"  # The preceding returned state settled the child at done.
            gradient = app.gradient
            await pilot.press("g")
            assert app.gradient is not gradient
            await pilot.press("w")
            assert app.main.state == "sleeping" and app.wake_active
            app.wake_due = monotonic() - 0.1
            app._tick()
            assert str(app.main.state) == "waking" and not app.wake_active
            await pilot.press("1")
            assert app.main.state == "working"
            await pilot.press("d")
            assert str(app.main.state) == "waking" and not app.wake_active

            await pilot.press("p")
            assert app.playing and app.main.state == "working"
            app.sequence_since = monotonic() - 25.1
            app._tick()
            assert app.main.state == "waking" and app.child.state == "done"
            app.sequence_since = monotonic() - 34.1
            app._tick()
            assert not app.playing and app.main.state == "idle" and app.child.state == "done"

            await pilot.press("l")
            assert app.query_one("#timings").display
            await pilot.press("space")
            assert app.motion_enabled
            await pilot.press("space")
            assert not app.motion_enabled
            await pilot.press("q")
            await pilot.pause(0.05)
            assert not app.is_running

    asyncio.run(scenario())
