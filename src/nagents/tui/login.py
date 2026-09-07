"""Ephemeral device-login surfaces. Credentials and polling belong to the harness."""

from __future__ import annotations

from math import ceil
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Static

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.binding import BindingType

    from nagents.harness.auth import DeviceAuthorization

DEVICE_URL = "https://auth.openai.com/codex/device"


class LoginMethodModal(ModalScreen[str]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,ctrl+c", "dismiss", show=False, priority=True)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog login-method-dialog"):
            yield Static("SIGN IN / CHOOSE ACCESS", classes="dialog-title", markup=False)
            with VerticalScroll(classes="login-method-content"):
                yield Static(
                    "ChatGPT / Codex\nUse eligible ChatGPT subscription access for Codex. "
                    "This is not OAuth access to the general OpenAI API.\n\n"
                    "Enable device code login in ChatGPT security settings. "
                    "Your workspace administrator may also need to allow it.\n\n"
                    "API key\nUsage-based provider API billing is separate from your ChatGPT subscription. "
                    "This version only explains environment setup; it never accepts or stores an API key here.",
                    markup=False,
                )
            yield Button("ChatGPT / Codex device login", id="login-chatgpt")
            yield Button("API-key environment instructions", id="login-api-key")
            yield Button("Cancel", id="login-choice-cancel")

    def on_mount(self) -> None:
        self.query_one("#login-choice-cancel", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "login-chatgpt":
            self.dismiss("chatgpt")
        elif event.button.id == "login-api-key":
            self.dismiss("api-key")
        else:
            self.dismiss()


class DeviceLoginModal(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,ctrl+c", "cancel_login", show=False, priority=True),
    ]

    def __init__(self, cancel: Callable[[], None]) -> None:
        super().__init__()
        self._cancel = cancel
        self._user_code = ""
        self._expires_at = 0.0
        self.cancel_requested = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog device-login-dialog"):
            yield Static("CHATGPT / CODEX DEVICE LOGIN", classes="dialog-title", markup=False)
            with VerticalScroll(classes="device-login-content"):
                yield Static("Complete sign-in yourself in your browser. Only enter this code on:", markup=False)
                yield Static(DEVICE_URL, id="device-url", markup=False)
                yield Static("", id="device-code", markup=False)
                yield Static("", id="device-time", markup=False)
                yield Static("Requesting code...", id="device-status", markup=False)
                yield Static(
                    "Keep the code private. ngn will not add it to the conversation.",
                    classes="dialog-hint",
                    markup=False,
                )
            with Horizontal(classes="dialog-actions device-actions"):
                yield Button("Open Browser", id="device-browser", disabled=True)
                yield Button("Copy Code", id="device-copy", disabled=True)
                yield Button("Cancel", id="device-cancel")

    def on_mount(self) -> None:
        self.query_one("#device-cancel", Button).focus()
        self.set_interval(1, self.tick)

    def show_code(self, authorization: DeviceAuthorization) -> float:
        if authorization.verification_url != DEVICE_URL:
            raise ValueError("Unexpected device verification URL")
        # Never retain the authorization object or inspect its private device_auth_id.
        self._user_code = authorization.user_code
        remaining = max(0.0, min(900.0, authorization.expires_at - monotonic()))
        self._expires_at = monotonic() + remaining
        self.query_one("#device-code", Static).update(self._user_code)
        self.query_one("#device-status", Static).update("Waiting for approval in your browser...")
        self.query_one("#device-browser", Button).disabled = not bool(self._user_code)
        self.query_one("#device-copy", Button).disabled = not bool(self._user_code)
        self.tick()
        return remaining

    def tick(self) -> None:
        if not self._expires_at or self.cancel_requested:
            return
        remaining = max(0, min(900, ceil(self._expires_at - monotonic())))
        self.query_one("#device-time", Static).update(f"Expires in {remaining // 60:02d}:{remaining % 60:02d}")
        if remaining == 0:
            self.clear_code()
            self.query_one("#device-status", Static).update("Code expired. Stopping login...")

    def clear_code(self) -> None:
        if self._user_code and self._user_code in self.app.clipboard:
            # Forget Textual's paste buffer, not the OS clipboard the user explicitly copied to.
            self.app._clipboard = ""
        self._user_code = ""
        self._expires_at = 0.0
        for widget in self.query("#device-code, #device-time").results(Static):
            widget.update("")
        for button in self.query("#device-browser, #device-copy").results(Button):
            button.disabled = True

    def action_cancel_login(self) -> None:
        self.cancel_requested = True
        self.clear_code()
        self.query_one("#device-status", Static).update("Cancelling login...")
        self.query_one("#device-cancel", Button).disabled = True
        self._cancel()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "device-cancel":
            self.action_cancel_login()
        elif self._user_code and not self.cancel_requested and monotonic() < self._expires_at:
            if event.button.id == "device-browser":
                try:
                    self.app.open_url(DEVICE_URL)
                except Exception:
                    self.query_one("#device-status", Static).update("Browser unavailable. Open the address above.")
            elif event.button.id == "device-copy":
                try:
                    self.app.copy_to_clipboard(self._user_code)
                except Exception:
                    self.query_one("#device-status", Static).update("Clipboard unavailable. Enter the code yourself.")
                else:
                    self.query_one("#device-status", Static).update("Copy requested. Waiting for browser approval...")
