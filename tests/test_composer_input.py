"""Configurable terminal key actions and run-safe prompt delivery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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
from tests.test_tui import FakeHarness
from tests.test_tui import idle
from tests.test_tui import make_app
from tests.test_tui import send

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


def test_tab_cycles_profiles_and_shift_tab_reverses(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.profiles["audit"] = AgentProfile(mode="reviewer")
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("tab")
            await idle(app, pilot)
            assert backend.config.agent == "agent"
            await pilot.press("tab")
            await idle(app, pilot)
            assert backend.config.agent == "reviewer"
            await pilot.press("tab")
            await idle(app, pilot)
            assert backend.config.agent == "audit"
            await pilot.press("shift+tab")
            await idle(app, pilot)
            assert backend.config.agent == "reviewer"
            await pilot.press("shift+tab")
            await idle(app, pilot)
            assert backend.config.agent == "agent"
            await pilot.press("shift+tab")
            await idle(app, pilot)
            assert backend.config.agent == "build"
            await pilot.press("shift+tab")
            await idle(app, pilot)
            assert backend.config.agent == "audit"
            assert app.focused is app.query_one(Composer)

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["agent", "focus"])
def test_tab_modes_preserve_draft_and_do_not_insert_indent(tmp_path: Path, action: str) -> None:
    async def scenario() -> None:
        backend = GatedHarness(tmp_path)
        backend.config.tab_action = action
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "first")
            composer = app.query_one(Composer)
            await pilot.press(*"draft", "tab")
            assert composer.text == "draft"
            assert backend.config.agent == "build"
            assert backend.prompts == ["first"]
            assert (app.focused is composer) is (action == "agent")
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


def test_input_policies_parse_from_flags_environment_and_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert HarnessConfig(workspace=tmp_path).submit_mode == "queue"
    assert HarnessConfig(workspace=tmp_path).tab_action == "agent"
    args = _parser().parse_args(["--submit-mode", "interrupt", "run", "--tab-action", "complete", "hello"])
    assert args.submit_mode == "interrupt" and args.tab_action == "complete"
    monkeypatch.setenv("NGN_SUBMIT_MODE", "interrupt")
    monkeypatch.setenv("NGN_TAB_ACTION", "focus")
    config = load_config(tmp_path)
    assert config.submit_mode == "interrupt" and config.tab_action == "focus"
    path = tmp_path / "config.toml"
    path.write_text('submit_mode = "queue"\ntab_action = "complete"\n')
    config = load_config(tmp_path, path)
    assert config.submit_mode == "queue" and config.tab_action == "complete"
    with pytest.raises(ValueError, match="submit_mode"):
        HarnessConfig(workspace=tmp_path, submit_mode="discard")
    with pytest.raises(ValueError, match="tab_action"):
        HarnessConfig(workspace=tmp_path, tab_action="unknown")
