"""Normal terminal focus and run-safe prompt delivery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Literal
from typing import cast

import pytest
from textual.events import Key
from textual.widgets import Static

from nagents.cli import _parser
from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.harness import HarnessConfig
from nagents.harness import load_config
from nagents.harness.config import AgentProfile
from nagents.tui.widgets import Composer
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.harness.types import HarnessEvent


class GatedHarness(FakeHarness):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.release = asyncio.Event()

    async def run(self, prompt: str) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        self.running = True
        try:
            yield TextChunkEvent(chunk=f"Processing {prompt}.")
            if prompt == "first":
                await self.release.wait()
            yield TextDoneEvent(text=f"Processing {prompt}.")
            yield DoneEvent()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.running = False


def test_empty_composer_has_no_placeholder_and_keeps_focus(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            assert composer.text == ""
            assert composer.placeholder == ""
            assert composer.has_focus
            assert composer.cursor_blink

    asyncio.run(scenario())


def test_shift_enter_is_newline_not_submit(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press(*"hello", "shift+enter", *"world")
            assert app.query_one(Composer).text == "hello\nworld"
            assert backend.prompts == []
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.prompts == ["hello\nworld"]

    asyncio.run(scenario())


def test_fast_keyboard_burst_preserves_newline_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            for character in "first line":
                composer.post_message(Key(character, character))
            composer.post_message(Key("shift+enter", None))
            for character in "second line":
                composer.post_message(Key(character, character))
            await pilot.pause()
            assert composer.text == "first line\nsecond line"
            assert backend.prompts == []

    asyncio.run(scenario())


def test_tab_keeps_composer_focus_without_switching_profiles(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.profiles["audit"] = AgentProfile(mode="reviewer")
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("keep this draft")
            composer.focus()
            await pilot.press("tab")
            assert app.focused is composer
            assert backend.config.agent == "assistant"
            assert composer.text == "keep this draft"
            await pilot.press("shift+tab")
            assert app.focused is composer
            assert backend.config.agent == "assistant"
            assert composer.text == "keep this draft"

    asyncio.run(scenario())


def test_tab_during_run_keeps_draft_without_switching_profile(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        backend.config.profiles["audit"] = AgentProfile(mode="reviewer")
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            composer = app.query_one(Composer)
            composer.load_text("draft stays here")
            await pilot.press("tab")
            assert composer.text == "draft stays here"
            assert backend.config.agent == "assistant"
            assert backend.prompts == ["first"]
            backend.release.set()
            await idle(app, pilot)

    asyncio.run(scenario())


def test_prompts_queue_fifo_without_interrupting_current_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            await send(app, pilot, "second")
            await send(app, pilot, "third")
            assert backend.prompts == ["first"]
            assert not backend.cancelled
            assert list(app._queued_prompts) == ["second", "third"]
            assert "2 queued" in str(app.query_one("#queue-status", Static).content)
            backend.release.set()
            await idle(app, pilot)
            assert backend.prompts == ["first", "second", "third"]
            assert not app.query_one("#queue-status").display

    asyncio.run(scenario())


@pytest.mark.parametrize("width", [40, 100])
def test_queue_shows_one_compact_line_per_message_above_composer(tmp_path: Path, width: int) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(width, 24)) as pilot:
            await idle(app, pilot)
            app._queued_prompts.extend(["first\nmultiline message", "工具" * 100, "[literal] third"])
            app._update_queue()
            await pilot.pause()
            queue = app.query_one("#queue-status", Static)
            lines = str(queue.content).splitlines()
            assert len(lines) == 3
            assert lines[0].endswith("1. first multiline message")
            assert lines[1].startswith("2. 工具")
            assert lines[2] == "3. [literal] third"
            assert queue.size.height == 3
            assert queue.region.bottom <= app.query_one(Composer).region.y
            await pilot.resize_terminal(width + 15, 24)
            await pilot.pause()
            assert queue.size.height == 3
            app._queued_prompts.clear()
            app._update_queue()
            await pilot.pause()
            assert not queue.display

    asyncio.run(scenario())


def test_prompt_commands_in_queue_expand_before_model_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        backend.commands.register("review", "Review a target", prompt="Review $ARGUMENTS")
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            await send(app, pilot, "/review README.md")
            assert list(app._queued_prompts) == ["/review README.md"]
            backend.release.set()
            await idle(app, pilot)
            assert backend.prompts == ["first", "Review README.md"]

    asyncio.run(scenario())


def test_interrupt_mode_waits_for_cancellation_before_next_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        backend.config.submit_mode = "interrupt"
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            await send(app, pilot, "second")
            await idle(app, pilot)
            assert backend.cancelled
            assert not backend.release.is_set()
            assert backend.prompts == ["first", "second"]

    asyncio.run(scenario())


def test_stop_pauses_queue_and_empty_enter_resumes_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            await send(app, pilot, "second")
            await pilot.press("escape")
            await idle(app, pilot)
            assert backend.cancelled and app._queue_paused
            assert backend.prompts == ["first"]
            assert list(app._queued_prompts) == ["second"]
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.prompts == ["first", "second"]

    asyncio.run(scenario())


def test_queue_can_be_cleared_without_stopping_current_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            await send(app, pilot, "second")
            await send(app, pilot, "/queue clear")
            assert not app._queued_prompts
            assert backend.running
            backend.release.set()
            await idle(app, pilot)
            assert backend.prompts == ["first"]

    asyncio.run(scenario())


def test_input_policies_parse_from_flags_environment_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert HarnessConfig(workspace=tmp_path).submit_mode == "queue"
    args = _parser().parse_args(["--submit-mode", "interrupt", "run", "hello"])
    assert args.submit_mode == "interrupt"
    monkeypatch.setenv("NGN_SUBMIT_MODE", "interrupt")
    config = load_config(tmp_path)
    assert config.submit_mode == "interrupt"
    path = tmp_path / "config.json"
    path.write_text('{"submit_mode": "queue"}\n')
    config = load_config(tmp_path, path)
    assert config.submit_mode == "queue"
    with pytest.raises(ValueError, match="submit_mode"):
        HarnessConfig(workspace=tmp_path, submit_mode=cast("Literal['queue', 'interrupt']", "discard"))
