"""Interactive TUI motion studies. No providers, credentials, or workspace writes.

Run from the repository: python -m examples.tui_motion
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from math import ceil
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Literal

from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.widgets import Button
from textual.widgets import Static

from nagents.tui.themes import configure_theme

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

Design = Literal["network", "orbit", "pulse"]
Phase = Literal["thinking", "tool", "responding", "idle", "waiting", "done"]
DESIGNS: tuple[Design, ...] = ("network", "orbit", "pulse")
FRAMES: dict[Design, tuple[str, ...]] = {
    "network": ("●─○─○", "○•○─○", "○─●─○", "○─○•○", "○─○─●", "○─○•○", "○─●─○", "○•○─○"),
    "orbit": ("⠋ ●", "⠙ ●", "⠹ ●", "⠸ ●", "⠼ ●", "⠴ ●", "⠦ ●", "⠧ ●", "⠇ ●", "⠏ ●"),
    "pulse": ("● · ·", "○ ● ·", "· ○ ●", "· · ○", "· · ●", "· ● ○", "● ○ ·", "○ · ·"),
}
LABELS: dict[Phase, str] = {
    "thinking": "Thinking through the options",
    "tool": "read_file: notes.md",
    "responding": "Writing the response",
    "idle": "Ready",
    "waiting": "Waiting for approval",
    "done": "Returned the findings",
}


def motion_frame(design: Design, elapsed: float, phase: Phase, *, animate: bool = True) -> str:
    if phase in {"idle", "waiting", "done"}:
        return {"idle": "○", "waiting": "◇", "done": "✓"}[phase].ljust(7)
    frames = FRAMES[design]
    speed = {"thinking": 6, "tool": 9, "responding": 4}[phase]
    return frames[int(max(0, elapsed) * speed) % len(frames) if animate else 0].ljust(7)


def duration(seconds: float) -> str:
    value = max(0, int(seconds))
    return f"{value // 60:02}:{value % 60:02}"


@dataclass
class DemoState:
    started: float = field(default_factory=monotonic)
    wall_started: datetime = field(default_factory=lambda: datetime.now(UTC))
    main: Phase = "thinking"
    child: Phase = "tool"
    main_since: float = 0
    child_since: float = 0
    wake: Literal["off", "pending", "queued", "delivered"] = "off"
    deadline: float = 0
    reason: str = "Check CI results and report back"
    events: list[str] = field(default_factory=list)

    def record(self, now: float, message: str) -> None:
        elapsed = max(0, now - self.started)
        stamp = (self.wall_started + timedelta(seconds=elapsed)).isoformat(timespec="milliseconds")
        self.events.append(f"{stamp}  +{elapsed:7.3f}s  {message}")
        self.events[:] = self.events[-8:]

    def schedule(self, now: float, seconds: float = 20) -> None:
        self.deadline = now + max(0, seconds)
        self.wake = "pending"
        self.record(now, f"Wake-up scheduled: {self.reason}")

    def advance(self, now: float) -> None:
        if self.wake == "pending" and now >= self.deadline:
            self.wake = "queued"
            self.record(now, "Wake-up due; waiting for assistant's next turn")
        if self.wake == "queued" and self.main == "idle":
            self.wake, self.main, self.main_since = "delivered", "thinking", now
            self.record(now, "Assistant resumed by wake-up")

    def wake_text(self, now: float) -> str:
        if self.wake == "pending":
            return f"in {duration(ceil(self.deadline - now))}"
        return {"off": "None scheduled", "queued": "Due · queued for next turn", "delivered": "Delivered to assistant"}[
            self.wake
        ]

    def due_text(self) -> str:
        due = self.wall_started + timedelta(seconds=self.deadline - self.started)
        return due.strftime("%H:%M:%S UTC") if self.wake != "off" else "—"


class MotionStudies(App[None]):
    TITLE = "ngn · TUI motion studies"
    CSS_PATH = "tui_motion.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "design('network')", "Network", priority=True),
        Binding("2", "design('orbit')", "Orbit", priority=True),
        Binding("3", "design('pulse')", "Pulse", priority=True),
        Binding("m", "main_state", "Main state", priority=True),
        Binding("s", "child_state", "Subagent state", priority=True),
        Binding("w", "wake", "Schedule wake-up", priority=True),
        Binding("d", "due", "Wake now", priority=True),
        Binding("x", "clear_wake", "Clear wake-up", priority=True),
        Binding("space", "motion", "Motion on/off", priority=True),
        Binding("0", "startup", "Replay startup", priority=True),
        Binding("l", "log", "Timing log", priority=True),
        Binding("q,ctrl+q,ctrl+c", "quit", "Quit", priority=True),
    ]

    def __init__(self, design: Design = "network", *, theme: str = "terminal", animations: bool = True) -> None:
        super().__init__()
        configure_theme(self, theme)
        self.design = design
        self.motion_enabled = animations
        self.state = DemoState()
        self.state.main_since = self.state.child_since = self.state.started
        self.state.schedule(self.state.started)
        self._last_log = ""
        self._splash_until = 0.0

    def compose(self) -> ComposeResult:
        yield Static("ngn  /  motion studies", id="brand", markup=False)
        with Horizontal(id="designs"):
            yield Button("1  Network", id="network")
            yield Button("2  Orbit", id="orbit")
            yield Button("3  Pulse", id="pulse")
        with VerticalScroll(id="body"):
            yield Static("You", classes="speaker", markup=False)
            yield Static("Help me compare the options and check the notes we saved.", classes="sample", markup=False)
            yield Static("assistant", classes="speaker", markup=False)
            yield Static(
                "I'm reviewing the notes. Mini is checking the details in parallel; the reviewer is waiting for approval.",
                classes="sample",
                markup=False,
            )
            with Vertical(id="activity"):
                yield Static("ACTIVITY", classes="section-label", markup=False)
                yield Static(id="main-detail", classes="main-actor", markup=False)
                yield Static(id="child-detail", classes="child-actor", markup=False)
                yield Static("   ◇       reviewer · swift pine · waiting for approval", classes="waiting", markup=False)
                yield Static(id="wake-detail", classes="waiting", markup=False)
            with Vertical(id="events"):
                yield Static(
                    "TIMING PREVIEW  /  UTC timestamp + monotonic elapsed", classes="section-label", markup=False
                )
                yield Static(id="event-lines", markup=False)
        with Horizontal(id="state-controls"):
            yield Button("M  Assistant state", id="main-state")
            yield Button("S  Subagent state", id="child-state")
            yield Button("W  Wake-up +20s", id="schedule")
        with Vertical(id="message") as message:
            message.border_title = "Message · layout preview"
            yield Static("What should we work on?", markup=False)
        with Vertical(id="below-bar"):
            with Horizontal(id="footer-states"):
                yield Static(id="main-status", classes="main-actor", markup=False)
                yield Static(id="child-status", classes="child-actor", markup=False)
                yield Static(id="wake-status", classes="waiting", markup=False)
            yield Static(
                "openai · gpt-6.1-sol    /    sample identity; no model calls", id="provider-model", markup=False
            )
        yield Static(
            "1/2/3 design · M assistant · S subagent · W timer · D due · X clear\nSpace motion · 0 startup · L timings · Q quit",
            id="keys",
            markup=False,
        )
        with Vertical(id="splash"):
            yield Static(id="startup-mark", markup=False)
            yield Static("n g n", id="startup-name", markup=False)
            yield Static("Your assistant, getting ready", id="startup-copy", markup=False)

    def on_mount(self) -> None:
        self.query_one("#events").display = False
        self.action_design(self.design)
        self.set_interval(0.125, self._tick)
        self.action_startup()
        self._tick()

    def _tick(self) -> None:
        now = monotonic()
        self.state.advance(now)
        elapsed = now - self.state.started
        main = motion_frame(self.design, elapsed, self.state.main, animate=self.motion_enabled)
        child = motion_frame(self.design, elapsed + 0.65, self.state.child, animate=self.motion_enabled)
        clock = (
            "◷      "
            if self.state.wake == "pending"
            else "◇      "
            if self.state.wake == "queued"
            else "✓      "
            if self.state.wake == "delivered"
            else "○      "
        )
        main_time, child_time = duration(now - self.state.main_since), duration(now - self.state.child_since)
        self.query_one("#main-detail", Static).update(f"{main} assistant · {LABELS[self.state.main]} · {main_time}")
        self.query_one("#child-detail", Static).update(
            f"   {child} mini · gentle cedar · {LABELS[self.state.child]} · {child_time}"
        )
        self.query_one("#wake-detail", Static).update(
            f"{clock} Wake-up · {self.state.wake_text(now)}\n"
            + (
                f"          assistant · {self.state.reason}\n          Scheduled for {self.state.due_text()}"
                if self.state.wake != "off"
                else ""
            )
        )
        main_label = LABELS[self.state.main] if self.design == "orbit" else self.state.main.capitalize()
        child_label = LABELS[self.state.child] if self.design == "orbit" else self.state.child.capitalize()
        self.query_one("#main-status", Static).update(f"{main} assistant · {main_label} · {main_time}")
        self.query_one("#child-status", Static).update(f"{child} mini · {child_label} · {child_time}")
        self.query_one("#wake-status", Static).update(f"{clock} Wake-up {self.state.wake_text(now)}")
        events = "\n".join(self.state.events)
        if events != self._last_log:
            self.query_one("#event-lines", Static).update(events)
            self._last_log = events
        splash = self.query_one("#splash")
        splash.display = now < self._splash_until
        if splash.display:
            self.query_one("#startup-mark", Static).update(motion_frame(self.design, elapsed, "thinking", animate=True))

    def action_design(self, design: Design) -> None:
        self.design = design
        for name in DESIGNS:
            self.screen.set_class(name == design, name)
            self.query_one(f"#{name}", Button).variant = "primary" if name == design else "default"
        self._tick()

    def action_main_state(self) -> None:
        phases: tuple[Phase, ...] = ("thinking", "tool", "responding", "idle")
        self.state.main = phases[(phases.index(self.state.main) + 1) % len(phases)]
        self.state.main_since = monotonic()
        self.state.record(self.state.main_since, f"Assistant: {self.state.main}")
        self._tick()

    def action_child_state(self) -> None:
        phases: tuple[Phase, ...] = ("tool", "waiting", "done")
        self.state.child = phases[(phases.index(self.state.child) + 1) % len(phases)]
        self.state.child_since = monotonic()
        self.state.record(self.state.child_since, f"Subagent gentle cedar: {self.state.child}")
        self._tick()

    def action_wake(self) -> None:
        self.state.schedule(monotonic())
        self._tick()

    def action_due(self) -> None:
        self.state.schedule(monotonic(), seconds=0)
        self._tick()

    def action_clear_wake(self) -> None:
        self.state.wake = "off"
        self.state.record(monotonic(), "Wake-up cleared")
        self._tick()

    def action_motion(self) -> None:
        self.motion_enabled = not self.motion_enabled
        self.state.record(
            monotonic(), "Animation enabled" if self.motion_enabled else "Animation paused; countdown continues"
        )
        self._tick()

    def action_startup(self) -> None:
        self._splash_until = monotonic() + 1.2 if self.motion_enabled else 0

    def action_log(self) -> None:
        widget = self.query_one("#events")
        widget.display = not widget.display

    @on(Button.Pressed)
    def chosen(self, event: Button.Pressed) -> None:
        actions = {
            "main-state": self.action_main_state,
            "child-state": self.action_child_state,
            "schedule": self.action_wake,
        }
        identifier = event.button.id or ""
        for design in DESIGNS:
            if identifier == design:
                self.action_design(design)
                return
        if identifier in actions:
            actions[identifier]()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", choices=DESIGNS, default="network")
    parser.add_argument("--theme", choices=("terminal", "graphite", "ocean", "ember"), default="terminal")
    parser.add_argument("--no-animations", action="store_true")
    args = parser.parse_args()
    app = MotionStudies(args.design, theme=args.theme, animations=not args.no_animations)
    app.run()
    print(f"ngn motion demo closed · elapsed {monotonic() - app.state.started:.2f}s")
    print("Demo only: no provider requests, saved sessions, or wire logs.")


if __name__ == "__main__":
    main()
