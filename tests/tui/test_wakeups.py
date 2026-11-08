"""Process local TUI wake-up bounds and claim ownership."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from nagents.tui import wakeups as wakeup_module
from nagents.tui.wakeups import Wakeups
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message


def test_schedule_bounds_and_finite_validation() -> None:
    scheduler = Wakeups()
    for delay in (0, -1, float("inf"), float("nan"), 604801, True):
        with pytest.raises(ValueError):
            scheduler.schedule("root", "root", delay, "later")
    with pytest.raises(ValueError):
        scheduler.schedule("root", "root", 1, " ")
    for _ in range(32):
        scheduler.schedule("root", "root", 1, "later")
    with pytest.raises(ValueError, match="budget"):
        scheduler.schedule("root", "root", 1, "later")
    assert len(scheduler.pending) == 32


def test_due_claim_is_root_scoped_and_one_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [10.0]
    monkeypatch.setattr(wakeup_module, "monotonic", lambda: now[0])
    scheduler = Wakeups()
    scheduled = scheduler.schedule("root-a", "child-a", 2, "check")
    due_at = datetime.fromisoformat(scheduled["due_at"])
    assert due_at.tzinfo == UTC
    assert abs((due_at - datetime.now(UTC) - timedelta(seconds=2)).total_seconds()) < 1
    now[0] = 12
    assert scheduler.claim("root-b") == ()
    claimed = scheduler.claim("root-a")
    assert len(claimed) == 1
    assert claimed[0].session_id == "child-a"
    assert scheduler.claim("root-a") == ()


def test_budget_caps_activation_chain_and_human_turn_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(wakeup_module, "monotonic", lambda: now[0])
    scheduler = Wakeups()
    scheduler.schedule("root", "child", 1, "step 1")
    for activation in range(8):
        now[0] += 1
        due = scheduler.claim("root")
        assert len(due) == 1
        assert due[0].budget.activations == activation + 1
        if activation < 7:
            scheduler.schedule("root", "child", 1, f"step {activation + 2}")
    with pytest.raises(ValueError, match="budget"):
        scheduler.schedule("root", "child", 1, "step 9")
    scheduler.human_turn()
    scheduler.schedule("root", "root", 1, "new human chain")


def test_cancel_turn_revokes_chain_and_close_drops_everything() -> None:
    scheduler = Wakeups()
    scheduler.schedule("root", "child", 60, "pending")
    scheduler.cancel_turn()
    assert scheduler.pending == {}
    with pytest.raises(RuntimeError):
        scheduler.schedule("root", "root", 1, "cancelled")
    scheduler.human_turn()
    scheduler.schedule("root", "root", 60, "pending after human")
    scheduler.close()
    assert scheduler.pending == {}
    assert scheduler.claim("root") == ()
    with pytest.raises(RuntimeError):
        scheduler.schedule("root", "root", 1, "closed")


def test_real_demo_harness_handler_is_owned_and_cleared(tmp_path: Path) -> None:
    from nagents.harness import Harness
    from nagents.harness import HarnessConfig
    from nagents.tui.app import NagentsApp

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            assert app._owns_wakeups
            assert harness.wakeup_handler is not None
        assert harness.wakeup_handler is None

    asyncio.run(scenario())


def test_existing_harness_handler_is_not_replaced(tmp_path: Path) -> None:
    from nagents.harness import Harness
    from nagents.harness import HarnessConfig
    from nagents.tui.app import NagentsApp

    async def handler(session_id: str, delay: float, reason: str) -> dict[str, str]:
        return {"session_id": session_id, "reason": reason, "delay": str(delay)}

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        harness.wakeup_handler = handler
        app = NagentsApp(harness)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            assert not app._owns_wakeups
            assert harness.wakeup_handler is handler
        assert harness.wakeup_handler is handler
        await harness.close()

    asyncio.run(scenario())


def test_real_tui_wakeup_waits_for_human_queue_then_uses_harness_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import ToolCallEvent
    from nagents.tui.app import NagentsApp
    from nagents.tui.widgets import Composer
    from tests.support.tui import send

    async def scenario() -> None:
        continue_schedule = asyncio.Event()
        schedule_started = asyncio.Event()
        provider_inputs: list[list[Message]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            provider_inputs.append(messages)
            request_number = len(provider.requests)
            if request_number == 1:
                yield ToolCallEvent(
                    id="timer", name="schedule_wakeup", arguments={"seconds": 0.2, "reason": "Check later"}
                )
            elif request_number == 2:
                schedule_started.set()
                await continue_schedule.wait()
                yield TextDoneEvent(text="Timer scheduled")
            elif request_number == 3:
                yield TextDoneEvent(text="Human follow-up handled")
            else:
                yield TextDoneEvent(text="Wake handled")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        try:
            async with app.run_test(size=(90, 28)) as pilot:
                await pilot.pause(0.2)
                assert app._owns_wakeups and harness.wakeup_handler is not None
                await send(app, pilot, "start timer")
                async with asyncio.timeout(HANG_GUARD):
                    await schedule_started.wait()
                await pilot.pause(0.3)
                assert app.busy
                assert len(app._wakeups.pending) == 1
                assert len(providers[0].requests) == 2
                await send(app, pilot, "queued human follow-up")
                assert app._queued_prompts
                continue_schedule.set()
                async with asyncio.timeout(HANG_GUARD):
                    while len(providers[0].requests) < 4 or app.busy:
                        await pilot.pause(0.02)
                assert not app._wakeups.pending
                assert len(providers[0].requests) == 4
                history = await harness.history()
                human_prompts = [str(message.content) for message in history if message.role == "user"]
                assert any("queued human follow-up" in prompt for prompt in human_prompts)
                assert not any("Check later" in prompt for prompt in human_prompts)
                assert any('"wakeups"' in str(message.content) for message in provider_inputs[-1])
                assert len(app.query(Composer)) == 1
                assert len(app.query("Turn")) == 3  # Two human turns and the scheduled-wakeup status turn.
        finally:
            continue_schedule.set()
            if app.is_running:
                await app.action_quit()
        assert harness.wakeup_handler is None
        await harness.close()


def test_pending_timer_stays_owned_by_source_across_tui_fork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nagents.events import TextDoneEvent
    from nagents.events import ToolCallEvent
    from nagents.tui.app import NagentsApp
    from nagents.tui.widgets import Turn
    from tests.support.tui import idle
    from tests.support.tui import send

    async def scenario() -> None:
        clock = [0.0]
        monkeypatch.setattr(wakeup_module, "monotonic", lambda: clock[0])

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="source-timer",
                    name="schedule_wakeup",
                    arguments={"seconds": 60, "reason": "Wake the original session"},
                )
            else:
                yield TextDoneEvent(text="Woken on the source session")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test(size=(90, 28)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "Original session turn")
            await idle(app, pilot)
            source_id = harness.session_id
            assert len(app._wakeups.pending) == 1
            original = next(iter(app._wakeups.pending.values()))
            assert original.root_session_id == source_id
            assert original.session_id == ""
            source_history_before_fork = await harness.history()

            await send(app, pilot, "/fork copied branch")
            await idle(app, pilot)
            copied_id = harness.session_id
            assert copied_id != source_id
            assert await harness.history() == source_history_before_fork
            assert next(iter(app._wakeups.pending.values())) is original
            assert original.root_session_id == source_id

            clock[0] = 60.0
            app._refresh_tasks()
            assert app._wakeups.claim(copied_id) == ()
            assert app._wakeups.pending == {original.id: original}
            assert len(providers[0].requests) == 2

            app._chosen_session(source_id)
            async with asyncio.timeout(HANG_GUARD):
                while app.busy or len(providers[0].requests) < 3:
                    await pilot.pause(0.02)
            assert len(providers[0].requests) == 3
            assert app._wakeups.pending == {}
            assert any('"wakeups"' in str(message.content) for message in providers[0].requests[-1])
            assert not any(
                turn.role == "user" and "Wake the original session" in str(turn.body.render())
                for turn in app.query(Turn)
            )

        assert app._wakeups.closed and not app._wakeups.pending
        assert harness.wakeup_handler is None
        assert all(worker.done() for worker in harness.tasks._workers.values())
        await harness.close()

    asyncio.run(scenario())
