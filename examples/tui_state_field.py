"""A native-background playground for continuous, state-driven dot-field gradients."""

from __future__ import annotations

import argparse
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.widgets import Button
from textual.widgets import Static

from examples.tui_motion import duration
from nagents._version import framework_version
from nagents.tui.activity import STATES
from nagents.tui.activity import StateField
from nagents.tui.activity import StateName
from nagents.tui.lifecycle import TRANSITIONS
from nagents.tui.lifecycle import Lifecycle
from nagents.tui.lifecycle import Transition
from nagents.tui.lifecycle import ease
from nagents.tui.startup import startup_title
from nagents.tui.themes import configure_theme

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

CONTROLS: tuple[tuple[str, StateName, str], ...] = (
    ("0", "starting", "Start"),
    ("1", "working", "Work"),
    ("2", "tool", "Tool"),
    ("3", "delegating", "Delegate"),
    ("4", "delegated", "Parallel"),
    ("5", "returned", "Return"),
    ("6", "approval", "Approval"),
    ("7", "sleeping", "Wait"),
    ("8", "waking", "Wake"),
    ("9", "responding", "Reply"),
    ("i", "idle", "Idle"),
    ("c", "done", "Done"),
    ("x", "error", "Error"),
)
SCENARIO: tuple[tuple[float, StateName, StateName], ...] = (
    (0, "working", "idle"),
    (2.5, "tool", "idle"),
    (5.0, "delegating", "starting"),
    (7.5, "delegated", "working"),
    (10.0, "working", "tool"),
    (12.5, "approval", "working"),
    (15.0, "returned", "done"),
    (18.0, "responding", "done"),
    (21.0, "sleeping", "done"),
    (25.0, "waking", "done"),
    (28.0, "tool", "done"),
    (31.0, "done", "done"),
    (34.0, "idle", "done"),
)


