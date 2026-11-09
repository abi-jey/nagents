"""Interpolated effects preserve content, layout and non-animated information."""

from __future__ import annotations

import asyncio

import pytest
from examples.tui_effects import EFFECTS
from examples.tui_effects import effect_text
from examples.tui_effects import particle_field
from examples.tui_motion_v2 import QuietMotion
from rich.cells import cell_len
from textual.widgets import Static


def test_text_effects_change_light_without_replacing_the_message() -> None:
    label = "Thinking through the options"
    for effect in ("shimmer", "breathe"):
        selected = next(value for value in EFFECTS if value == effect)
        earlier = effect_text(label, 0.4, selected, native=False)
        later = effect_text(label, 0.8, selected, native=False)
        assert earlier.plain == later.plain == label
        assert earlier.spans != later.spans
        assert effect_text(label, 0.4, selected, motion=False) == effect_text(label, 8, selected, motion=False)
        assert effect_text(label, 0.4, selected, active=False).plain == label


def test_continuous_particle_canvas_stays_within_its_cell_rectangle() -> None:
    frames = {particle_field(index / 20, width=16, height=3) for index in range(20)}
    assert len(frames) > 4
    for frame in frames:
        rows = frame.splitlines()
        assert len(rows) == 3
        assert all(cell_len(row) == 16 for row in rows)
        assert all(0x2800 <= ord(character) <= 0x28FF for row in rows for character in row)
    active = effect_text("mini  Working", 0.4, "field")
    settled = effect_text("mini  Working", 0.4, "field", active=False)
    assert cell_len(active.plain) == cell_len(settled.plain)
    assert active.plain[8:] == settled.plain[8:] == "mini  Working"


@pytest.mark.parametrize("effect", EFFECTS)
@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_quiet_demo_footer_and_controls(effect: str, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        selected = next(value for value in EFFECTS if value == effect)
        app = QuietMotion(selected, theme="graphite", motion=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            model = app.query_one("#provider-model", Static)
            assert "openai · gpt-6.1-sol" in str(model.content)
            assert model.region.y >= app.query_one("#message").region.bottom
            assert model.region.bottom <= size[1]
            assert not app.query_one("#intro").display
            assert not app.query_one("#agent-details").display
            await pilot.press("2", "e", "d")
            assert str(app.effect) == "field"
            assert app.query_one("#agent-details").display
            assert str(app.state.wake) == "queued"
            assert "Due" in str(app.query_one("#details", Static).content)
            await pilot.press("m", "m", "m")
            assert str(app.state.wake) == "delivered"
            await pilot.press("3", "l")
            assert str(app.effect) == "breathe"
            assert app.query_one("#timings").display
            await pilot.press("q")

    asyncio.run(scenario())


@pytest.mark.parametrize("effect", EFFECTS)
def test_startup_effect_fades_out_and_can_be_replayed_or_quit(effect: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pilot.press waits for the message queue to settle. On a busy runner that
    # may outlast the entire 1.2-second intro, so control the demo's clock rather
    # than assuming an active frame survives until press returns.
    clock = [100.0]
    monkeypatch.setattr("examples.tui_motion_v2.monotonic", lambda: clock[0])

    async def scenario() -> None:
        selected = next(value for value in EFFECTS if value == effect)
        app = QuietMotion(selected, theme="graphite")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("0")
            assert app.query_one("#intro").display
            clock[0] += 1.0
            app._tick()
            assert app._intro_fading
            assert app.query_one("#intro").display
            clock[0] += 0.3
            app._tick()
            assert not app.query_one("#intro").display
            await pilot.press("0")
            assert app.query_one("#intro").display
            await pilot.press("q")

    asyncio.run(scenario())


def test_demo_tick_does_not_query_widgets_during_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = QuietMotion("shimmer", theme="graphite")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.exit()

            # Reproduce an already-dispatched timer arriving after exit starts.
            # Querying even a cached widget is unsafe as screen teardown proceeds.
            def unexpected_query(*args: object, **kwargs: object) -> None:
                pytest.fail("A shutdown tick must not query the removed screen")

            with monkeypatch.context() as patch:
                patch.setattr(app, "query_one", unexpected_query)
                app._tick()

    asyncio.run(scenario())


def test_startup_effect_timer_hides_expired_intro() -> None:
    async def scenario() -> None:
        app = QuietMotion("shimmer", theme="graphite")
        async with app.run_test(size=(80, 24)):
            # Start synchronously: unlike Pilot.press this cannot yield past
            # the active frame before we assert it. The real timer must expire it.
            app.action_intro()
            assert app.query_one("#intro").display
            async with asyncio.timeout(10):
                while app.query_one("#intro").display:
                    await asyncio.sleep(0.05)
            assert not app.query_one("#intro").display

    asyncio.run(scenario())
