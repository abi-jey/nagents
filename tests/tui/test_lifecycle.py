"""Foreground lifecycle transitions are interruptible without changing layout or motion state."""

from __future__ import annotations

import asyncio
import tomllib
from math import isclose
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from examples.tui_state_field import StateFieldDemo
from textual.widgets import Button
from textual.widgets import Static

from nagents._version import framework_version
from nagents.tui.activity import StateField
from nagents.tui.lifecycle import Lifecycle

if TYPE_CHECKING:
    from textual.widget import Widget


def test_replay_close_reopens_and_then_finishes_without_exit_signal() -> None:
    lifecycle = Lifecycle("quiet", "ready")
    lifecycle.begin("closing", 10.0, replay=True)
    close_end = 10.0 + lifecycle.seconds + 0.01

    assert not lifecycle.advance(close_end)
    assert str(lifecycle.phase) == "opening"
    assert not lifecycle.replay
    open_end = lifecycle.since + lifecycle.seconds + 0.01
    assert not lifecycle.advance(open_end)
    assert str(lifecycle.phase) == "ready"
    assert not lifecycle.advance(open_end + 1)


def test_real_close_signals_exit_once() -> None:
    lifecycle = Lifecycle("stagger", "ready")
    lifecycle.begin("closing", 20.0)
    close_end = 20.0 + lifecycle.seconds + 0.01

    assert lifecycle.advance(close_end)
    assert lifecycle.phase == "closed"
    assert not lifecycle.advance(close_end + 1)


def test_mid_entrance_close_and_mode_retarget_preserve_visibility_and_aperture() -> None:
    lifecycle = Lifecycle("sweep", "opening", since=0)
    entrance_time = 0.34
    before = lifecycle.visibility(entrance_time), lifecycle.aperture(entrance_time)

    lifecycle.begin("closing", entrance_time, replay=True)
    assert (lifecycle.visibility(entrance_time), lifecycle.aperture(entrance_time)) == before

    retarget_time = entrance_time + 0.17
    before_retarget = lifecycle.visibility(retarget_time), lifecycle.aperture(retarget_time)
    lifecycle.begin("closing", retarget_time, replay=True)
    lifecycle.transition = "quiet"
    assert isclose(lifecycle.visibility(retarget_time), before_retarget[0], abs_tol=1e-12)
    assert isclose(lifecycle.aperture(retarget_time), before_retarget[1], abs_tol=1e-12)


def test_aperture_rendering_does_not_advance_normal_particle_phase() -> None:
    field = StateField("delegating", offset=0.4)
    field.advance(0.23)
    phase = field.phase
    closed = field.points(width=16, height=3, aperture=0)
    open_points = field.points(width=16, height=3, aperture=1)

    field.render(width=16, height=3, aperture=0)
    field.render(width=16, height=3, aperture=1)

    assert closed != open_points
    assert field.phase == phase