class StateFieldDemo(App[None]):
    TITLE = "ngn · state field"
    CSS_PATH = "tui_state_field.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        *(Binding(key, f"state('{state}')", label, priority=True) for key, state, label in CONTROLS),
        Binding("p", "play", "Play sequence", priority=True),
        Binding("g", "gradient", "Gradient / solid", priority=True),
        Binding("s", "child", "Child state", priority=True),
        Binding("w", "state('sleeping')", "Wait for wake-up", priority=True),
        Binding("d", "due", "Wake now", priority=True),
        Binding("l", "log", "Timings", priority=True),
        Binding("space", "motion", "Motion on/off", priority=True),
        Binding("f1", "transition('sweep')", "Sweep", priority=True),
        Binding("f2", "transition('stagger')", "Stagger", priority=True),
        Binding("f3", "transition('quiet')", "Quiet", priority=True),
        Binding("r", "replay", "Replay entrance / exit", priority=True),
        Binding("enter", "continue_launch", "Continue", priority=True),
        Binding("escape", "cancel_transition", "Keep open", priority=True),
        Binding("q", "leave", "Close", priority=True),
        Binding("ctrl+c,ctrl+q", "quit", "Quit immediately", priority=True),
    ]

    def __init__(self, *, motion: bool = True, transition: Transition = "sweep", intro: bool = True) -> None:
        super().__init__()
        configure_theme(self, "terminal", background="terminal")
        self.main = StateField()
        self.child = StateField("idle", offset=1.7)
        self.motion_enabled = motion
        self.gradient = True
        self.started = self.last_tick = self.state_since = monotonic()
        self.wall_started = datetime.now(UTC)
        self.wake_due = 0.0
        self.wake_active = False
        self.playing = False
        self.sequence_since = 0.0
        self.sequence_index = -1
        self.events: list[str] = []
        self._opening = True
        self.lifecycle = Lifecycle(
            transition,
            "intro" if motion and intro else "opening" if motion else "ready",
            self.started,
            intro_enabled=motion and intro,
        )
        self.framework_version = framework_version()
        self._brand_title = startup_title(self.framework_version, self.main.stops)
        self._launch_visible = motion and intro
        self._launch_close_visibility = 1.0
        self._constant_text: dict[str, tuple[Text, float]] = {}
        self._rendered_text: dict[str, Text] = {}
        self._mode_text = Text()

    def compose(self) -> ComposeResult:
        with Vertical(id="launch"):
            yield Static(id="launch-title", markup=False)
            yield Static(id="launch-tagline", markup=False)
            yield Static(id="launch-field", markup=False)
            yield Static(id="launch-hint", markup=False)
        with Vertical(id="workspace"):
            yield from self._compose_workspace()

    def _compose_workspace(self) -> ComposeResult:
        yield Static(self._brand_title.copy(), id="brand", markup=False)
        with Vertical(id="controls"):
            for start, end in ((0, 5), (5, 9), (9, 13)):
                with Horizontal(classes="state-options"):
                    for key, state, label in CONTROLS[start:end]:
                        yield Button(f"{key} {label}", id=f"state-{state}")
            yield Static(id="mode", markup=False)
        with Vertical(id="conversation"):
            yield Static(
                "You\nReview the change; ask mini to check it.\n\nassistant\nI'll review it while mini checks the layout.",
                id="sample-chat",
                markup=False,
            )
        with Vertical(id="message"):
            yield Static(Text("Message", style="dim"), id="message-label", markup=False)
            yield Static("What should we work on?", id="message-placeholder", markup=False)
        yield Static(id="main-row", markup=False)
        yield Static(id="main-detail", markup=False)
        yield Static(id="child-row", markup=False)
        yield Static(Text("openai · gpt-6.1-sol", style="dim"), id="provider-model", markup=False)
        yield Static(
            Text(
                "F1 sweep · F2 stagger · F3 quiet · R replay\nP play · G gradient · S child · D wake now\nSpace motion · L timings · Q close · Ctrl+C quit",
                style="dim",
            ),
            id="help",
            markup=False,
        )
        yield Static(id="timings", markup=False)

    def on_mount(self) -> None:
        self.query_one("#timings").display = False
        if self.lifecycle.phase == "intro":
            self.lifecycle.since = monotonic()
        for selector, order in (
            ("brand", 0.05),
            ("sample-chat", 0.25),
            ("message-label", 0.5),
            ("message-placeholder", 0.55),
            ("provider-model", 0.12),
            ("help", 0.7),
        ):
            content = self.query_one(f"#{selector}", Static).content
            text = content.copy() if isinstance(content, Text) else Text(str(content))
            self._constant_text[selector] = text, order
        self._sync_controls()
        if not self.motion_enabled:
            self._opening = False
            self._select("working", monotonic())
        self.set_interval(1 / 30, self._tick)
        self._tick()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "continue_launch":
            return self.lifecycle.phase == "intro"
        return super().check_action(action, parameters)

    def _record(self, text: str, now: float) -> None:
        elapsed = now - self.started
        stamp = (self.wall_started + timedelta(seconds=elapsed)).isoformat(timespec="milliseconds")
        self.events.append(f"{stamp} +{elapsed:6.2f}s  {text}")
        self.events[:] = self.events[-6:]

    def _select(self, state: StateName, now: float) -> None:
        self._advance_to(now)
        self.main.select(state, immediate=not self.motion_enabled)
        self.state_since = now
        self._record(f"assistant: {state}", now)
        if state == "delegating":
            self.child.select("starting", immediate=not self.motion_enabled)
        elif state == "returned":
            self.child.select("done", immediate=not self.motion_enabled)
        self.wake_active = state == "sleeping"
        if self.wake_active:
            self.wake_due = now + 12
        self._sync_controls()

    def _sync_controls(self) -> None:
        for _, state, _ in CONTROLS:
            self.query_one(f"#state-{state}", Button).set_class(state == self.main.state, "selected")
        treatment = "Gradient follows state" if self.gradient else "Single color follows state"
        playback = " · sequence playing" if self.playing else " · click any state"
        self._mode_text = Text(
            f"{self.lifecycle.transition.title()} entrance / exit · {treatment}{playback}", style="dim"
        )

    def _tick(self) -> None:
        if not self.is_running:
            return
        now = monotonic()
        self._advance_to(now)
        if self.lifecycle.advance(now):
            self.exit()
            return
        if self.lifecycle.phase == "closed":
            return
        if self.lifecycle.phase == "intro":
            self._launch_visible = True
        elif self.lifecycle.phase in {"opening", "ready"}:
            self._launch_visible = False
        self.query_one("#launch").display = self._launch_visible
        self.query_one("#workspace").display = not self._launch_visible
        if self._launch_visible:
            self._render_launch(now)
            return
        if self.lifecycle.phase in {"opening", "ready"} and self._opening and now - self.started >= 1.5:
            self._opening = False
            if self.main.state == "starting":
                self._select("working", now)
        if self.lifecycle.phase != "closing" and self.playing:
            elapsed = now - self.sequence_since
            next_index = max(index for index, (at, _, _) in enumerate(SCENARIO) if elapsed >= at)
            if next_index != self.sequence_index:
                self.sequence_index = next_index
                _, main_state, child_state = SCENARIO[next_index]
                self._select(main_state, now)
                self.child.select(child_state, immediate=not self.motion_enabled)
                if main_state == "sleeping":
                    self.wake_due = self.sequence_since + 25
                if next_index == len(SCENARIO) - 1:
                    self.playing = False
                    self._sync_controls()
        elif self.lifecycle.phase != "closing" and self.wake_active and now >= self.wake_due:
            self._select("waking", now)
        main = self.main.render(gradient=self.gradient, aperture=self.lifecycle.aperture(now))
        main.append_text(
            self.lifecycle.text(
                Text(f"  assistant  {STATES[self.main.state].label}  ·  {duration(now - self.state_since)}"),
                now,
                order=0.0,
                accent=self.main.stops[1],
            )
        )
        self.query_one("#main-row", Static).update(main, layout=False)
        detail = STATES[self.main.state].detail
        if self.wake_active:
            due = (self.wall_started + timedelta(seconds=self.wake_due - self.started)).strftime("%H:%M:%S UTC")
            detail = f"in {duration(self.wake_due - now + 0.99)} · {detail} · {due}"
        self._reveal("main-detail", Text("         " + detail, style="dim"), now, 0.6)
        self.query_one("#main-detail", Static).tooltip = detail
        child = self.child.render(gradient=self.gradient, aperture=self.lifecycle.aperture(now))
        child.append(f"  mini       {STATES[self.child.state].label}")
        self._reveal("child-row", child, now, 0.65)
        self._reveal("timings", Text("\n".join(self.events), style="dim"), now, 0.8)
        self._reveal("mode", self._mode_text, now, 0.3)
        for selector, (text, order) in self._constant_text.items():
            self._reveal(selector, text, now, order)
        for index, (_, state, _) in enumerate(CONTROLS):
            self.query_one(f"#state-{state}", Button).styles.visibility = (
                "visible" if self.lifecycle.amount(now, 0.1 + index * 0.04) >= 0.5 else "hidden"
            )

    def _reveal(self, selector: str, text: Text, now: float, order: float) -> None:
        # The wordmark stays anchored while the selected sweep reveals the interface below it.
        rendered = (
            text
            if selector == "brand" and self.lifecycle.intro_enabled and self.lifecycle.phase == "opening"
            else self.lifecycle.text(text, now, order=order, accent=self.main.stops[1])
        )
        if self._rendered_text.get(selector) != rendered:
            self.query_one(f"#{selector}", Static).update(rendered, layout=selector == "timings")
            self._rendered_text[selector] = rendered

    def _launch_amount(self, now: float) -> float:
        progress = self.lifecycle.progress(now)
        if self.lifecycle.phase == "closing":
            return self._launch_close_visibility * (1 - ease(progress))
        return ease(progress * 2.5)

    def _render_launch(self, now: float) -> None:
        amount = self._launch_amount(now)
        title = self._brand_title[: round(len(self._brand_title) * amount)]
        self.query_one("#launch-title", Static).update(title, layout=False)
        tagline = Text("Your assistant.", style="dim")
        self.query_one("#launch-tagline", Static).update(tagline[: round(len(tagline) * amount)], layout=False)
        field = self.main.render(width=14, height=2, gradient=self.gradient, aperture=self.lifecycle.aperture(now))
        self.query_one("#launch-field", Static).update(field, layout=False)
        hint = Text("TUI motion preview · Enter to continue", style="dim")
        self.query_one("#launch-hint", Static).update(hint[: round(len(hint) * amount)], layout=False)

    def _begin_close(self, now: float, *, replay: bool = False) -> None:
        if self._launch_visible:
            self._launch_close_visibility = self._launch_amount(now)
        self.lifecycle.begin("closing", now, replay=replay)

    def _advance_to(self, now: float) -> None:
        if self.motion_enabled:
            self.main.advance(now - self.last_tick)
            self.child.advance(now - self.last_tick)
        self.last_tick = now

    def action_state(self, state: StateName) -> None:
        self.action_cancel_transition()
        self.playing, self._opening = False, False
        self._select(state, monotonic())
        self._tick()

    def action_child(self) -> None:
        self.action_cancel_transition()
        self.playing = False
        self._advance_to(monotonic())
        phases: tuple[StateName, ...] = ("starting", "working", "tool", "approval", "responding", "done", "idle")
        state = phases[(phases.index(self.child.state) + 1) % len(phases)]
        self.child.select(state, immediate=not self.motion_enabled)
        self._record(f"mini: {state}", monotonic())
        self._sync_controls()
        self._tick()

    def action_play(self) -> None:
        self.action_cancel_transition()
        self.playing = not self.playing
        self._opening = False
        if self.playing:
            self.sequence_since = monotonic()
            self.sequence_index = -1
        self._sync_controls()
        self._tick()

    def action_due(self) -> None:
        self.action_state("waking")

    def action_gradient(self) -> None:
        self.action_cancel_transition()
        self.gradient = not self.gradient
        self._sync_controls()
        self._tick()

    def action_motion(self) -> None:
        self.action_cancel_transition()
        self.motion_enabled = not self.motion_enabled
        self.main.select(self.main.state, immediate=not self.motion_enabled)
        self.child.select(self.child.state, immediate=not self.motion_enabled)
        self.last_tick = monotonic()
        self._tick()

    def action_log(self) -> None:
        self.action_cancel_transition()
        widget = self.query_one("#timings")
        widget.display = not widget.display

    def action_leave(self) -> None:
        if not self.motion_enabled or self.lifecycle.phase in {"closing", "closed"}:
            self.exit()
            return
        self.playing = False
        self._begin_close(monotonic())
        self._tick()

    def action_replay(self) -> None:
        if not self.motion_enabled:
            return
        self.playing, self._opening = False, False
        self._begin_close(monotonic(), replay=True)
        self._tick()

    def action_transition(self, transition: Transition) -> None:
        if self.motion_enabled:
            self._begin_close(monotonic(), replay=True)
            self.playing, self._opening = False, False
        self.lifecycle.transition = transition
        self._sync_controls()
        self._tick()

    def action_cancel_transition(self) -> None:
        self.lifecycle.phase, self.lifecycle.replay = "ready", False

    def action_continue_launch(self) -> None:
        if self.lifecycle.phase == "intro":
            self.lifecycle.begin("opening", monotonic())
            self._tick()

    @on(Button.Pressed)
    def select_state(self, event: Button.Pressed) -> None:
        for _, state, _ in CONTROLS:
            if event.button.id == f"state-{state}":
                self.action_state(state)
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fullscreen", action="store_true", help="Use full-screen mode instead of inline mode")
    parser.add_argument("--no-animations", action="store_true")
    parser.add_argument("--skip-intro", action="store_true", help="Skip the versioned introduction")
    parser.add_argument("--transition", choices=TRANSITIONS, default="sweep")
    args = parser.parse_args()
    transition = next(value for value in TRANSITIONS if value == args.transition)
    app = StateFieldDemo(motion=not args.no_animations, transition=transition, intro=not args.skip_intro)
    app.run(inline=not args.fullscreen)
    print(f"ngn · preview closed · {duration(monotonic() - app.started)}")


if __name__ == "__main__":
    main()
