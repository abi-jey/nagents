"""A dot-field color playground, with a compact terminal entrance before the TUI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from math import pi
from math import sin
from time import monotonic
from time import sleep
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Literal

from rich.align import Align
from rich.console import Console
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.widgets import Button
from textual.widgets import Static

from examples.tui_effects import RGB
from examples.tui_effects import Effect
from examples.tui_effects import particle_field
from examples.tui_effects import shade
from examples.tui_motion_v2 import QuietMotion

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.types import CSSPathType

ColorName = Literal["mint", "prism", "amber", "ice"]
COLORS: tuple[ColorName, ...] = ("mint", "prism", "amber", "ice")


@dataclass(frozen=True)
class FieldPalette:
    label: str
    stops: tuple[RGB, RGB, RGB]
    ansi: str


PALETTES: dict[ColorName, FieldPalette] = {
    "mint": FieldPalette("Mint", ((96, 190, 173), (125, 231, 201), (195, 241, 216)), "cyan"),
    "prism": FieldPalette("Prism", ((112, 177, 242), (178, 155, 244), (135, 224, 215)), "magenta"),
    "amber": FieldPalette("Amber", ((235, 163, 108), (246, 208, 139), (238, 168, 167)), "yellow"),
    "ice": FieldPalette("Ice", ((129, 173, 202), (183, 219, 238), (224, 233, 245)), "blue"),
}


def field_text(
    elapsed: float,
    palette: FieldPalette,
    *,
    width: int = 7,
    height: int = 1,
    gradient: bool = True,
    motion: bool = True,
    native: bool = False,
    background: RGB = (21, 24, 32),
) -> Text:
    """Color the same particle geometry, without changing its timing or dimensions."""
    time = elapsed if motion else 0.0
    result = Text()
    for row, line in enumerate(particle_field(time, width=width, height=height).splitlines()):
        if row:
            result.append("\n")
        for column, character in enumerate(line):
            amount = 0.5 + 0.5 * sin(column / max(1, width - 1) * pi + time * 0.35) if gradient else 0.5
            segment = min(1, int(amount * 2))
            mix = amount * 2 - segment
            start, end = palette.stops[segment : segment + 2]
            ink = (
                round(start[0] + (end[0] - start[0]) * mix),
                round(start[1] + (end[1] - start[1]) * mix),
                round(start[2] + (end[2] - start[2]) * mix),
            )
            style = Style(color=palette.ansi) if native else shade(0.95, ink, background, native=False)
            result.append(character, style)
    return result


def entrance_frame(elapsed: float, seconds: float, width: int, palette: FieldPalette, *, native: bool) -> Align:
    """Grow a small block in the shell; do not clear the terminal or enter its alternate screen."""
    progress = min(1.0, max(0.0, elapsed / seconds))
    growth = progress * progress * (3 - 2 * progress)
    maximum = max(12, min(72, width - 2))
    minimum = min(24, maximum)
    panel_width = minimum + round((maximum - minimum) * growth)
    dots = field_text(
        elapsed, palette, width=max(1, panel_width - 8), height=1 if progress < 0.55 else 3, native=native
    )
    content = Group(Align.center(dots), Align.center(Text("Opening color preview", style="dim")))
    return Align.center(Panel(content, title="ngn", width=panel_width, border_style=palette.ansi, padding=(0, 1)))


def play_entrance(console: Console, palette: FieldPalette) -> None:
    """Preview-only duration: production startup would follow actual initialization."""
    started, seconds = monotonic(), 2.2
    with Live(console=console, transient=True, refresh_per_second=20, vertical_overflow="crop") as live:
        while (elapsed := monotonic() - started) < seconds:
            # The shell background is unknown; use its ANSI palette until the app paints its own surface.
            live.update(entrance_frame(elapsed, seconds, console.width, palette, native=True))
            sleep(0.05)


class DotFieldColors(QuietMotion):
    TITLE = "ngn · dot field colors"
    CSS_PATH: ClassVar[CSSPathType | None] = ["tui_motion_v2.tcss", "tui_dot_colors.tcss"]
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "color('mint')", "Mint", priority=True),
        Binding("2", "color('prism')", "Prism", priority=True),
        Binding("3", "color('amber')", "Amber", priority=True),
        Binding("4", "color('ice')", "Ice", priority=True),
        Binding("c", "next_color", "Next color", priority=True),
        Binding("g", "gradient", "Gradient / solid", priority=True),
    ]

    def __init__(self, color: ColorName = "mint", *, theme: str = "graphite", motion: bool = True) -> None:
        self.color = color
        self.gradient = True
        super().__init__("field", theme=theme, motion=motion)

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace-shell"):
            yield Static("ngn  /  dot field colors", id="brand", markup=False)
            with Horizontal(id="effects"):
                for index, color in enumerate(COLORS, start=1):
                    yield Button(f"{index} {PALETTES[color].label}", id=color)
            yield Static(id="color-mode", markup=False)
            with VerticalScroll(id="conversation"):
                yield Static("You", classes="byline", markup=False)
                yield Static(
                    "Help me review these options and check the notes we saved.", classes="prose", markup=False
                )
                yield Static("assistant", classes="byline", markup=False)
                yield Static(
                    "I'll go through the notes. Mini is checking the details in parallel.",
                    classes="prose",
                    markup=False,
                )
                yield Static("mini · read_file: notes.md", id="delegation", markup=False)
                with Vertical(id="agent-details"):
                    yield Static(id="details", markup=False)
                with Vertical(id="timings"):
                    yield Static(
                        "UTC timestamp                       elapsed      event", classes="muted", markup=False
                    )
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
                "1-4 color · G gradient · M main · S child · W wake · D due\nSpace motion · E details · L timings · 0 entrance · Q quit",
                id="help",
                markup=False,
            )
        with Vertical(id="intro"):
            yield Static(id="intro-field", markup=False)
            yield Static(id="intro-name", markup=False)
            yield Static("Opening color preview", id="intro-copy", markup=False)

    def on_mount(self) -> None:
        # Textual dispatches the base class's mount handler as well.
        if self.motion_enabled:
            shell = self.query_one("#workspace-shell")
            shell.styles.opacity = 0.3
            shell.styles.animate("opacity", 1.0, duration=0.6, easing="out_cubic")

    def _field(self, elapsed: float, *, width: int = 7, height: int = 1) -> Text:
        background = self.screen.styles.background
        return field_text(
            elapsed,
            PALETTES[self.color],
            width=width,
            height=height,
            gradient=self.gradient,
            motion=self.motion_enabled,
            native=self.native,
            background=(background.r, background.g, background.b),
        )

    def _paint(self, selector: str, text: str, elapsed: float, active: bool) -> None:
        dots = self._field(elapsed) if active else Text("   ·   ", style=PALETTES[self.color].ansi)
        dots.append(" " + text)
        self.query_one(selector, Static).update(dots)

    def _tick(self) -> None:
        super()._tick()
        if self.query_one("#intro").display:
            elapsed = monotonic() - self.state.started
            self.query_one("#intro-field", Static).update(self._field(elapsed, width=16, height=3))
            self.query_one("#intro-name", Static).update(Text("n g n", style="bold"))

    def _sync_colors(self) -> None:
        for color in COLORS:
            self.query_one(f"#{color}", Button).variant = "primary" if self.color == color else "default"
        treatment = "Flowing gradient" if self.gradient else "Single color"
        suffix = " · ANSI color fallback" if self.native else ""
        self.query_one("#color-mode", Static).update(f"{PALETTES[self.color].label} · {treatment}{suffix}")

    def action_effect(self, effect: Effect) -> None:
        self.effect = "field"
        self._sync_colors()
        self._tick()

    def action_color(self, color: ColorName) -> None:
        self.color = color
        self._sync_colors()
        self._tick()

    def action_next_color(self) -> None:
        self.action_color(COLORS[(COLORS.index(self.color) + 1) % len(COLORS)])

    def action_gradient(self) -> None:
        self.gradient = not self.gradient
        self._sync_colors()
        self._tick()

    def action_intro(self) -> None:
        if not self.motion_enabled:
            return
        intro = self.query_one("#intro")
        intro.styles.animate("opacity", 1, duration=0.18, easing="out_cubic")
        intro.styles.height = 5
        intro.styles.width = 26
        intro.styles.animate("height", self.size.height, duration=0.8, easing="in_out_cubic")
        intro.styles.animate("width", self.size.width, duration=0.8, easing="in_out_cubic")
        self._intro_fading = False
        self._intro_until = monotonic() + 1.5
        self._tick()

    @on(Button.Pressed)
    def select_color(self, event: Button.Pressed) -> None:
        for color in COLORS:
            if event.button.id == color:
                self.action_color(color)
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--color", choices=COLORS, default="mint")
    parser.add_argument("--theme", choices=("terminal", "graphite", "ocean", "ember"), default="graphite")
    parser.add_argument("--no-animations", action="store_true")
    parser.add_argument("--skip-entrance", action="store_true")
    args = parser.parse_args()
    color = next(value for value in COLORS if value == args.color)
    console = Console()
    try:
        if console.is_terminal and not args.no_animations and not args.skip_entrance:
            play_entrance(console, PALETTES[color])
        app = DotFieldColors(color, theme=args.theme, motion=not args.no_animations)
        app.run()
    except KeyboardInterrupt:
        console.print("Color preview closed.")
        return
    console.print("Color preview closed. Sample state only; no provider calls or persistent logs.")


if __name__ == "__main__":
    main()
