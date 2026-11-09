"""Offline completion-color study. Run: poetry run python -m examples.tui_tool_completion."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.text import Text
from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

from examples.tui_tool_transitions import foreground_mix
from examples.tui_tool_transitions import smooth
from nagents.tui import themes
from nagents.tui.widgets import ToolCard

if TYPE_CHECKING:
    from textual.timer import Timer


class CompletionCard(ToolCard):
    """Experiment only: one successful completion pass, then a settled tint."""

    def __init__(self, call_id: str, tool: str, arguments: object) -> None:
        self.completion_started = 0.0
        self.completion_source = Text()
        self.completion_timer: Timer | None = None
        super().__init__(call_id, tool, arguments)

    def finish(self, result: object, error: str | None, duration_ms: float) -> None:
        previous = self._summary_text.copy()
        super().finish(result, error, duration_ms)
        if self._outcome != "complete":
            return
        # Preserve each visible character's previous foreground, including the
        # in-flight teal/lavender name shimmer, rather than flashing to gray.
        self.completion_source = previous
        self.completion_started = monotonic()
        self.completion_timer = self.set_interval(1 / 30, self.paint_completion)
        self.paint_completion()

    def paint_completion(self) -> None:
        text = self._summary_text.copy()
        age = monotonic() - self.completion_started
        duration = 1.25
        progress = min(1.0, age / duration)
        head = -5 + smooth(progress) * (len(text) + 10)
        for index in range(len(text)):
            source = self.completion_source.get_style_at_offset(self.app.console, index).color
            base = source.name if source is not None else "#888888"
            settled = smooth((head - index + 3) / 6)
            color = foreground_mix(base, "#8fae98", settled)
            gleam = smooth(1 - abs(index - head) / 5) * (1 - smooth((progress - 0.85) / 0.15))
            color = foreground_mix(color, "#b6d8bb", gleam * 0.8)
            text.stylize("not dim " + color, index, index + 1)
        self._title.set_reactive(type(self._title).label, Content.from_rich_text(text))
        self._title.mutate_reactive(type(self._title).label)
        if progress >= 1 and self.completion_timer is not None:
            self.completion_timer.stop()
            self.completion_timer = None

    def on_resize(self) -> None:
        # ToolCard updates its summary during size allocation, before this event.
        if self.completion_started:
            self.paint_completion()

    def on_unmount(self) -> None:
        super().on_unmount()
        if self.completion_timer is not None:
            self.completion_timer.stop()


class CompletionStudy(App[None]):
    CSS_PATH = themes.__file__.replace("themes.py", "app.tcss")
    CSS = """
    Screen { background: transparent; }
    #completion-help { height: auto; padding: 1; }
    #completion-rows { padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "replay", "Replay"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        themes.configure_theme(self, "terminal", background="terminal")
        self.generation = 0

    def compose(self) -> ComposeResult:
        yield Static(
            "COMPLETION COLOR STUDY · offline, no tools execute\n"
            "One green pass → muted green header. Failure stays red.\n"
            "R: replay · Click/Enter: details · Hover: bold · Q: quit",
            id="completion-help",
            markup=False,
        )
        yield VerticalScroll(id="completion-rows")

    async def on_mount(self) -> None:
        await self.action_replay()

    async def action_replay(self) -> None:
        self.generation += 1
        generation = self.generation
        rows = self.query_one("#completion-rows", VerticalScroll)
        await rows.remove_children()
        fixtures = (
            ("read_file", {"path": "src/demo.py"}, "Read 42 lines", None),
            ("search", {"pattern": "ToolCall", "path": "src/"}, "3 matches", None),
            ("shell", {"command": "pytest tests/tui -q"}, "12 passed", None),
            ("mcp.catalog.lookup", {"query": "restricted collection"}, "", "Permission denied"),
        )
        for index, (tool, arguments, output, error) in enumerate(fixtures):
            card = CompletionCard(str(index), tool, arguments)
            await rows.mount(card)
            delay = 2.5 + index * 1.7

            def finish(
                card: CompletionCard = card, output: str = output, error: str | None = error, delay: float = delay
            ) -> None:
                if generation == self.generation and card.is_mounted:
                    card.finish(output, error, delay * 1000)

            self.set_timer(delay, finish)


if __name__ == "__main__":
    CompletionStudy().run()
