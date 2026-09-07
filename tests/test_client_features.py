"""Main-app integration of manual child follow-ups and opt-in voice drafts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.widgets import TextArea

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.tui import NagentsApp
from nagents.tui.dictation import DictationModal
from nagents.tui.screens import DetailModal
from nagents.tui.tasks import TaskScreen
from nagents.tui.widgets import Composer
from tests.test_subagents import setup_harness
from tests.test_tui import FakeHarness
from tests.test_tui import idle
from tests.test_tui import make_app
from tests.test_tui import send
from tests.test_tui_dictation import FakeVoice

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.test_subagents import FakeProvider


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
            async with asyncio.timeout(5):
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
            async with asyncio.timeout(5):
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


@pytest.mark.parametrize("cancel_key", ["escape", "ctrl+c"])
def test_main_dictation_binding_cancels_capture_and_keeps_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_key: str,
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.demo = False
        backend.config.dictation_enabled = True
        voice = FakeVoice(backend.config)
        monkeypatch.setattr("nagents.tui.dictation.VoiceDictation", lambda config: voice)
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("Existing draft")
            await pilot.press("ctrl+g")
            await pilot.pause()
            assert isinstance(app.screen, DictationModal)
            assert voice.capture_calls == 0
            await pilot.click("#dictation-primary")
            await pilot.pause()
            assert voice.capture_calls == 1
            await pilot.press(cancel_key)
            await pilot.pause()
            assert not isinstance(app.screen, DictationModal)
            assert voice.closed and voice.cancelled.is_set()
            assert not voice.uploads
            assert composer.text == "Existing draft" and not backend.prompts

    asyncio.run(scenario())


def test_main_dictation_uses_edited_preview_without_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.demo = False
        backend.config.dictation_enabled = True
        voice = FakeVoice(backend.config)
        voice.upload_release.set()
        monkeypatch.setattr("nagents.tui.dictation.VoiceDictation", lambda config: voice)
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("Before. ")
            composer.move_cursor((0, len(composer.text)))
            app.command("/dictate")
            await pilot.pause()
            assert isinstance(app.screen, DictationModal)
            await pilot.click("#dictation-primary")
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await pilot.pause()
            preview = app.screen.query_one("#dictation-preview", TextArea)
            assert preview.text == "Synthetic dictated draft."
            preview.load_text("Edited transcript")
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await pilot.pause()
            assert composer.text == "Before. Edited transcript"
            assert voice.closed and not backend.prompts
            assert composer.has_focus

    asyncio.run(scenario())


def test_dictation_requires_opt_in_and_demo_stays_offline(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.demo = False
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+g")
            assert isinstance(app.screen, DetailModal)
            assert "--dictation" in app.screen.text
            await pilot.press("escape")
            backend.config.demo = True
            backend.config.dictation_enabled = True
            app.command("/dictate")
            await pilot.pause()
            assert not isinstance(app.screen, DictationModal)
            assert not backend.prompts

    asyncio.run(scenario())
