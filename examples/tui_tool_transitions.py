"""Animated, offline compact tool rows. Run: poetry run python -m examples.tui_tool_transitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.color import Color
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Collapsible
from textual.widgets import Footer
from textual.widgets import Static

from nagents.tui.themes import configure_theme

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult
    from textual.binding import BindingType


@dataclass(frozen=True)
class Fixture:
    name: str
    target: str
    arguments: dict[str, object]
    output: str
    language: str = "text"
    failed: bool = False


FIXTURES = (
    Fixture(
        "read_file",
        "src/demo.py:1-6",
        {"path": "src/demo.py", "start_line": 1, "limit": 6},
        'from pathlib import Path\n\ndef greeting(name: str) -> str:\n    return f"Hello, {name}"\n',
        "python",
    ),
    Fixture(
        "search",
        '"ToolCallEvent" in src/',
        {"query": "ToolCallEvent", "path": "src"},
        "src/agent.py:51: from .events import ToolCallEvent\nsrc/tools.py:82: yield ToolCallEvent(...)\n2 matches in 2 files",
    ),
    Fixture(
        "edit",
        "src/demo.py",
        {"path": "src/demo.py", "old": 'return "hello"', "new": 'return "welcome"'},
        '--- src/demo.py\n+++ src/demo.py\n@@ -1 +1 @@\n-return "hello"\n+return "welcome"',
        "diff",
    ),
    Fixture(
        "shell",
        "pytest tests/tui -q",
        {"command": "pytest tests/tui -q", "timeout": 30},
        "Collecting tests…\ntest_tool_status PASSED\ntest_tool_timer PASSED\n\n2 passed in 4.0s\nExit code: 0",
    ),
    Fixture(
        "mcp.catalog.lookup",
        "MCP / catalog",
        {"query": "terminal tools", "filters": {"language": "python", "limit": 2}},
        json.dumps(
            {
                "content": [{"type": "text", "text": "Two matches [literal brackets]"}],
                "structuredContent": {"items": [{"id": "tool-01", "score": 0.98}, {"id": "tool-02", "score": 0.87}]},
                "isError": False,
            },
            indent=2,
        ),
        "json",
    ),
    Fixture(
        "custom_weather",
        "Custom / JSON fallback",
        {"city": "Tokyo", "units": "celsius"},
        json.dumps({"city": "Tokyo", "temperature": 21, "conditions": ["clear"], "source": "demo"}, indent=2),
        "json",
    ),
    Fixture(
        "mcp.catalog.lookup",
        "MCP / failed response",
        {"query": "restricted collection"},
        json.dumps(
            {
                "content": [{"type": "text", "text": "Permission denied: collection requires authorization"}],
                "isError": True,
            },
            indent=2,
        ),
        "json",
        True,
    ),
)
STATES = ("writing", "queued", "waiting", "running", "complete")
DURATIONS = (2.4, 1.5, 2.0, 4.0)
HOVER_OPTIONS = ("Subtle background tint", "Bold header", "Tint + bold name")
ARRIVAL_GAP = 2.2
INK = {
    "writing": "magenta",
    "queued": "dim",
    "waiting": "yellow",
    "running": "cyan",
    "complete": "green",
    "failed": "red",
}
ICONS = {"writing": "✎", "queued": "·", "waiting": "?", "running": "◌", "complete": "✓", "failed": "✕"}


def state_at(seconds: float, failed: bool = False) -> tuple[str, float]:
    """Return phase and phase-local time; execution time excludes queue/approval."""
    elapsed = max(0.0, seconds)
    for state, duration in zip(STATES, DURATIONS, strict=False):
        if elapsed < duration:
            return state, elapsed
        elapsed -= duration
    return ("failed" if failed else "complete"), DURATIONS[-1]


def clock(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{remainder:04.1f}"


def smooth(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return value * value * value * (value * (value * 6 - 15) + 10)


def foreground_mix(base: str, accent: str, amount: float) -> str:
    """Interpolate foreground only; never blend/paint the terminal background."""
    start = Color.parse(base).get_truecolor()
    end = Color.parse(accent).get_truecolor()
    amount = min(1.0, max(0.0, amount))
    return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in zip(start, end, strict=True))


class LiveRow(Collapsible):
    """Keep the same mounted row, focus and expansion throughout every tick."""

    def __init__(self, fixture: Fixture) -> None:
        self.fixture = fixture
        self.preview = Static(markup=False, classes="preview")
        self.trail = Static(markup=False, classes="trail")
        super().__init__(self.preview, self.trail, title=fixture.name, collapsed=True)
        self.last_preview: tuple[str, int] = ("", -1)
        self.current_frame = (0.0, 0)
        self.reduced_motion = False
        self.hover_level = 0.0
        self.hover_from = 0.0
        self.hover_target = 0.0
        self.hover_started = monotonic()
        self.hover_base = "#888888"
        self.hover_accent = "#b5a5df"
        self.hover_option = 0

    def _watch_title(self, title: str) -> None:
        # Rich and Textual use different markup parsers. Convert through Text
        # so literal brackets never become Textual tags or swallow name spans.
        self._title.label = Content.from_rich_text(Text.from_markup(title))

    def on_mount(self) -> None:
        self.set_interval(1 / 30, self.refresh_interaction)

    def on_enter(self, event: events.Enter) -> None:
        self.start_hover(True)

    def on_leave(self, event: events.Leave) -> None:
        self.start_hover(False)

    def start_hover(self, entered: bool) -> None:
        self.hover_target = float(entered)
        # Immediate visible feedback, then ease the remaining contrast. Mouse
        # events—not a polling interval—own the first rendered hover frame.
        self.hover_level = 0.6 if entered else 0.0
        self.hover_from = self.hover_level
        self.hover_started = monotonic()
        self.update_frame(*self.current_frame)

    def refresh_interaction(self) -> None:
        target = float(self.is_mouse_over)
        if target != self.hover_target:
            self.hover_from = self.hover_level
            self.hover_target = target
            self.hover_started = monotonic()
        progress = 1.0 if self.reduced_motion else smooth((monotonic() - self.hover_started) / 0.24)
        level = self.hover_from + (self.hover_target - self.hover_from) * progress
        if abs(level - self.hover_level) > 0.001 or (target and not self.reduced_motion):
            self.hover_level = level
            self.update_frame(*self.current_frame)

    def on_resize(self) -> None:
        self.update_frame(*self.current_frame)

    def update_frame(self, seconds: float, frame: int) -> None:
        self.current_frame = (seconds, frame)
        state, local = state_at(seconds, self.fixture.failed)
        args = json.dumps(self.fixture.arguments, indent=2, ensure_ascii=False)
        count = min(len(args), int(len(args) * local / DURATIONS[0])) if state == "writing" else len(args)
        symbol = ICONS[state]
        # Phase-local timing avoids every parallel tool pulsing in lockstep.
        if state in ("writing", "running") and not self.reduced_motion:
            sequence = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            symbol = sequence[int(local * 8) % len(sequence)]
        # Reserve the same cells for name, state and elapsed time in every phase.
        width = max(1, self.content_size.width - 2)
        timer = f"30s / {clock(local):>7}" if self.fixture.name == "shell" else f"{clock(local):>13}"
        suffix = Text(f" {state:<8}  {timer}", style="red" if state == "failed" else "dim")
        title = Text()
        title.append(f"{symbol} ", style=INK[state])
        # Retain fixed name/target offsets across every hover treatment.
        title.append(" ")
        title.append(self.fixture.name, style="dim")
        title.append(" ")
        title.append(f"  {self.fixture.target}", style="dim")
        terminal = state in ("complete", "failed")
        base = self.hover_base
        # Decorative palette is independent of execution state; arguments,
        # targets, status and timers never inherit the shimmer's colors.
        accent = "#81c7be"
        length = len(self.fixture.name)
        # Hover owns a wall-clock animation even after simulation completion
        # or pause; execution elapsed time and the frozen timer stay intact.
        hover_age = max(0.0, monotonic() - self.hover_started)
        animation_time = hover_age if terminal and self.hover_target else local
        head = -4 + smooth((animation_time % 1.8) / 1.8) * (length + 12)
        envelope = smooth(animation_time / 0.24)
        for index in range(length):
            wave = (
                smooth(1 - abs(index - head) / 5)
                if (state in ("writing", "running") or bool(self.hover_target)) and not self.reduced_motion
                else 0.0
            )
            tint = foreground_mix(accent, self.hover_accent, smooth((index - head + 5) / 10))
            color = foreground_mix(base, tint, wave * envelope)
            title.stylize("not dim " + color, 3 + index, 4 + index)
        title.truncate(max(0, width - suffix.cell_len - 1), overflow="ellipsis")
        gap_width = max(1, width - title.cell_len - suffix.cell_len)
        gap = Text(" " * gap_width)
        title.append_text(gap)
        title.append_text(suffix)
        title.stylize("red" if state == "failed" else "dim", len(title) - len(suffix), len(title))
        if self.hover_option == 2 and self.hover_level > 0:
            title.stylize("bold", 3, min(len(title), 3 + length))
        # Native title :hover styles provide immediate full-header feedback,
        # independent of simulation clocks, completion, polling or Rich markup.
        self.set_class(self.hover_option in (0, 2), "tint-hover")
        self.set_class(self.hover_option == 1, "bold-hover")
        self.set_class(self.hover_option == 2, "name-bold-hover")
        self.set_class(self.hover_level > 0.5, "hovered")
        # Flatten overlapping Rich spans before serialization: both parsers
        # must see the winning style, including name weight and shimmer.
        self.title = "".join(
            f"[{segment.style}]{escape(segment.text)}[/]" if segment.style else escape(segment.text)
            for segment in title.render(self.app.console)
        )
        terminal = state in ("complete", "failed")
        self.trail.display = True
        if not terminal:
            notes = {
                "writing": "Receiving arguments",
                "queued": "Waiting for a slot",
                "waiting": "Approval pending (simulation)",
                "running": "Receiving output",
            }
            self.trail.update(Text("↳ " + notes[state], style="dim"))
        if terminal:
            receipts = {
                "read_file": "Read source · Python preview available",
                "search": "2 matches in 2 files",
                "edit": "Change applied · inspect diff above",
                "shell": "2 passed · exit 0",
                "custom_weather": "Tokyo · 21°C · clear",
            }
            receipt = (
                "Permission denied · inspect error above"
                if self.fixture.failed
                else receipts.get(self.fixture.name, "2 catalog items · structured JSON available")
            )
            self.trail.update(Text("↳ " + receipt, style="red" if self.fixture.failed else "dim"))
        # Expanded output is not part of the header hover treatment.
        # Preview contrast remains constant in all three options.
        self.preview.styles.opacity = 0.7
        self.set_class(state == "failed", "failed")
        self.set_class(state == "waiting", "waiting")
        # Update only changing bodies; syntax-highlight complete JSON, never partial JSON.
        lines = self.fixture.output.splitlines()
        visible = max(1, int(local / DURATIONS[-1] * len(lines))) if state == "running" else len(lines)
        key = (state, count if state == "writing" else visible)
        if key == self.last_preview:
            return
        self.last_preview = key
        compact_args = " ".join(args[:count].split())
        if state == "writing":
            self.preview.update(Text(compact_args + "▏", style="dim"))
        elif state == "queued":
            self.preview.update(Text("Waiting for execution slot · " + compact_args, style="dim"))
        elif state == "waiting":
            self.preview.update(Text("Approval pending (simulated) · " + compact_args, style="dim"))
        else:
            available = lines[:visible]
            limit = 3
            body = "\n".join(available[:limit])
            if len(available) > limit:
                body += f"\n… {len(available) - limit} more lines (mock output)"
            language = "text" if state == "running" and self.fixture.language == "json" else self.fixture.language
            self.preview.update(
                Syntax(
                    body,
                    language,
                    theme="ansi_dark",
                    background_color="default",
                    word_wrap=True,
                    line_numbers=self.fixture.name == "read_file",
                )
            )


class TransitionStudy(App[None]):
    CSS = """
    Screen { background: transparent; }
    #heading, #help { height: auto; padding: 0 1; }
    #help { color: $text-muted; }
    #rows { padding: 0 1; }
    LiveRow { height: auto; margin: 0; padding: 0; border: none; background: transparent; }
    CollapsibleTitle { padding: 0; background: transparent; }
    CollapsibleTitle:hover { background: transparent; text-style: none; }
    CollapsibleTitle:focus { background: transparent; text-style: underline; }
    LiveRow.tint-hover CollapsibleTitle:hover { background: #819db0 12%; }
    LiveRow.bold-hover CollapsibleTitle:hover { text-style: bold; }
    Contents { padding: 0; background: transparent; }
    .preview { height: 3; max-height: 3; overflow: hidden; padding: 0 0 0 2;
        background: transparent; opacity: 70%; transition: opacity 180ms; }
    .trail { height: 1; padding-left: 2; color: $text-muted;
        text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "hover(0)", "Subtle tint"),
        Binding("2", "hover(1)", "Bold header"),
        Binding("3", "hover(2)", "Tint + name"),
        Binding("m", "motion", "Reduced motion"),
        Binding("space", "pause", "Pause/play"),
        Binding("r", "replay", "Replay"),
        Binding("n", "next", "Next tool"),
        Binding("a", "all", "Replay scenario"),
        Binding("e", "expand", "Details"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        configure_theme(self, "terminal", background="terminal")
        self.hover_option = 0
        self.index = 0
        self.parallel = True
        self.elapsed = 0.0
        self.playing = True
        self.expanded = False
        self.frame = 0
        self.reduced_motion = False

    def compose(self) -> ComposeResult:
        yield Static(id="heading", markup=False)
        yield Static(
            "SIMULATED · No tools execute.\n1: Subtle tint · 2: Bold header · 3: Tint + bold name\nHover a tool to compare. Click/Enter: details · E: all details",
            id="help",
            markup=False,
        )
        yield VerticalScroll(id="rows")
        yield Footer()

    async def on_mount(self) -> None:
        await self.mount_rows()
        self.set_interval(1 / 30, self.tick)

    async def mount_rows(self) -> None:
        container = self.query_one("#rows", VerticalScroll)
        await container.remove_children()
        for fixture in FIXTURES if self.parallel else (FIXTURES[self.index],):
            row = LiveRow(fixture)
            row.collapsed = not self.expanded
            await container.mount(row)
        self.elapsed = 0
        self.draw_frame()

    def draw_frame(self) -> None:
        self.query_one("#heading", Static).update(
            f"Specular shimmer / Hover {self.hover_option + 1}: {HOVER_OPTIONS[self.hover_option]} / {'playing' if self.playing else 'paused'}"
        )
        completed = 0
        arrived = 0
        for index, row in enumerate(self.query(LiveRow)):
            start = index * ARRIVAL_GAP if self.parallel else 0
            row.display = self.elapsed >= start
            if not row.display:
                continue
            arrived += 1
            local = self.elapsed - start
            state = state_at(local, row.fixture.failed)[0]
            completed += state in ("complete", "failed")
            row.reduced_motion = self.reduced_motion
            row.hover_option = self.hover_option
            row.update_frame(local, self.frame)
        self.query_one("#help", Static).update(
            f"SIMULATED · {arrived} added · {arrived - completed} active · {completed} finished\n"
            "1: Subtle tint · 2: Bold header · 3: Tint + bold name\n"
            "Hover a tool · Click/Enter: details · E: all details · R: replay · Space: pause"
        )

    def tick(self) -> None:
        if self.playing and self.elapsed < 26.0 and self.query("#heading"):
            self.elapsed = min(26.0, self.elapsed + 1 / 30)
            self.frame += 1
            self.draw_frame()

    def action_hover(self, option: int) -> None:
        self.hover_option = option
        # Apply the selected native hover style under a stationary pointer.
        for row in self.query(LiveRow):
            row.hover_started = monotonic()
            row.hover_from = row.hover_level
        self.draw_frame()

    def action_motion(self) -> None:
        self.reduced_motion = not self.reduced_motion
        self.draw_frame()

    def action_pause(self) -> None:
        self.playing = not self.playing
        self.draw_frame()

    def action_replay(self) -> None:
        self.elapsed = 0
        self.playing = True
        self.draw_frame()

    async def action_next(self) -> None:
        self.index = (self.index + 1) % len(FIXTURES)
        self.query_one("#rows", VerticalScroll).scroll_to_widget(list(self.query(LiveRow))[self.index])

    async def action_all(self) -> None:
        self.action_replay()

    def action_expand(self) -> None:
        self.expanded = not self.expanded
        for row in self.query(LiveRow):
            row.collapsed = not self.expanded


if __name__ == "__main__":
    TransitionStudy().run()
