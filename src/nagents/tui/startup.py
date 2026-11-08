"""The compact, versioned ngn introduction."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.color import Color
from rich.style import Style
from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.widgets import Static

from nagents._version import framework_version

from .activity import StateField
from .lifecycle import Lifecycle
from .lifecycle import ease

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from .activity import Stops


def startup_title(label: str, stops: Stops) -> Text:
    title = Text()
    for letter, color in zip("ngn", stops, strict=True):
        title.append(letter, Style(color=Color.from_rgb(*color), bold=True))
    title.append(f"  {label}", style="dim")
    return title


class StartupApp(App[bool]):
    """A compact launch signature. It never initializes or calls the harness."""

    TITLE = "ngn"
    CSS_PATH = "startup.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter,space", "continue", "Continue", priority=True),
        Binding("ctrl+c,ctrl+q,escape", "cancel", "Exit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__(ansi_color=True)
        from .themes import configure_theme

        configure_theme(self, "terminal", background="terminal")
        self.field = StateField()
        self.lifecycle = Lifecycle("sweep", "intro", intro_enabled=True)
        self._last_tick = 0.0
        self._title = startup_title(framework_version(), self.field.stops)

    def compose(self) -> ComposeResult:
        yield Static(id="startup-title", markup=False)
        yield Static(id="startup-tagline", markup=False)
        yield Static(id="startup-field", markup=False)
        yield Static(id="startup-hint", markup=False)

    def on_mount(self) -> None:
        self.lifecycle.since = self._last_tick = monotonic()
        self.set_interval(1 / 30, self._tick)
        self._tick()

    def _tick(self) -> None:
        if not self.is_running:
            return
        now = monotonic()
        self.field.advance(now - self._last_tick)
        self._last_tick = now
        self.lifecycle.advance(now)
        if self.lifecycle.phase == "opening":
            self.exit(True)
            return
        amount = ease(self.lifecycle.progress(now) * 2.5)
        self.query_one("#startup-title", Static).update(self._title[: round(len(self._title) * amount)], layout=False)
        tagline = Text("Your assistant.", style="dim")
        self.query_one("#startup-tagline", Static).update(tagline[: round(len(tagline) * amount)], layout=False)
        self.query_one("#startup-field", Static).update(
            self.field.render(width=14, height=2, aperture=self.lifecycle.aperture(now)), layout=False
        )
        self.query_one("#startup-hint", Static).update(Text("Enter to continue", style="dim"), layout=False)

    def action_continue(self) -> None:
        self.exit(True)

    def action_cancel(self) -> None:
        self.exit(False)
