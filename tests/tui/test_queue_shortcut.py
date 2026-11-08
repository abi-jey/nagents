"""Queued-message shortcut stages delivery, then safely interrupts on repeat."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.screen import ModalScreen
from textual.widgets import Static

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.tui.widgets import Composer
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.harness.types import HarnessEvent


class QueuedHarness(FakeHarness):
    def __init__(self, workspace: Path, phase: str) -> None:
        super().__init__(workspace)
        self.phase = phase
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleaned = False

    async def run(self, prompt: str) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        self.running = True
        try:
            if prompt == "first":
                if self.phase == "text":
                    yield TextChunkEvent(chunk="Partial answer")
                else:
                    yield ToolCallEvent(id="tool", name="shell", arguments={"command": "check"})
                    if self.phase == "result":
                        yield ToolResultEvent(id="tool", name="shell", result="tool result")
                await self.release.wait()
            else:
                assert self.cleaned
            yield DoneEvent()
        except asyncio.CancelledError:
            self.cancelled = True
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            raise
        finally:
            self.cleaned = True
            self.running = False


@pytest.mark.parametrize("phase", ["text", "tool", "result"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_queue_shortcut_stages_then_interrupts(tmp_path: Path, phase: str, interrupt: bool) -> None:
    async def scenario() -> None:
        backend = QueuedHarness(tmp_path, phase)
        app = make_app(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            await send(app, pilot, "second")
            await send(app, pilot, "third")
            composer = app.query_one(Composer)
            composer.text = "unsent draft"
            label = app.query_one("#queue-status", Static)
            assert "Ctrl+S: send next" in str(label.content)
            await pilot.press("ctrl+s")
            assert backend.prompts == ["first"]
            assert not backend.cancelled
            assert "Ctrl+S again: interrupt & send" in str(label.content)
            if interrupt:
                await pilot.press("ctrl+s")
                await asyncio.wait_for(backend.cleanup_started.wait(), 2)
                assert backend.prompts == ["first"]
                assert not backend.cleaned
                # Repeats during cancellation must not cancel cleanup again.
                await pilot.press("ctrl+s")
                backend.cleanup_release.set()
            else:
                backend.release.set()
            await idle(app, pilot)
            assert backend.prompts == ["first", "second", "third"]
            assert backend.cancelled is interrupt
            assert composer.text == "unsent draft"
            assert not label.display
            assert not app._queue_send_armed

    asyncio.run(scenario())


def test_queue_shortcut_empty_modal_and_paused_queue(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = QueuedHarness(tmp_path, "text")
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.text = "draft"
            await pilot.press("ctrl+s", "ctrl+s")
            assert not backend.prompts
            assert composer.text == "draft"
            await send(app, pilot, "first")
            await send(app, pilot, "second")
            await app.push_screen(ModalScreen[None]())
            await pilot.press("ctrl+s", "ctrl+s")
            assert not app._queue_send_armed
            assert not backend.cancelled
            app.pop_screen()
            await pilot.pause()
            await pilot.press("ctrl+s")
            assert app._queue_send_armed
            backend.cleanup_release.set()
            await pilot.press("escape")
            await idle(app, pilot)
            assert app._queue_paused
            assert not app._queue_send_armed
            await pilot.press("ctrl+s")
            await idle(app, pilot)
            assert backend.prompts == ["first", "second"]
            assert not app._queue_paused

    asyncio.run(scenario())
