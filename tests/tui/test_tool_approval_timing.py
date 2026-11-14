"""Approval snapshots exclude human waiting from the live root tool clock."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.types import ToolApprovalTiming
from nagents.tui.widgets import ToolCard
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


def test_repeated_approval_waits_rebase_active_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("nagents.tui.widgets.monotonic", lambda: clock[0])
    card = ToolCard("call", "shell", {"timeout": 30})
    clock[0] = 102.0
    card.set_approval_timing(1.25, waiting=True)
    clock[0] = 1002.0
    card._tick_elapsed()
    assert card._duration == "1.25s"
    assert "1.25s" in card.title and "30s" in card.title

    card.set_approval_timing(1.25, waiting=False)
    clock[0] += 2
    card._tick_elapsed()
    assert card._duration == "3.25s"
    card.set_approval_timing(3.0, waiting=True)
    clock[0] += 500
    card._tick_elapsed()
    assert card._duration == "3.00s"
    card.set_approval_timing(3.0, waiting=False)
    clock[0] += 0.5
    card._tick_elapsed()
    assert card._duration == "3.50s"


@pytest.mark.parametrize("outcome", ["complete", "failed", "cancelled"])
def test_terminal_card_ignores_late_approval_timing(outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("nagents.tui.widgets.monotonic", lambda: clock[0])
    card = ToolCard("call", "custom", {})
    card.set_approval_timing(1.25, waiting=True)
    clock[0] += 1000
    if outcome == "cancelled":
        card.cancel()
        assert card._duration == "1.25s"
    else:
        card.finish({}, "denied" if outcome == "failed" else None, 1500)
        assert card._duration == "1.50s"
    frozen = card.title
    card.set_approval_timing(99, waiting=False)
    card._tick_elapsed()
    assert card.title == frozen
    assert card._elapsed_timer is None


def test_new_invocation_resets_approval_pause() -> None:
    card = ToolCard("old", "custom", {})
    card.set_approval_timing(2, waiting=True)
    card.cancel()
    card.start_call("new", "custom", {})
    assert not card._approval_waiting
    assert card._duration == "0.00s"


def test_tui_routes_approval_timing_only_to_running_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await app._event(ToolApprovalTiming("missing", 50, True))
            assert not app._tools
            await app._event(ToolCallEvent(id="call", name="shell", arguments={"timeout": 30}))
            await app._event(ToolCallEvent(id="other", name="custom", arguments={}))
            card = app._tools["call"]
            other = app._tools["other"]
            clock = [100.0]
            monkeypatch.setattr("nagents.tui.widgets.monotonic", lambda: clock[0])
            await app._event(ToolApprovalTiming("call", 1.25, True))
            timer = card._elapsed_timer
            assert timer is not None
            clock[0] += 1000
            await pilot.pause(0.15)
            assert card._duration == "1.25s"
            assert not other._approval_waiting
            assert app._running_tools == {"call", "other"}
            await app._event(ToolApprovalTiming("call", 1.25, False))
            clock[0] += 2
            await pilot.pause(0.15)
            assert card._duration == "3.25s"
            assert card._elapsed_timer is timer
            await app._event(ToolResultEvent(id="call", name="shell", result={}, duration_ms=3500))
            frozen = card.title
            await app._event(ToolApprovalTiming("call", 99, False))
            assert card.title == frozen
            assert card._elapsed_timer is None
            assert len(app._tools) == 2

    asyncio.run(scenario())
