"""Device-login UI tests. All codes and credentials here are synthetic; no network."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import Static

from nagents.harness.auth import DeviceAuthorization
from nagents.tui.login import DEVICE_URL
from nagents.tui.login import DeviceLoginModal
from nagents.tui.login import LoginMethodModal
from nagents.tui.screens import ChoiceModal
from nagents.tui.screens import DetailModal
from nagents.tui.widgets import Composer

from .test_tui import FakeHarness
from .test_tui import idle
from .test_tui import make_app
from .test_tui import send

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot

    from nagents.tui import NagentsApp

USER_CODE = "TEST-1234"
PRIVATE_ID = "synthetic-private-device-auth-id"
TOKEN = "synthetic-access-token-must-not-render"


class LoginHarness(FakeHarness):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.config.demo = False
        self.login_calls = 0
        self.logout_calls = 0
        self.login_cancelled = False
        self.issue_code = asyncio.Event()
        self.callback_returned = asyncio.Event()
        self.approve_login = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.stopped = asyncio.Event()
        self.expires_in = 900.0
        self.url = DEVICE_URL
        self.failure = ""

    def auth_status(self) -> str:
        return "ChatGPT/Codex signed in" if self.config.auth == "chatgpt" else "Not signed in; API-key setup available"

    async def login(self, show_code: Callable[[DeviceAuthorization], Awaitable[None]]) -> None:
        self.login_calls += 1
        self.running = True
        try:
            await self.issue_code.wait()
            if self.failure == "request":
                raise RuntimeError(f"server body: {TOKEN}, {PRIVATE_ID}, {USER_CODE}")
            await show_code(
                DeviceAuthorization(
                    verification_url=self.url,
                    user_code=USER_CODE,
                    device_auth_id=PRIVATE_ID,
                    interval=1,
                    expires_at=monotonic() + self.expires_in,
                )
            )
            self.callback_returned.set()
            await self.approve_login.wait()
            if self.failure == "expired":
                raise TimeoutError(f"expired: {TOKEN}, {PRIVATE_ID}, {USER_CODE}")
            if self.failure == "denied":
                raise PermissionError(f"denied: {TOKEN}, {PRIVATE_ID}, {USER_CODE}")
            self.config.auth = "chatgpt"
            self.config.model = "gpt-5.3-codex"
        except asyncio.CancelledError:
            self.login_cancelled = True
            await self.cleanup_release.wait()
            raise
        finally:
            self.running = False
            self.stopped.set()

    async def logout(self) -> None:
        self.logout_calls += 1
        if self.failure == "logout":
            raise RuntimeError(f"store error: {TOKEN}, {PRIVATE_ID}, {USER_CODE}")
        self.config.auth = "api-key"


async def start_login(app: NagentsApp, pilot: Pilot[None]) -> DeviceLoginModal:
    await send(app, pilot, "/login")
    assert isinstance(app.screen, LoginMethodModal)
    assert app.screen.focused is app.screen.query_one("#login-choice-cancel", Button)
    await pilot.click("#login-chatgpt")
    await pilot.pause()
    assert isinstance(app.screen, DeviceLoginModal)
    return app.screen


def assert_private(app: NagentsApp, *, code_visible: bool = False) -> None:
    transcript = "\n".join(str(widget.content) for widget in app.query_one("#conversation").query(Static))
    assert USER_CODE not in transcript
    assert all(USER_CODE not in item for item in app.query_one(Composer).prompt_history)
    rendered = app.export_screenshot()
    assert PRIVATE_ID not in rendered
    assert TOKEN not in rendered
    if not code_visible:
        assert USER_CODE not in rendered


def test_login_choices_and_api_key_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", TOKEN)

    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            assert backend.login_calls == 0
            assert "Not signed in" in str(app.query_one("#mode", Static).content)
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, ChoiceModal)
            app.screen.query_one(Input).value = "/login"
            await pilot.pause()
            await pilot.press("enter")
            assert app.query_one(Composer).text == "/login "
            await pilot.press("enter")
            assert isinstance(app.screen, LoginMethodModal)
            text = "\n".join(str(widget.content) for widget in app.screen.query(Static))
            assert "subscription" in text
            assert "usage-based" in text.lower()
            assert "security settings" in text
            assert "not OAuth access to the general OpenAI API" in text
            assert not app.screen.query(Input)
            await pilot.click("#login-api-key")
            assert isinstance(app.screen, DetailModal)
            assert "OPENAI_API_KEY" in app.screen.text
            assert "secret manager" in app.screen.text
            assert TOKEN not in app.screen.text
            assert backend.login_calls == 0
            await pilot.press("escape")
            await send(app, pilot, "/login")
            await pilot.press("ctrl+c")
            assert not isinstance(app.screen, LoginMethodModal)
            assert not backend.closed
            assert_private(app)

    asyncio.run(scenario())


def test_device_code_callback_and_explicit_browser_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        backend.expires_in = 3600
        app = make_app(backend)
        opened: list[str] = []
        copied: list[str] = []
        monkeypatch.setattr(app, "open_url", opened.append)
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            modal = await start_login(app, pilot)
            assert modal.focused is modal.query_one("#device-cancel", Button)
            assert "Requesting code" in str(modal.query_one("#device-status", Static).content)
            assert modal.query_one("#device-browser", Button).disabled
            assert modal.query_one("#device-copy", Button).disabled
            backend.issue_code.set()
            await pilot.pause()
            assert backend.callback_returned.is_set()
            assert app.busy
            assert str(modal.query_one("#device-code", Static).content) == USER_CODE
            assert modal._expires_at <= monotonic() + 900
            assert "15:00" in str(modal.query_one("#device-time", Static).content)
            assert not opened and not copied
            assert_private(app, code_visible=True)
            await pilot.click("#device-browser")
            assert opened == [DEVICE_URL]
            await pilot.click("#device-copy")
            assert copied == [USER_CODE]
            assert modal.query_one("#device-cancel").region.bottom <= 20
            assert modal.query_one("#device-browser").region.x >= 0
            assert (
                modal.query_one("#device-status").region.bottom
                <= modal.query_one(".device-login-content").region.bottom
            )
            await pilot.press("escape")
            await idle(app, pilot)
            assert backend.login_cancelled
            assert modal._user_code == ""
            assert_private(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("after_code", [False, True])
@pytest.mark.parametrize("cancel", ["escape", "ctrl+c", "button", "dismiss"])
def test_login_cancel_awaits_cleanup(tmp_path: Path, after_code: bool, cancel: str) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        backend.cleanup_release.clear()
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            modal = await start_login(app, pilot)
            if after_code:
                backend.issue_code.set()
                await pilot.pause()
                assert backend.callback_returned.is_set()
            if cancel == "button":
                await pilot.click("#device-cancel")
            elif cancel == "dismiss":
                modal.dismiss()
                await pilot.pause()
            else:
                await pilot.press(cancel)
            assert app.busy
            assert backend.login_cancelled
            assert not backend.stopped.is_set()
            assert modal._user_code == ""
            assert_private(app)
            backend.cleanup_release.set()
            await idle(app, pilot)
            assert backend.stopped.is_set()
            assert not isinstance(app.screen, DeviceLoginModal)
            assert "Signed in" not in app.export_screenshot()
            assert not backend.prompts
        assert backend.closed

    asyncio.run(scenario())


def test_login_success_logout_and_job_guard(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(132, 38)) as pilot:
            await idle(app, pilot)
            modal = await start_login(app, pilot)
            backend.issue_code.set()
            await pilot.pause()
            app.command("/login")
            assert app.screen is modal
            assert backend.login_calls == 1
            app.command("/logout")
            assert backend.logout_calls == 0
            backend.approve_login.set()
            await idle(app, pilot)
            assert not isinstance(app.screen, DeviceLoginModal)
            assert modal._user_code == ""
            assert backend.config.auth == "chatgpt"
            assert "gpt-5.3-codex" in str(app.query_one("#model", Static).content)
            assert "signed in" in str(app.query_one("#mode", Static).content)
            assert "signed in" in str(app.query_one("#rail-auth", Static).content)
            assert not app.query_one("#status").has_class("error")
            assert any(
                "Signed in to ChatGPT/Codex" in str(widget.content) for widget in app.query(".notice").results(Static)
            )
            assert_private(app)
            await send(app, pilot, "/logout")
            await idle(app, pilot)
            assert backend.logout_calls == 1
            assert backend.config.auth == "api-key"
            assert "Not signed in" in str(app.query_one("#mode", Static).content)
            assert_private(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["request", "denied", "expired"])
def test_login_errors_do_not_echo_response_bodies(
    tmp_path: Path, failure: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        backend.failure = failure
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            modal = await start_login(app, pilot)
            backend.issue_code.set()
            backend.approve_login.set()
            await idle(app, pilot)
            assert app.query_one("#status").has_class("error")
            status = str(app.query_one("#status", Static).content)
            assert "/login" in status
            assert "expired" in status
            assert "Signed in" not in app.export_screenshot()
            assert modal._user_code == ""
            assert backend.config.auth != "chatgpt"
            assert_private(app)
        output = capsys.readouterr()
        for secret in (USER_CODE, PRIVATE_ID, TOKEN):
            assert secret not in output.out + output.err + caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("expires_in", [-1.0, 0.1])
def test_device_expiry_stops_polling(tmp_path: Path, expires_in: float) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        backend.expires_in = expires_in
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            modal = await start_login(app, pilot)
            backend.issue_code.set()
            await idle(app, pilot)
            assert backend.stopped.is_set()
            assert "expired" in str(app.query_one("#status", Static).content)
            assert app.query_one("#status").has_class("error")
            assert modal._user_code == ""
            assert_private(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("after_code", [False, True])
def test_quit_awaits_login_before_backend_close(tmp_path: Path, after_code: bool) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            modal = await start_login(app, pilot)
            if after_code:
                backend.issue_code.set()
                await pilot.pause()
            await pilot.press("ctrl+q")
        assert backend.login_cancelled
        assert backend.stopped.is_set()
        assert backend.closed
        assert modal._user_code == ""

    asyncio.run(scenario())


def test_demo_never_starts_login_and_logout_errors_are_safe(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        backend.config.demo = True
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "/login")
            await pilot.click("#login-chatgpt")
            await idle(app, pilot)
            assert backend.login_calls == 0
            assert "Restart without --demo" in str(app.query_one("#status", Static).content)
            await send(app, pilot, "/logout")
            await idle(app, pilot)
            assert backend.logout_calls == 0
            assert "Restart without --demo" in str(app.query_one("#status", Static).content)
            backend.config.demo = False
            backend.failure = "logout"
            await send(app, pilot, "/logout")
            await idle(app, pilot)
            assert app.query_one("#status").has_class("error")
            assert "permissions" in str(app.query_one("#status", Static).content)
            assert_private(app)

    asyncio.run(scenario())


def test_unexpected_verification_url_is_not_opened_or_displayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        backend.url = f"https://example.invalid/device?token={TOKEN}"
        app = make_app(backend)
        opened: list[str] = []
        monkeypatch.setattr(app, "open_url", opened.append)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await start_login(app, pilot)
            backend.issue_code.set()
            await idle(app, pilot)
            assert not opened
            assert app.query_one("#status").has_class("error")
            assert_private(app)

    asyncio.run(scenario())


def test_cancellation_forgets_textual_clipboard_copy(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = LoginHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await start_login(app, pilot)
            backend.issue_code.set()
            await pilot.pause()
            await pilot.click("#device-copy")
            assert app.clipboard == USER_CODE
            await pilot.press("escape")
            await idle(app, pilot)
            assert app.clipboard == ""
            assert_private(app)

    asyncio.run(scenario())
