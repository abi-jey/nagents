"""One application owns the expanding shell, reveal, initialization and exit."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.tui import NagentsApp
from nagents.tui.screens import ChoiceModal
from nagents.tui.widgets import Composer
from tests.support.tui import FakeHarness
from tests.tui.test_motion_integration import _rendered_screen_text

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness import Harness
    from nagents.harness.types import SessionInfo


@pytest.mark.parametrize("startup", [False, True])
def test_inline_handoff_first_render_has_complete_layout(
    tmp_path: Path, startup: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = NagentsApp(cast("Harness", backend), startup=startup)
        app._complete_inline_entrance()
        frames: list[str] = []

        def capture() -> None:
            frames.append(_rendered_screen_text(app))

        monkeypatch.setattr(app, "post_display_hook", capture)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#app-shell").region.height == 24
            assert not app._sweep.enabled
            assert app._motion_lifecycle.phase == "ready"
            assert frames
            assert "Explain this workspace" in frames[0]
            assert app.startup_opened

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (132, 38)])
def test_backend_initializes_during_entrance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    async def scenario() -> None:
        now = [0.0]
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: now[0])
        backend = FakeHarness(tmp_path)
        app = NagentsApp(cast("Harness", backend), startup=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            shell = app.query_one("#app-shell")
            initial = shell.region.height
            assert backend.initialized
            assert not app.startup_opened
            assert app._sweep.amount == 0
            now[0] = 0.125
            app._animate_activity()
            await pilot.pause()
            middle = shell.region.height
            assert initial < middle < size[1]
            assert app._sweep.amount == 0
            rendered = _rendered_screen_text(app)
            assert "╭" in rendered and "╰" in rendered
            assert "Explain this workspace" not in rendered
            now[0] = 0.25
            app._animate_activity()
            await pilot.pause()
            assert shell.region.height == size[1]
            assert app._sweep.amount == 0
            now[0] = 0.5
            app._animate_activity()
            assert 0 < app._sweep.amount < 1
            assert backend.create_session_calls == [True]
            now[0] = 0.8
            app._animate_activity()
            app._animate_activity()
            await pilot.pause()
            assert app.startup_opened
            assert backend.initialized
            assert not app._sweep.enabled
        assert backend.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["enter", "space"])
def test_skip_enters_once_without_submitting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: 0.0)
        backend = FakeHarness(tmp_path)
        app = NagentsApp(cast("Harness", backend), startup=True)
        async with app.run_test() as pilot:
            await pilot.press(key)
            await pilot.pause()
            assert app.startup_opened
            assert backend.create_session_calls == [True]
            assert not backend.prompts
            assert not app._sweep.enabled

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["escape", "ctrl+c", "ctrl+q"])
def test_cancel_cleans_up_initialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: 0.0)
        backend = FakeHarness(tmp_path)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def initialize(*, create_session: bool = True) -> None:
            backend.create_session_calls.append(create_session)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(backend, "initialize", initialize)
        app = NagentsApp(cast("Harness", backend), startup=True)
        async with app.run_test() as pilot:
            await started.wait()
            await pilot.press(key)
            app._finish_startup()
        assert not app.startup_opened
        assert cancelled.is_set()
        assert backend.create_session_calls == [True]
        assert backend.closed
        assert app._active is None

    asyncio.run(scenario())


def test_skip_during_loading_does_not_restart_or_lose_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: 0.0)
        backend = FakeHarness(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()

        async def initialize(*, create_session: bool = True) -> None:
            backend.create_session_calls.append(create_session)
            started.set()
            await release.wait()
            backend.initialized = True

        monkeypatch.setattr(backend, "initialize", initialize)
        app = NagentsApp(cast("Harness", backend), startup=True)
        async with app.run_test() as pilot:
            await started.wait()
            await pilot.press("enter", "h", "i", "enter")
            assert app.startup_opened
            assert backend.create_session_calls == [True]
            assert not backend.prompts
            release.set()
            from tests.support.hang_guard import HANG_GUARD

            async with asyncio.timeout(HANG_GUARD):
                while not backend.prompts:
                    await pilot.pause()
            assert backend.prompts == ["hi"]
            assert backend.create_session_calls == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_session_picker_waits_for_entrance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: 0.0)
        backend = FakeHarness(tmp_path)
        listed = asyncio.Event()
        original = backend.list_sessions

        async def list_sessions() -> list[SessionInfo]:
            sessions = await original()
            listed.set()
            return sessions

        monkeypatch.setattr(backend, "list_sessions", list_sessions)
        app = NagentsApp(cast("Harness", backend), startup=True, pick_session=True)
        async with app.run_test() as pilot:
            await listed.wait()
            await pilot.pause()
            assert backend.create_session_calls == [False]
            assert not isinstance(app.screen, ChoiceModal)
            if cancel:
                await pilot.press("escape")
            else:
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, ChoiceModal)
                assert backend.create_session_calls == [False]
        assert backend.closed
        assert app._active is None

    asyncio.run(scenario())


def test_typing_skips_entrance_and_keeps_first_character(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: 0.0)
        backend = FakeHarness(tmp_path)
        app = NagentsApp(cast("Harness", backend), startup=True)
        async with app.run_test() as pilot:
            await pilot.press("h", "i")
            assert app.startup_opened
            assert app.query_one(Composer).text == "hi"
            assert not backend.prompts
            assert backend.create_session_calls == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("reduced", [False, True])
def test_no_motion_initializes_immediately(tmp_path: Path, reduced: bool) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.animations = reduced
        app = NagentsApp(cast("Harness", backend), startup=True)
        if reduced:
            app.animation_level = "none"
        async with app.run_test() as pilot:
            await pilot.pause()
            app._animate_activity()
            await pilot.pause()
            assert app.startup_opened
            assert backend.initialized
            assert app.default_screen.region.height == app.size.height
            assert not app._sweep.enabled

    asyncio.run(scenario())
