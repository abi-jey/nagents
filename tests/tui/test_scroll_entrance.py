"""The normal-screen prelude redraws only its own newly allocated rows."""

from __future__ import annotations

import asyncio
import io
import os
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.tui import NagentsApp
from nagents.tui.entrance import _draw
from tests.support.tui import FakeHarness

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nagents.harness import Harness


@pytest.mark.parametrize("key,opened", [("\r", True), (" ", True), ("\x1b", False), ("\x03", False), ("\x11", False)])
def test_skip_and_cancel_append_only(monkeypatch: pytest.MonkeyPatch, key: str, opened: bool) -> None:
    output = io.StringIO("previous shell output\n")
    output.seek(0, 2)
    monkeypatch.setattr("sys.stdout", output)
    assert asyncio.run(_draw(lambda: key)) is opened
    text = output.getvalue()
    assert text.startswith("previous shell output\n")
    assert "\x1b[4A" not in text
    assert "\x1b[2J" not in text
    assert text.endswith("+\n\x1b[?25h")


def test_expansion_then_reveal_only_allocated_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    now = [0.0]
    monkeypatch.setattr("nagents.tui.entrance.monotonic", lambda: now[0])

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("nagents.tui.entrance.asyncio.sleep", sleep)
    assert asyncio.run(_draw(lambda: ""))
    allocated, revealed = output.getvalue().split("\x1b7")
    assert len(allocated.splitlines()) == 6  # blank, top, three rows, bottom
    assert "Opening workspace..." not in allocated
    assert revealed.count("\x1b[4A") == 3
    assert "Opening workspace..." in revealed
    assert "\x1b[2J" not in revealed
    assert revealed.endswith("\x1b8\x1b[?25h")
    assert 0.32 <= now[0] < 0.35


def test_draw_exception_restores_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)

    def fail() -> str:
        raise RuntimeError("input failed")

    with pytest.raises(RuntimeError, match="input failed"):
        asyncio.run(_draw(fail))
    assert output.getvalue().endswith("\x1b[?25h")


@pytest.mark.parametrize("size", [(18, 24), (80, 6)])
def test_small_terminal_skips_redraw(monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]) -> None:
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr("nagents.tui.entrance.shutil.get_terminal_size", lambda: os.terminal_size(size))
    assert asyncio.run(_draw(lambda: ""))
    assert not output.getvalue()


def test_preinitialized_task_is_consumed_once_on_same_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = NagentsApp(cast("Harness", backend))
        owner = asyncio.get_running_loop()

        async def initialize() -> None:
            assert asyncio.get_running_loop() is owner
            await backend.initialize()

        app._preinitialized = asyncio.create_task(initialize())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._backend_ready
            assert backend.create_session_calls == [True]
            assert asyncio.get_running_loop() is owner
        assert backend.closed

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX prelude input")
@pytest.mark.parametrize("cancel", [False, True])
def test_terminal_handoff_and_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    import pty
    import termios

    from nagents.tui.entrance import run_tui

    async def scenario() -> None:
        master, slave = pty.openpty()
        stream = os.fdopen(os.dup(slave), "w")
        before = termios.tcgetattr(slave)
        backend = FakeHarness(tmp_path)
        app = NagentsApp(cast("Harness", backend))
        owner = asyncio.get_running_loop()
        started = asyncio.Event()
        cleaned = asyncio.Event()
        runs: list[bool] = []

        async def initialize(*, create_session: bool = True) -> None:
            backend.create_session_calls.append(create_session)
            assert asyncio.get_running_loop() is owner
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        async def draw(read_key: Callable[[], str]) -> bool:
            await started.wait()
            assert termios.tcgetattr(slave) != before
            os.write(master, "hé".encode())
            await asyncio.sleep(0.02)
            assert read_key() == "h"
            return not cancel

        async def run_async() -> None:
            assert asyncio.get_running_loop() is owner
            assert termios.tcgetattr(slave) == before
            assert app.startup_opened
            assert app._entrance_text == "hé"
            assert app._startup_complete.is_set()
            assert app._motion_lifecycle.phase == "ready"
            assert not app._sweep.enabled
            runs.append(True)
            await app._shutdown()

        monkeypatch.setattr("sys.stdin", stream)
        monkeypatch.setattr("sys.stdout", stream)
        monkeypatch.setattr("nagents.tui.entrance._draw", draw)
        monkeypatch.setattr(backend, "initialize", initialize)
        monkeypatch.setattr(app, "run_async", run_async)
        try:
            await run_tui(app)
            assert runs == ([] if cancel else [True])
            assert backend.create_session_calls == [True]
            assert backend.closed
            assert cleaned.is_set()
            assert termios.tcgetattr(slave) == before
        finally:
            stream.close()
            os.close(master)
            os.close(slave)

    asyncio.run(scenario())