def _region(widget: Widget) -> tuple[int, int, int, int]:
    region = widget.region
    return region.x, region.y, region.width, region.height


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_enabled_entrance_replay_close_and_fixed_layout(size: tuple[int, int], monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        now = [monotonic()]
        monkeypatch.setattr("examples.tui_state_field.monotonic", lambda: now[0])
        app = StateFieldDemo(motion=True, intro=False)
        # Transitions below are advanced explicitly; wall time under CI load
        # must not finish a close while pilot.press is processing the key.
        monkeypatch.setattr(app, "set_interval", lambda *args, **kwargs: None)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            assert str(app.lifecycle.phase) == "opening"
            fixed = {
                selector: _region(app.query_one(selector)) for selector in ("#main-row", "#provider-model", "#message")
            }

            now[0] += 0.1
            before = (app.lifecycle.visibility(now[0]), app.lifecycle.aperture(now[0]))
            await pilot.press("f2")
            assert str(app.lifecycle.phase) == "closing" and app.lifecycle.replay
            assert app.lifecycle.transition == "stagger"
            assert isclose(app.lifecycle.origin_visibility, before[0], abs_tol=0.05)
            assert isclose(app.lifecycle.origin_aperture, before[1], abs_tol=0.05)

            now[0] += app.lifecycle.seconds + 0.01
            app._tick()
            assert app.lifecycle.phase == "opening"
            assert app.is_running
            now[0] += app.lifecycle.seconds + 0.01
            app._tick()
            await pilot.pause(0.05)
            assert str(app.lifecycle.phase) == "ready"
            assert app.is_running

            for selector, region in fixed.items():
                assert _region(app.query_one(selector)) == region, selector
            for button in app.query(Button):
                x, _y, width, height = _region(button)
                assert width > 0 and height > 0
                assert x >= 0 and x + width <= size[0]
                assert button.styles.visibility == "visible"

            await pilot.press("r")
            assert str(app.lifecycle.phase) == "closing" and app.lifecycle.replay
            await pilot.press("escape")
            assert str(app.lifecycle.phase) == "ready" and app.is_running

            await pilot.press("q")
            assert str(app.lifecycle.phase) == "closing" and app.is_running
            now[0] += app.lifecycle.seconds + 0.01
            app._tick()
            await pilot.pause(0.05)
            assert str(app.lifecycle.phase) == "closed"
            assert not app.is_running

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_ctrl_c_quits_without_waiting_for_animated_close(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=True, intro=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.1)
            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            assert not app.is_running
            assert app.lifecycle.phase != "closing"
        phase_after_shutdown = app.main.phase
        app._tick()  # Simulate a queued timer callback after Textual unmounts the widget tree.
        assert app.main.phase == phase_after_shutdown

    asyncio.run(scenario())


@pytest.fixture(autouse=True)
def driven_demo_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every lifecycle test advances _tick explicitly. Real timer callbacks can
    # otherwise advance beyond the phase being asserted during a slow key event.
    monkeypatch.setattr(StateFieldDemo, "set_interval", lambda *args, **kwargs: None)


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_enabled_brand_intro_versions_then_enters_sweep(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.05)
            assert str(app.lifecycle.phase) == "intro"
            assert app.query_one("#launch").display
            assert not app.query_one("#workspace").display

            version = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())["project"][
                "version"
            ]
            assert app.framework_version == framework_version() == version
            app.lifecycle.since = monotonic() - app.lifecycle.seconds * 0.45
            app._tick()
            title = app.query_one("#launch-title", Static).content
            assert version in str(getattr(title, "plain", title))
            assert not app.query_one("#workspace").display

            await pilot.press("enter")
            assert str(app.lifecycle.phase) == "opening"
            assert not app.query_one("#launch").display
            assert app.query_one("#workspace").display
            fixed = {
                selector: _region(app.query_one(selector)) for selector in ("#main-row", "#provider-model", "#message")
            }
            app.lifecycle.since = monotonic() - app.lifecycle.seconds - 0.01
            app._tick()
            assert str(app.lifecycle.phase) == "ready"
            for selector, region in fixed.items():
                assert _region(app.query_one(selector)) == region, selector
            assert version in str(app.query_one("#brand", Static).content)

    asyncio.run(scenario())


def test_intro_replay_returns_to_branded_intro_without_resetting_particle_phase() -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.05)
            app.lifecycle.since = monotonic() - app.lifecycle.seconds * 0.45
            app._tick()
            assert str(app.lifecycle.phase) == "intro"
            assert app.framework_version in str(app.query_one("#launch-title", Static).content)
            await pilot.press("enter")
            assert str(app.lifecycle.phase) == "opening"
            app.lifecycle.since = monotonic() - app.lifecycle.seconds - 0.01
            app._tick()
            assert str(app.lifecycle.phase) == "ready"

            state_before, phase_before = app.main.state, app.main.phase
            await pilot.press("r")
            assert str(app.lifecycle.phase) == "closing" and app.lifecycle.replay
            app.lifecycle.since = monotonic() - app.lifecycle.seconds - 0.01
            app._tick()
            assert str(app.lifecycle.phase) == "intro"
            assert app.is_running
            assert str(app.main.state) == str(state_before)
            assert app.main.phase > phase_before
            assert app.query_one("#launch").display
            assert not app.query_one("#workspace").display

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_q_during_intro_closes_without_showing_workspace(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.05)
            assert app.query_one("#launch").display
            assert not app.query_one("#workspace").display
            await pilot.press("q")
            assert str(app.lifecycle.phase) == "closing"
            assert app.query_one("#launch").display
            assert not app.query_one("#workspace").display
            app.lifecycle.since = monotonic() - app.lifecycle.seconds - 0.01
            app._tick()
            await pilot.pause(0.05)
            assert str(app.lifecycle.phase) == "closed"
            assert not app.is_running
            assert not app.query_one("#workspace").display

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
@pytest.mark.parametrize("motion,intro,phase", [(True, False, "opening"), (False, True, "ready")])
def test_skip_intro_and_no_animations_use_workspace_directly(
    size: tuple[int, int], motion: bool, intro: bool, phase: str
) -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=motion, intro=intro)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.05)
            assert str(app.lifecycle.phase) == phase
            assert not app.query_one("#launch").display
            assert app.query_one("#workspace").display
            await pilot.press("q")
            if motion:
                assert str(app.lifecycle.phase) == "closing"
                app.lifecycle.since = monotonic() - app.lifecycle.seconds - 0.01
                app._tick()
                await pilot.pause(0.05)
            assert not app.is_running

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_ctrl_c_exits_immediately_during_enabled_intro(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = StateFieldDemo(motion=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.05)
            assert str(app.lifecycle.phase) == "intro"
            await pilot.press("ctrl+c")
            await pilot.pause(0.05)
            assert not app.is_running
            assert str(app.lifecycle.phase) == "intro"

    asyncio.run(scenario())
