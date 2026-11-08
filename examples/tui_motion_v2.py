"""Quieter TUI studies using shimmer, procedural dots and interpolated light."""

from __future__ import annotations

import argparse
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.widgets import Button
from textual.widgets import Static

from examples.tui_effects import EFFECTS
from examples.tui_effects import Effect
from examples.tui_effects import effect_text
from examples.tui_effects import particle_field
from examples.tui_motion import LABELS
from examples.tui_motion import DemoState
from examples.tui_motion import Phase
from examples.tui_motion import duration
from nagents.tui.themes import configure_theme

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.types import CSSPathType


class QuietMotion(App[None]):
    TITLE = "ngn · motion studies II"
    CSS_PATH: ClassVar[CSSPathType | None] = "tui_motion_v2.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "effect('shimmer')", "Shimmer", priority=True),
        Binding("2", "effect('field')", "Dot field", priority=True),
        Binding("3", "effect('breathe')", "Breathe", priority=True),
        Binding("m", "main_state", "Main state", priority=True),
        Binding("s", "child_state", "Subagent state", priority=True),
        Binding("w", "wake", "Wake +20s", priority=True),
        Binding("d", "due", "Due now", priority=True),
        Binding("e", "details", "Agent details", priority=True),
        Binding("l", "log", "Timings", priority=True),
        Binding("space", "motion", "Motion on/off", priority=True),
        Binding("0", "intro", "Intro", priority=True),
        Binding("q,ctrl+c,ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, effect: Effect = "shimmer", *, theme: str = "terminal", motion: bool = True) -> None:
        super().__init__()
        configure_theme(self, theme)
        self.effect = effect
        self.native = theme == "terminal"
        self.motion_enabled = motion
        self.state = DemoState()
        self.state.main_since = self.state.child_since = self.state.started
        self.state.schedule(self.state.started)
        self._last_events = ""
        self._intro_until = 0.0
        self._intro_fading = False

    def compose(self) -> ComposeResult:
        yield Static("ngn  /  motion studies II", id="brand", markup=False)
        with Horizontal(id="effects"):
            yield Button("1 Shimmer", id="shimmer")
            yield Button("2 Dot field", id="field")
            yield Button("3 Breathe", id="breathe")
        with VerticalScroll(id="conversation"):
            yield Static("You", classes="byline", markup=False)
            yield Static("Help me review these options and check the notes we saved.", classes="prose", markup=False)
            yield Static("assistant", classes="byline", markup=False)
            yield Static(
                "I'll go through the notes. Mini is checking the details in parallel.", classes="prose", markup=False
            )
            yield Static("mini · read_file: notes.md", id="delegation", markup=False)
            with Vertical(id="agent-details"):
                yield Static(id="details", markup=False)
            with Vertical(id="timings"):
                yield Static("UTC timestamp                       elapsed      event", classes="muted", markup=False)
                yield Static(id="timing-lines", markup=False)
        with Vertical(id="message") as message:
            message.border_title = "Message"
            yield Static("What should we work on?", markup=False)
        with Vertical(id="status-area"):
            yield Static(id="main-status", classes="main-status", markup=False)
            yield Static(id="child-status", classes="child-status", markup=False)
            yield Static(id="wake-status", classes="wake-status", markup=False)
            yield Static("openai · gpt-6.1-sol", id="provider-model", markup=False)
        yield Static(
            "M main · S subagent · W wake · D due · E details\nSpace motion · 0 startup · L timings · Q quit",
            id="help",
            markup=False,
        )
        with Vertical(id="intro"):
            yield Static(id="intro-field", markup=False)
            yield Static(id="intro-name", markup=False)
            yield Static("Getting ready", id="intro-copy", markup=False)

    def on_mount(self) -> None:
        for name in ("intro", "agent-details", "timings"):
            self.query_one(f"#{name}").display = False
        self.action_effect(self.effect)
        self.set_interval(0.05, self._tick)

    def _paint(self, selector: str, text: str, elapsed: float, active: bool) -> None:
        widget = self.query_one(selector, Static)
        color, background = widget.styles.color, self.screen.styles.background
        widget.update(
            effect_text(
                text,
                elapsed,
                self.effect,
                active=active,
                motion=self.motion_enabled,
                ink=(color.r, color.g, color.b),
                background=(background.r, background.g, background.b),
                native=self.native,
            )
        )

    def _tick(self) -> None:
        now = monotonic()
        self.state.advance(now)
        elapsed = now - self.state.started
        self._paint(
            "#main-status",
            f"assistant  {LABELS[self.state.main]}  ·  {duration(now - self.state.main_since)}",
            elapsed,
            self.state.main != "idle",
        )
        self._paint(
            "#child-status",
            f"mini  {LABELS[self.state.child]}  ·  gentle cedar",
            elapsed + 0.9,
            self.state.child == "tool",
        )
        wake = (
            f"Wake-up {self.state.wake_text(now)}  ·  {self.state.reason}"
            if self.state.wake != "off"
            else "No wake-up scheduled"
        )
        wake_widget = self.query_one("#wake-status", Static)
        color, background = wake_widget.styles.color, self.screen.styles.background
        if self.state.wake == "pending":
            indicator = effect_text(
                "• ",
                elapsed / 3,
                "breathe",
                motion=self.motion_enabled,
                ink=(color.r, color.g, color.b),
                background=(background.r, background.g, background.b),
                native=self.native,
            )
        else:
            indicator = Text({"off": "· ", "queued": "◇ ", "delivered": "✓ "}[self.state.wake])
        indicator.append(wake)
        wake_widget.update(indicator)
        self.query_one("#details", Static).update(
            f"assistant   {LABELS[self.state.main]}\nmini        {LABELS[self.state.child]}\n"
            f"reviewer    Waiting for approval\n\nWake-up     {self.state.wake_text(now)}\n"
            f"Target      assistant\nReason      {self.state.reason}\nDue         {self.state.due_text()}"
        )
        events = "\n".join(self.state.events)
        if events != self._last_events:
            self.query_one("#timing-lines", Static).update(events)
            self._last_events = events
        intro = self.query_one("#intro")
        if now < self._intro_until:
            intro.display = True
            field = particle_field(elapsed, width=16, height=3) if self.effect == "field" else ""
            self.query_one("#intro-field", Static).update(field)
            self.query_one("#intro-name", Static).update(
                effect_text("n g n", elapsed, self.effect, motion=self.motion_enabled, native=self.native)
            )
            if now > self._intro_until - 0.25 and not self._intro_fading:
                self._intro_fading = True
                intro.styles.animate("opacity", 0, duration=0.25, easing="out_cubic")
        else:
            intro.display = False

    def _transition(self) -> None:
        if self.motion_enabled:
            for name in ("main-status", "child-status"):
                widget = self.query_one(f"#{name}")
                widget.styles.opacity = 0.65
                widget.styles.animate("opacity", 1.0, duration=0.3, easing="out_cubic")
        self._tick()

    def action_effect(self, effect: Effect) -> None:
        self.effect = effect
        for name in EFFECTS:
            self.query_one(f"#{name}", Button).variant = "primary" if name == effect else "default"
        self._transition()

    def action_main_state(self) -> None:
        phases: tuple[Phase, ...] = ("thinking", "tool", "responding", "idle")
        self.state.main = phases[(phases.index(self.state.main) + 1) % len(phases)]
        self.state.main_since = monotonic()
        self.state.record(self.state.main_since, f"Assistant: {self.state.main}")
        self._transition()

    def action_child_state(self) -> None:
        phases: tuple[Phase, ...] = ("tool", "waiting", "done")
        self.state.child = phases[(phases.index(self.state.child) + 1) % len(phases)]
        self.state.record(monotonic(), f"Mini: {self.state.child}")
        self._transition()

    def action_wake(self) -> None:
        self.state.schedule(monotonic())
        self._tick()

    def action_due(self) -> None:
        self.state.schedule(monotonic(), seconds=0)
        self._tick()

    def action_details(self) -> None:
        widget = self.query_one("#agent-details")
        widget.display = not widget.display

    def action_log(self) -> None:
        widget = self.query_one("#timings")
        widget.display = not widget.display

    def action_motion(self) -> None:
        self.motion_enabled = not self.motion_enabled
        self._tick()

    def action_intro(self) -> None:
        if not self.motion_enabled:
            return
        intro = self.query_one("#intro")
        intro.styles.animate("opacity", 1, duration=0.18, easing="out_cubic")
        self._intro_fading = False
        self._intro_until = monotonic() + 1.2
        self._tick()

    @on(Button.Pressed)
    def select_effect(self, event: Button.Pressed) -> None:
        for effect in EFFECTS:
            if event.button.id == effect:
                self.action_effect(effect)
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effect", choices=EFFECTS, default="shimmer")
    parser.add_argument("--theme", choices=("terminal", "graphite", "ocean", "ember"), default="terminal")
    parser.add_argument("--no-animations", action="store_true")
    args = parser.parse_args()
    app = QuietMotion(args.effect, theme=args.theme, motion=not args.no_animations)
    app.run()
    print(f"ngn motion studies II closed · {monotonic() - app.state.started:.2f}s")
    print("Preview only; no provider calls or persistent logs.")


if __name__ == "__main__":
    main()
