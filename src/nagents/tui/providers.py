"""Terminal editor for shared, environment-referenced provider connections."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import ClassVar

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Checkbox
from textual.widgets import Input
from textual.widgets import Select
from textual.widgets import Static

from nagents.harness.config import DEFAULT_HARNESS_MODEL
from nagents.harness.providers import KINDS
from nagents.harness.providers import LiveProfile
from nagents.harness.providers import ProviderProfile

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType


class ProviderEditor(ModalScreen[tuple[str, ProviderProfile] | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", show=False)]

    def __init__(self, name: str = "", profile: ProviderProfile | None = None) -> None:
        super().__init__()
        self.connection_name = name
        self.profile = profile or ProviderProfile(
            kind="openai", model=DEFAULT_HARNESS_MODEL, auth="auto", live=LiveProfile(backend_mode="assistant")
        )

    def compose(self) -> ComposeResult:
        value = self.profile
        with Vertical(classes="dialog provider-key-dialog"):
            yield Static("PROVIDER CONNECTION", classes="dialog-title", markup=False)
            with VerticalScroll(classes="provider-key-content"):
                yield Static(
                    "Connections are shared with ngn serve. Keys are read from the environment when used; no key is stored here.",
                    markup=False,
                )
                yield Static("Connection name", markup=False)
                yield Input(self.connection_name, id="provider-name", disabled=bool(self.connection_name))
                yield Static("Provider type", markup=False)
                yield Select([(kind.label, name) for name, kind in KINDS.items()], value=value.kind, id="provider-kind")
                yield Static("Authentication", markup=False)
                yield Select([(mode, mode) for mode in KINDS[value.kind].auth], value=value.auth, id="provider-auth")
                yield Static("Model ID", markup=False)
                yield Input(value.model, id="provider-model")
                yield Static("HTTP API", markup=False)
                yield Select([(api, api) for api in KINDS[value.kind].apis], value=value.api, id="provider-api")
                yield Static("API prefix URL", id="provider-url-help", markup=False)
                yield Input(value.base_url, id="provider-url")
                yield Static("API key environment variable (NAME or ${NAME})", markup=False)
                yield Input(value.api_key_env, placeholder=KINDS[value.kind].env, id="provider-env")
                yield Static("Azure API version (versioned route only)", markup=False)
                yield Input(value.api_version, id="provider-version")
                yield Static("Entra token scope (Foundry only)", markup=False)
                yield Input(value.scope, id="provider-scope")
                yield Checkbox("Enable GPT-Live", value=value.live.enabled, id="provider-live")
                yield Static("Voice backend mode", markup=False)
                yield Select(
                    [("Main assistant", "assistant"), ("Separate hosted backend", "hosted")],
                    value=value.live.backend_mode,
                    id="provider-backend-mode",
                )
                yield Static("Live voice model", markup=False)
                yield Input(value.live.model, id="provider-live-model")
                yield Static("Live hosted backend model", markup=False)
                yield Input(value.live.backend_model, id="provider-backend-model")
                yield Static("Live voice", markup=False)
                yield Select(
                    [(voice, voice) for voice in ("marin", "cedar")], value=value.live.voice, id="provider-voice"
                )
                yield Static("", id="provider-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Save", id="provider-save", variant="primary")
                yield Button("Cancel", id="provider-cancel")

    def on_mount(self) -> None:
        required = KINDS[self.profile.kind].endpoint_required
        self.query_one("#provider-url-help", Static).display = required
        self.query_one("#provider-url", Input).display = required
        self.query_one("#provider-api", Select).disabled = self.profile.kind == "openai" and self.profile.auth in {
            "auto",
            "chatgpt",
            "codex",
        }
        self.query_one("#provider-backend-mode", Select).disabled = not KINDS[self.profile.kind].live

    @on(Select.Changed, "#provider-kind")
    def changed_kind(self, event: Select.Changed) -> None:
        kind = event.value
        if not isinstance(kind, str) or kind not in KINDS:
            return
        auth = self.query_one("#provider-auth", Select)
        auth.set_options([(mode, mode) for mode in KINDS[kind].auth])
        auth.value = KINDS[kind].auth[0]
        api = self.query_one("#provider-api", Select)
        api.set_options([(mode, mode) for mode in KINDS[kind].apis])
        api.value = KINDS[kind].apis[0]
        self.query_one("#provider-env", Input).placeholder = KINDS[kind].env
        self.query_one("#provider-url-help", Static).display = KINDS[kind].endpoint_required
        url = self.query_one("#provider-url", Input)
        url.display = KINDS[kind].endpoint_required
        if not url.display:
            url.value = ""
        mode = self.query_one("#provider-backend-mode", Select)
        mode.disabled = not KINDS[kind].live
        mode.value = "assistant" if KINDS[kind].live else "hosted"
        enabled = self.query_one("#provider-live", Checkbox)
        enabled.disabled = not KINDS[kind].live
        if enabled.disabled:
            enabled.value = False

    @on(Select.Changed, "#provider-auth")
    def changed_auth(self, event: Select.Changed) -> None:
        kind = self.query_one("#provider-kind", Select).value
        self.query_one("#provider-env", Input).disabled = event.value not in {"api-key", "auto"} and kind != "openai"
        api = self.query_one("#provider-api", Select)
        api.disabled = kind == "openai" and event.value in {"auto", "chatgpt", "codex"}
        if api.disabled:
            api.value = "auto"

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "provider-save":
            self.dismiss(None)
            return

        def input_value(id: str) -> str:
            return self.query_one(f"#{id}", Input).value.strip()

        kind = self.query_one("#provider-kind", Select).value
        auth = self.query_one("#provider-auth", Select).value
        api = self.query_one("#provider-api", Select).value
        voice = self.query_one("#provider-voice", Select).value
        backend_mode = self.query_one("#provider-backend-mode", Select).value
        if not all(isinstance(item, str) for item in (kind, auth, api, voice, backend_mode)):
            return
        assert isinstance(kind, str) and isinstance(auth, str) and isinstance(api, str)
        assert isinstance(voice, str) and isinstance(backend_mode, str)
        name = input_value("provider-name")
        try:
            profile = ProviderProfile(
                kind=kind,
                auth=auth,
                api=api,
                model=input_value("provider-model"),
                base_url=input_value("provider-url"),
                api_key_env=input_value("provider-env") if auth in {"auto", "api-key"} or kind == "openai" else "",
                api_version=input_value("provider-version"),
                scope=input_value("provider-scope"),
                live=LiveProfile(
                    enabled=self.query_one("#provider-live", Checkbox).value,
                    model=input_value("provider-live-model"),
                    backend_model=input_value("provider-backend-model"),
                    voice=voice,
                    backend_mode=backend_mode,
                ),
            )
            profile.validate()
        except ValueError as error:
            self.query_one("#provider-error", Static).update(str(error))
            return
        self.dismiss((name, profile))
