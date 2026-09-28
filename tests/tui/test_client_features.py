"""Main-app integration of manual child follow-ups."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.widgets import TextArea

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.tui import NagentsApp
from nagents.tui.tasks import TaskScreen
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import setup_harness
from tests.support.tui import idle
from tests.support.tui import send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
def test_followup_while_parent_busy_uses_existing_run_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        blocked = asyncio.Event()
        release = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                yield TextDoneEvent(text="Child completed this question.")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate-one", name="delegate", arguments={"prompt": "Independent task"})
            elif len(provider.requests) == 2:
                blocked.set()
                await release.wait()
                yield TextDoneEvent(text="Parent continued its own work.")
            else:
                yield TextDoneEvent(text="Parent considered the child exchange.")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 32)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "Delegate one task")
            async with asyncio.timeout(HANG_GUARD):
                await blocked.wait()
                while not harness.tasks.list() or harness.tasks.list()[0].status != "completed":
                    await pilot.pause(0.01)
            active = app._active
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert isinstance(app.screen, TaskScreen)
            task_id = app.screen.selected_id
            app.screen.query_one("#task-followup", TextArea).load_text("Human child follow-up")
            await pilot.click("#task-send")
            async with asyncio.timeout(HANG_GUARD):
                while harness.tasks.list()[0].followups != 1 or harness.tasks.list()[0].status != "completed":
                    await pilot.pause(0.01)
            assert app._active is active and app.busy
            assert not app._queued_prompts
            release.set()
            await idle(app, pilot)
            assert any(message.content == "Human child follow-up" for message in await harness.task_history(task_id))
            assert any(
                isinstance(message.content, str)
                and '"human_messages"' in message.content
                and "Human child follow-up" in message.content
                for message in await harness.history()
            )

    asyncio.run(scenario())
