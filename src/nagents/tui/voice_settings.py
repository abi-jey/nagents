"""Edit canonical Voice preferences without replacing inherited fields/prompts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import ClassVar

from textual import on
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import Select
from textual.widgets import Static
from textual.widgets import Switch

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.events import Resize


@dataclass(frozen=True)
class VoiceEdits:
    snapshot: dict[str, object]
    changes: dict[str, object]
    reset: bool = False


class VoiceSettingsModal(ModalScreen[VoiceEdits]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss", "Back")]

    def __init__(self, snapshot: dict[str, object]) -> None:
        super().__init__()
        self.snapshot = snapshot
        values = snapshot.get("values")
        if not isinstance(values, dict):
            raise ValueError("Voice preferences could not be read")
        self.values: dict[str, object] = values

    def compose(self) -> ComposeResult:
        values = self.values
        connections = self.snapshot.get("connections", [])
        options = [("Follow active provider", "")]
        if isinstance(connections, list):
            options += [
                (f"{item['name']} / {item.get('provider', '')}", str(item["name"]))
                for item in connections
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
        selected = str(values.get("connection_id", ""))
        if selected not in {value for _, value in options}:
            options.append((selected + " (unavailable)", selected))
        with Vertical(classes="dialog voice-settings-dialog"):
            scope = str(self.snapshot.get("scope", "agent"))
            label = str(self.snapshot.get("agent", ""))
            yield Static(f"VOICE SETTINGS / {scope} {label}".rstrip(), classes="dialog-title", markup=False)
            with VerticalScroll(id="voice-settings-content"):
                yield Static("Enable Voice (changes apply to the next call)", markup=False)
                yield Switch(bool(values["enabled"]), id="voice-enabled")
                yield Static("Voice provider connection (separate from the assistant model)", markup=False)
                yield Select(options, value=selected, allow_blank=False, id="voice-connection")
                yield Static("Live model", markup=False)
                yield Input(str(values["model"]), id="voice-model")
                yield Static("Voice (unsupported voices fall back to the connection's default)", markup=False)
                voices = (
                    "marin",
                    "cedar",
                    "arbor",
                    "breeze",
                    "cove",
                    "ember",
                    "juniper",
                    "maple",
                    "sol",
                    "spruce",
                    "vale",
                )
                yield Select(
                    [(voice, voice) for voice in voices], value=str(values["voice"]), allow_blank=False, id="voice-name"
                )
                yield Static("Startup context (text only; excludes tools, attachments and reasoning)", markup=False)
                yield Select(
                    [(name, name) for name in ("recent", "summary", "none")],
                    value=str(values["context_mode"]),
                    allow_blank=False,
                    id="voice-context",
                )
                yield Static("Backend / Main assistant retains this chat's tools and approval rules", markup=False)
                yield Select(
                    [("Main assistant", "assistant"), ("Hosted Responses (no workspace tools)", "hosted")],
                    value=str(values["backend_mode"]),
                    allow_blank=False,
                    id="voice-backend",
                )
                yield Static("Hosted backend model (API-key mode only)", markup=False)
                yield Input(str(values["backend_model"]), id="voice-backend-model")
                path = self.snapshot.get("preference_path", "the assistant.md for this scope")
                yield Static(
                    f"Instructions inherit from live.prompt in {path}. This form preserves them.", markup=False
                )
                yield Static(
                    "Microphone/speaker devices belong to the machine running ngn. Use headphones to prevent echo.",
                    markup=False,
                )
            yield Static(
                "Changed fields only · Reset = inherit",
                classes="dialog-hint",
                markup=False,
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Back", id="voice-back")
                if scope != "global":
                    yield Button("Reset", id="voice-reset")
                yield Button("Save", id="voice-save", variant="primary")

    def on_mount(self) -> None:
        enabled = self.query_one("#voice-enabled", Switch)
        enabled.focus(scroll_visible=False)
        self._resize_voice_form(self.app.size.width, self.app.size.height)

    def on_resize(self, event: Resize) -> None:
        self._resize_voice_form(event.size.width, event.size.height)

    def _resize_voice_form(self, width: int, height: int) -> None:
        # Modal screens are siblings of the workspace, not descendants of its
        # .small class. Own responsive styling and reflow for this form.
        self.set_class(width < 72 or height < 23, "small")
        if self.focused is not None:
            self.call_after_refresh(self.focused.scroll_visible, animate=False)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "voice-back":
            self.dismiss()
        elif event.button.id == "voice-reset":
            self.dismiss(VoiceEdits(self.snapshot, {}, True))
        elif event.button.id == "voice-save":
            values: dict[str, object] = {
                "enabled": self.query_one("#voice-enabled", Switch).value,
                "connection_id": str(self.query_one("#voice-connection", Select).value),
                "model": self.query_one("#voice-model", Input).value.strip(),
                "voice": str(self.query_one("#voice-name", Select).value),
                "context_mode": str(self.query_one("#voice-context", Select).value),
                "backend_mode": str(self.query_one("#voice-backend", Select).value),
                "backend_model": self.query_one("#voice-backend-model", Input).value.strip(),
            }
            if not values["model"] or not values["backend_model"]:
                self.notify("Model IDs cannot be empty.", severity="error")
                return
            source = self.snapshot.get("global_preferences" if self.snapshot["scope"] == "global" else "overrides", {})
            changes: dict[str, object] = dict(source) if isinstance(source, dict) else {}
            if self.snapshot["scope"] == "global":
                origins = self.snapshot.get("origins")
                if not isinstance(origins, dict):
                    self.notify(
                        "Reload Voice settings before saving; preference origins are unavailable.", severity="error"
                    )
                    return
                # Global snapshots contain resolved defaults as well as saved
                # fields. Re-saving defaults must not pin a followed provider.
                changes = {key: value for key, value in changes.items() if origins.get(key) == "global"}
            changes.pop("prompt", None)  # The shared settings API preserves live.prompt separately.
            changed = {key: value for key, value in values.items() if value != self.values.get(key)}
            if self.snapshot["scope"] == "agent":
                if {"connection_id", "model"} & changed.keys():
                    provider = str(values["connection_id"] or self.snapshot.get("profile_name", ""))
                    changes["model"] = {"provider": provider, "id": values["model"]}
                changed = {key: value for key, value in changed.items() if key not in {"connection_id", "model"}}
            changes.update(changed)
            self.dismiss(VoiceEdits(self.snapshot, changes))
