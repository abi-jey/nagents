"""Offline tool-call design study: python -m examples.tui_tool_calls."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Collapsible
from textual.widgets import Footer
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType


@dataclass(frozen=True)
class Call:
    name: str
    target: str
    state: str
    elapsed: str
    output: str


DESIGNS = ("Compact", "Cards", "Timeline")
SCENARIOS: dict[str, tuple[Call, ...]] = {
    "Mixed": (
        Call("read_file", "src/nagents/agent.py", "success", "0.08s", "Read 120 lines · Python · 4.2 KB"),
        Call("search", '"ToolCallEvent" in src/', "running", "1.4s", "Scanning 38 files…"),
        Call(
            "shell",
            "pytest tests/tui -q",
            "error",
            "3.2s",
            "FAILED test_layout\nAssertionError: expected width 80, got 79\nExit code: 1",
        ),
        Call(
            "edit",
            "src/nagents/tui/widgets.py",
            "approval",
            "—",
            "- collapsed=True\n+ collapsed=False\n\nApprove this simulated change?",
        ),
    ),
    "Parallel": (
        Call("read_file", "README.md", "success", "0.04s", "Read 80 lines"),
        Call("search", '"approval" in tests/', "running", "0.8s", "18 matches so far…"),
        Call("shell", "ruff check src/", "queued", "—", "Waiting for a worker"),
        Call(
            "to_agent",
            "Review tool-call accessibility",
            "running",
            "2.1s",
            "Child s-demo1234 · inspecting keyboard navigation",
        ),
    ),
    "Approval": (
        Call(
            "shell",
            "poetry run pytest",
            "approval",
            "—",
            "Run a local shell command?\nWorking directory: /demo/nagents\nTimeout: 120s\n\nThis is only a visual preview; nothing will execute.",
        ),
        Call("edit", "src/nagents/tui/widgets.py", "denied", "0.2s", "User declined the change. No files modified."),
    ),
    "Failures": (
        Call("shell", "pytest -q", "error", "2.6s", "2 failed, 41 passed\nAssertionError: tool status was hidden"),
        Call("shell", "sleep 90", "timeout", "30.0s", "Command exceeded its 30 second deadline."),
        Call("search", '"TODO" in src/', "cancelled", "0.7s", "Cancelled by user."),
    ),
    "Long output": (
        Call(
            "shell",
            "pytest tests/tui/test_tool_activity.py -vv --capture=no",
            "success",
            "4.8s",
            "\n".join(f"test_tool_state_{index:02d} PASSED" for index in range(1, 31)) + "\n\n30 passed in 4.8s",
        ),
        Call(
            "read_file",
            "src/a_very_long_directory_name/nested/another_directory/tool_call_presentation.py",
            "success",
            "0.1s",
            "Literal output, not markup: [red]hello[/red]\nUnicode: 日本語 / café / →\nResize the terminal to check wrapping.",
        ),
    ),
}
COLORS = {
    "success": "green",
    "running": "cyan",
    "queued": "dim",
    "error": "red",
    "approval": "yellow",
    "denied": "red",
    "timeout": "red",
    "cancelled": "dim",
}
SYMBOLS = {
    "success": "✓",
    "running": "◌",
    "queued": "·",
    "error": "✕",
    "approval": "?",
    "denied": "⊘",
    "timeout": "!",
    "cancelled": "-",
}


def summary(call: Call, *, timeline: bool = False) -> Text:
    """Render literal tool data; do not interpret Rich markup in arguments."""
    text = Text("│  " if timeline else "")
    text.append(f"{SYMBOLS[call.state]} ", style=COLORS[call.state])
    text.append(call.name, style="bold")
    text.append(f"  {call.target}")
    text.append(f"  · {call.state} · {call.elapsed}", style=COLORS[call.state])
    return text


class ToolCallStudy(App[None]):
    """Three presentation alternatives sharing exactly the same mock calls."""

    CSS = """
    Screen { background: $surface; }
    #heading { height: auto; padding: 1 2 0 2; text-style: bold; }
    #context { height: auto; padding: 0 2 1 2; color: $text-muted; }
    #calls { padding: 0 2; }
    Collapsible { margin: 0 0 1 0; padding: 0; border: none; background: transparent; }
    CollapsibleTitle { padding: 0 1; }
    CollapsibleTitle:hover { background: $boost; }
    .card { border: round $primary-muted; padding: 0 1; }
    .timeline { border-left: solid $primary-muted; padding-left: 1; }
    .output { height: auto; padding: 0 1 1 3; color: $text-muted; }
    .approval { border-left: solid $warning; }
    .error { border-left: solid $error; }
    #note { height: auto; padding: 1 2; color: $text-muted; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "design(0)", "Compact"),
        Binding("2", "design(1)", "Cards"),
        Binding("3", "design(2)", "Timeline"),
        Binding("n", "next_scenario", "Next scenario"),
        Binding("e", "expand", "Expand / collapse"),
        Binding("space", "replay", "Replay lifecycle"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, design: int = 0, scenario: str = "Mixed") -> None:
        super().__init__()
        self.design = design
        self.scenario = scenario
        self.expanded = False
        self.phase = -1

    def compose(self) -> ComposeResult:
        yield Static(id="heading")
        yield Static(id="context")
        yield VerticalScroll(id="calls")
        yield Static(
            "SIMULATION ONLY · no models, commands, edits, or approvals are executed.\nTab / arrows to focus a call; Enter to inspect its output. Space steps through a lifecycle.",
            id="note",
            markup=False,
        )
        yield Footer()

    async def on_mount(self) -> None:
        await self.render_calls()

    def visible_calls(self) -> tuple[Call, ...]:
        if self.phase < 0:
            return SCENARIOS[self.scenario]
        states = ("queued", "approval", "running", "success")
        outputs = (
            "Waiting for a worker",
            "Waiting for permission (simulated)",
            "Collecting test results…",
            "42 passed in 2.4s",
        )
        return (
            Call(
                "shell",
                "pytest tests/tui -q",
                states[self.phase],
                ("—", "—", "1.2s", "2.4s")[self.phase],
                outputs[self.phase],
            ),
        )

    async def render_calls(self) -> None:
        label = f"Lifecycle {self.phase + 1}/4" if self.phase >= 0 else self.scenario
        self.query_one("#heading", Static).update(f"TOOL CALL LAB  /  {DESIGNS[self.design]}  /  {label}")
        self.query_one("#context", Static).update(
            "Same mock data, different presentation.  1-3 design · N scenario · E details · Space lifecycle"
        )
        container = self.query_one("#calls", VerticalScroll)
        await container.remove_children()
        for call in self.visible_calls():
            card = Collapsible(
                Static(call.output, markup=False, classes="output"),
                title=summary(call, timeline=self.design == 2).markup,
                collapsed=not (self.expanded or self.design == 1),
                collapsed_symbol="▸",
                expanded_symbol="▾",
                classes=("card" if self.design == 1 else "timeline" if self.design == 2 else "compact")
                + f" {call.state}",
            )
            await container.mount(card)

    async def action_design(self, design: int) -> None:
        self.design = design
        await self.render_calls()

    async def action_next_scenario(self) -> None:
        names = tuple(SCENARIOS)
        self.scenario = names[(names.index(self.scenario) + 1) % len(names)]
        self.phase = -1
        await self.render_calls()

    def action_expand(self) -> None:
        self.expanded = not self.expanded
        for card in self.query(Collapsible):
            card.collapsed = not self.expanded

    async def action_replay(self) -> None:
        self.phase = (self.phase + 1) % 4
        await self.render_calls()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", choices=("compact", "cards", "timeline"), default="compact")
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="Mixed")
    args = parser.parse_args()
    ToolCallStudy(design=("compact", "cards", "timeline").index(args.design), scenario=args.scenario).run()


if __name__ == "__main__":
    main()
