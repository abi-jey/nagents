"""Ephemeral provider-login surfaces. Credentials and polling belong to the harness."""

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
from textual.widgets import Input
from textual.widgets import Static

from nagents.harness import provider_login

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.binding import BindingType

    from nagents.harness.auth import DeviceAuthorization
    from nagents.harness.provider_login import LoginMethod

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
                    "OpenRouter\nSign in once in your browser; ngn stores a user-controlled API key.\n\n"
                    "API key / other provider\nUsage-based provider billing, separate from ChatGPT. "
                    "Keys are stored in ngn's private credential store and never shown in the conversation. "
                    "To reference an environment variable instead, run ngn login in a terminal.",
                    markup=False,
                )
            yield Button("ChatGPT / Codex device login", id="login-chatgpt")
            yield Button("OpenRouter browser sign-in", id="login-openrouter")
            yield Button("API key / other provider", id="login-api-key")
            yield Button("Cancel", id="login-choice-cancel")

    def on_mount(self) -> None:
        self.query_one("#login-choice-cancel", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "login-chatgpt":
            self.dismiss("chatgpt")
        elif event.button.id == "login-openrouter":
            self.dismiss("openrouter")
        elif event.button.id == "login-api-key":
            self.dismiss("api-key")
        else:
            self.dismiss()


class ProviderKeyModal(ModalScreen[tuple[str, str, str] | None]):
    """Collect a write-only API key plus optional model and base URL."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,ctrl+c", "cancel", show=False, priority=True)]

    def __init__(self, chosen: LoginMethod) -> None:
        super().__init__()
        self.chosen = chosen

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog provider-key-dialog"):
            yield Static(f"SIGN IN / {self.chosen.label.upper()}", classes="dialog-title", markup=False)
            with VerticalScroll(classes="provider-key-content"):
                yield Static(
                    "Paste the API key below. It is written only to ngn's private credential store "
                    "(POSIX 0600) and is never shown in the conversation or history.\n\n"
                    f"Environment variable: ${self.chosen.env}.",
                    markup=False,
                )
                yield Input(placeholder="API key", password=True, id="provider-key")
                if not self.chosen.default_model:
                    yield Input(placeholder="Model ID", id="provider-model")
                if self.chosen.needs_base_url:
                    yield Input(placeholder="Base URL, e.g. https://gateway.example/v1", id="provider-base-url")
                yield Static("", id="provider-key-status", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Save", id="key-save", variant="primary")
                yield Button("Cancel", id="key-cancel")

    def on_mount(self) -> None:
        self.query_one("#provider-key", Input).focus()

    def _message(self, text: str) -> None:
        self.query_one("#provider-key-status", Static).update(text)

    def submit_values(self) -> None:
        key = self.query_one("#provider-key", Input).value.strip()
        model = self.query_one("#provider-model", Input).value.strip() if not self.chosen.default_model else ""
        base_url = self.query_one("#provider-base-url", Input).value.strip() if self.chosen.needs_base_url else ""
        if not key:
            self._message("An API key is required; use ngn login in a terminal to reference an environment variable.")
            return
        if not self.chosen.default_model and not model:
            self._message("A model ID is required for this provider.")
            return
        if self.chosen.needs_base_url and not base_url:
            self._message("A base URL is required for this provider.")
            return
        self.dismiss((key, model, base_url))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.submit_values()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "key-save":
            self.submit_values()
        else:
            self.dismiss(None)


class OpenRouterLoginModal(ModalScreen[str | None]):
    """Headless PKCE: show the authorization URL and accept the pasted code."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,ctrl+c", "cancel", show=False, priority=True)]

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog openrouter-login-dialog"):
            yield Static("SIGN IN / OPENROUTER", classes="dialog-title", markup=False)
            with VerticalScroll(classes="openrouter-login-content"):
                yield Static("1. Open this address in your browser and approve ngn:", markup=False)
                yield Static(self.url, id="openrouter-url", markup=False)
                yield Static("2. OpenRouter then shows a single-use code. Paste it below.", markup=False)
                yield Input(placeholder="Authorization code", password=True, id="openrouter-code")
                yield Static("", id="openrouter-status", markup=False)
                yield Static(
                    "Only continue if you started this sign-in. The code expires in 10 minutes.",
                    classes="dialog-hint",
                    markup=False,
                )
            with Horizontal(classes="dialog-actions"):
                yield Button("Open Browser", id="openrouter-browser")
                yield Button("Copy URL", id="openrouter-copy")
                yield Button("Continue", id="openrouter-continue", variant="primary")
                yield Button("Cancel", id="openrouter-cancel")

    def on_mount(self) -> None:
        self.query_one("#openrouter-code", Input).focus()

    def _message(self, text: str) -> None:
        self.query_one("#openrouter-status", Static).update(text)

    def submit_code(self) -> None:
        code = self.query_one("#openrouter-code", Input).value.strip()
        if not provider_login.valid_code(code):
            self._message("Enter the code shown by OpenRouter; run /login again if it expired.")
            return
        self.dismiss(code)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.submit_code()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "openrouter-browser":
            try:
                self.app.open_url(self.url)
            except Exception:
                self._message("Browser unavailable. Open the address above yourself.")
        elif event.button.id == "openrouter-copy":
            try:
                self.app.copy_to_clipboard(self.url)
            except Exception:
                self._message("Clipboard unavailable. Select the address above.")
            else:
                self._message("Address copied to the clipboard.")
        elif event.button.id == "openrouter-continue":
            self.submit_code()
        else:
            self.dismiss(None)


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
