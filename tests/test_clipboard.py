"""Clipboard tests never read or overwrite the developer's desktop clipboard."""

from __future__ import annotations

import asyncio
import base64
import shutil
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest
from textual.events import MouseDown
from textual.events import MouseMove
from textual.events import MouseUp
from textual.events import TextSelected
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from nagents.tui import clipboard as clipboard_module
from nagents.tui.clipboard import copy_native
from nagents.tui.widgets import Composer
from nagents.tui.widgets import Turn
from tests.test_tui import FakeHarness
from tests.test_tui import idle
from tests.test_tui import make_app
from tests.test_tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_drag_copies_only_on_release_and_empty_click_preserves_clipboard(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            await app._add(Static("alpha beta gamma", id="copy-source", markup=False))
            await pilot.pause()
            app.copy_to_clipboard("unchanged")
            await pilot.mouse_down("#copy-source", offset=(0, 0))
            await pilot.hover("#copy-source", offset=(4, 0))
            assert app.clipboard == "unchanged"
            await pilot.mouse_up("#copy-source", offset=(4, 0))
            await pilot.pause()
            assert app.clipboard == "alpha"
            await pilot.click("#copy-source", offset=(9, 0))
            await pilot.pause()
            assert app.clipboard == "alpha"

    asyncio.run(scenario())


def test_multiline_selection_and_double_click_copy_plain_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            text = "alpha\n  beta\n\u03bb = 1"
            source = Static(text, id="copy-source", markup=False)
            await app._add(source)
            await pilot.pause()
            await pilot.mouse_down(source, offset=(0, 0))
            await pilot.hover(source, offset=(6, 1))
            await pilot.mouse_up(source, offset=(6, 1))
            await pilot.pause()
            assert app.clipboard == "alpha\n  beta"
            await pilot.click(source, offset=(2, 0), times=2)
            await pilot.pause()
            assert app.clipboard == text

    asyncio.run(scenario())


def test_keyboard_copy_wins_over_interrupt_without_breaking_paste(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "wait")
            composer = app.query_one(Composer)
            composer.load_text("selected draft")
            composer.move_cursor((0, len(composer.text)))
            app.copy_to_clipboard("external paste")
            await pilot.press("shift+home")
            assert composer.selected_text == "selected draft"
            assert app.clipboard == "external paste"  # Keyboard selection must not clobber a paste.
            await pilot.press("ctrl+c")
            assert app.clipboard == "selected draft" and app.busy and not backend.cancelled
            app.copy_to_clipboard("external paste")
            await pilot.press("ctrl+v")
            assert composer.text == "external paste"
            await pilot.press("ctrl+c")
            await idle(app, pilot)
            assert backend.cancelled

    asyncio.run(scenario())


def test_output_keyboard_copy_and_background_updates(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            source = Static("result text", markup=False)
            await app._add(source)
            await pilot.pause()
            app.screen.selections = {source: Selection.from_offsets(Offset(0, 0), Offset(6, 0))}
            await pilot.press("ctrl+shift+c")
            assert app.clipboard == "result"
            source.update("result changed in background")
            await pilot.pause()
            assert app.clipboard == "result"
            app.screen.clear_selection()
            await pilot.press("ctrl+shift+c")
            assert app.clipboard == "result" and not app._interrupt_at

    asyncio.run(scenario())


def test_mouse_selection_in_composer_copies_and_selection_does_not_run_commands(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("alpha beta")
            await pilot.pause()
            offset = composer.content_region.offset - composer.region.offset
            await pilot.mouse_down(composer, offset=offset)
            await pilot.hover(composer, offset=Offset(offset.x + 5, offset.y))
            await pilot.mouse_up(composer, offset=Offset(offset.x + 5, offset.y))
            await pilot.pause()
            assert app.clipboard == "alpha"
            composer.load_text("/")
            composer.move_cursor((0, 1))
            await pilot.press("down", "down")
            assert app.clipboard == "alpha"
            assert not backend.prompts

    asyncio.run(scenario())


@pytest.mark.parametrize("editor", [False, True])
def test_mouse_input_burst_copies_final_selection(tmp_path: Path, editor: bool) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            source: Composer | Static
            if editor:
                source = app.query_one(Composer)
                source.load_text("alpha beta")
            else:
                source = Static("alpha beta", markup=False)
                await app._add(source)
            await pilot.pause()
            x, y = source.content_region.offset
            for event in (
                MouseDown(None, x, y, 0, 0, 1, False, False, False),
                MouseMove(None, x + 5, y, 5, 0, 1, False, False, False),
                MouseUp(None, x + 5, y, 0, 0, 1, False, False, False),
            ):
                app.post_message(event)
            # Raw messages and deferred refresh callbacks drain on separate ticks.
            async with asyncio.timeout(2):
                while not app.clipboard:
                    await pilot.pause()
            assert app.clipboard == ("alpha" if editor else "alpha "), (
                source.selected_text if isinstance(source, Composer) else app.screen.get_selected_text(),
                app._mouse_selection_editor,
                app.mouse_captured,
            )

    asyncio.run(scenario())


def test_code_selection_copies_code_without_highlight_markup(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await app._add(Turn("assistant", "```python\ndef greet():\n    return 'hello'\n```"))
            await pilot.pause()
            fence = app.query_one("MarkdownFence")
            app.screen.selections = {fence.query_one("#code-content"): Selection(None, None)}
            app.screen.post_message(TextSelected())
            async with asyncio.timeout(2):
                while not app.clipboard:
                    await pilot.pause()
            assert "def greet():\n    return 'hello'" in app.clipboard
            assert "\x1b" not in app.clipboard and "```" not in app.clipboard

    asyncio.run(scenario())


def test_native_copy_coalesces_pending_requests_and_does_not_restore_cleared_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        entered, release = asyncio.Event(), asyncio.Event()
        copies: list[str] = []

        async def native(text: str, workspace: Path) -> bool:
            copies.append(text)
            if text == "first":
                entered.set()
                await release.wait()
            return True

        monkeypatch.setattr("nagents.tui.app.copy_native", native)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            with monkeypatch.context() as patch:
                # The headless driver still discards OSC 52; native I/O is fake.
                patch.setattr(type(app), "is_headless", property(lambda self: False))
                app.copy_to_clipboard("first")
                await entered.wait()
                app.copy_to_clipboard("intermediate")
                app.copy_to_clipboard("last")
                app._clipboard = ""  # Device-login cleanup intentionally forgets its local copy.
                release.set()
                assert app._clipboard_task is not None
                await app._clipboard_task
            assert copies == ["first", "last"]
            assert app.clipboard == "" and app._clipboard_pending is None

    asyncio.run(scenario())


def test_headless_copy_never_touches_desktop_clipboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(text: str, workspace: Path) -> bool:
        pytest.fail("Headless tests must never invoke desktop clipboard helpers")

    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        monkeypatch.setattr("nagents.tui.app.copy_native", forbidden)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            app.copy_to_clipboard("synthetic clipboard")
            await pilot.pause()
            assert app.clipboard == "synthetic clipboard" and app._clipboard_task is None

    asyncio.run(scenario())


def test_terminal_copy_is_base64_encoded_and_preserves_unicode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            output: list[str] = []
            assert app._driver is not None
            with monkeypatch.context() as patch:
                patch.setattr(app._driver, "write", output.append)
                app.copy_to_clipboard("\u03bb\n  quoted 'text'")
            encoded = base64.b64encode("\u03bb\n  quoted 'text'".encode()).decode()
            assert output == [f"\x1b]52;c;{encoded}\a"]

    asyncio.run(scenario())


class ClipboardProcess:
    def __init__(self, *, code: int = 0, blocked: bool = False) -> None:
        self.returncode: int | None = None
        self.code = code
        self.blocked = blocked
        self.input = b""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.killed = False
        self.waited = False

    async def communicate(self, data: bytes) -> tuple[bytes, bytes]:
        self.input = data
        self.entered.set()
        if self.blocked:
            await self.release.wait()
        self.returncode = self.code
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.release.set()

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


@pytest.fixture
def clipboard_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path.parent / "native-copy"))


@pytest.mark.usefixtures("clipboard_environment")
@pytest.mark.parametrize(
    "platform,display,expected",
    [
        ("darwin", "", ""),
        ("linux", "WAYLAND_DISPLAY", "--type"),
        ("linux", "DISPLAY", "-selection"),
        ("win32", "", "-NoProfile"),
    ],
)
def test_native_clipboard_uses_stdin_not_shell_or_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    display: str,
    expected: str,
) -> None:
    process = ClipboardProcess()
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(clipboard_module, "sys", SimpleNamespace(platform=platform))
    if display:
        monkeypatch.setenv(display, "test-display")

    async def start(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        calls.append((args, kwargs))
        return cast("asyncio.subprocess.Process", process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    text = "\u03bb\n  code; $(not-a-command)"
    assert asyncio.run(copy_native(text, tmp_path))
    assert process.input == text.encode("utf-8")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert not expected or expected in args
    assert text not in str(args)
    assert kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.DEVNULL


@pytest.mark.usefixtures("clipboard_environment")
@pytest.mark.parametrize("reason", ["ssh", "workspace-helper", "no-display", "missing-tool"])
def test_native_copy_does_not_run_remote_or_workspace_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    monkeypatch.setattr(clipboard_module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setenv("DISPLAY", "test-display")
    if reason == "ssh":
        monkeypatch.setenv("SSH_CONNECTION", "test-ssh")
    elif reason == "workspace-helper":
        monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "xclip"))
    elif reason == "no-display":
        monkeypatch.delenv("DISPLAY")
    else:
        monkeypatch.setattr(shutil, "which", lambda name: None)

    async def forbidden(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        pytest.fail("No native clipboard process should start")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    assert not asyncio.run(copy_native("synthetic clipboard", tmp_path))


@pytest.mark.usefixtures("clipboard_environment")
@pytest.mark.parametrize("cancel", [False, True])
def test_native_timeout_and_cancellation_reap_owned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    monkeypatch.setattr(clipboard_module, "sys", SimpleNamespace(platform="darwin"))

    async def scenario() -> None:
        process = ClipboardProcess(blocked=True)

        async def start(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            return cast("asyncio.subprocess.Process", process)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
        task = asyncio.create_task(copy_native("synthetic clipboard", tmp_path))
        await process.entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert not await task
        assert process.killed and process.waited

    asyncio.run(scenario())


@pytest.mark.usefixtures("clipboard_environment")
def test_cancel_during_native_spawn_still_reaps_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clipboard_module, "sys", SimpleNamespace(platform="darwin"))

    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        process = ClipboardProcess()

        async def start(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            started.set()
            await release.wait()
            return cast("asyncio.subprocess.Process", process)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
        task = asyncio.create_task(copy_native("synthetic clipboard", tmp_path))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.waited
        assert not process.input

    asyncio.run(scenario())
