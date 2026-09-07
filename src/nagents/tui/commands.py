"""Inline command discovery. Keep keyboard focus in the composer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from textual import on
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.strip import Strip
from textual.widgets import OptionList
from textual.widgets import Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from nagents.harness.commands import Command


class SlashOptions(OptionList):
    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        # Textual 8.2 drops CSS reverse in option visual styles. Apply it after
        # rendering, with native defaults so selection works on light and dark.
        if (
            self.app.native_ansi_color
            and self.highlighted is not None
            and any(segment.style and segment.style.meta.get("option") == self.highlighted for segment in strip)
        ):
            strip = Strip(
                Segment.apply_style(
                    strip, post_style=Style(color="default", bgcolor="default", reverse=True, bold=True)
                ),
                strip.cell_length,
            )
        return strip


class SlashMenu(Vertical):
    class Chosen(Message):
        def __init__(self, command: Command) -> None:
            super().__init__()
            self.command = command

    def __init__(self) -> None:
        super().__init__(id="slash-menu")
        self.matches: list[Command] = []
        self._signature: tuple[object, ...] = ()

    def compose(self) -> ComposeResult:
        options = SlashOptions(id="slash-options")
        options.can_focus = False
        yield options
        yield Static(id="slash-hint", markup=False)

    def update_commands(self, commands: list[Command], query: str) -> None:
        width = max(20, self.app.size.width - 8)
        rows = max(2, min(7, self.app.size.height - 13))
        signature = (tuple(commands), query, width, rows)
        if signature == self._signature:
            return
        self._signature = signature
        options = self.query_one(OptionList)
        selected = self.selected()
        query = query.casefold()
        self.matches = [
            command
            for command in commands
            if query in command.name.casefold()
            or query in command.description.casefold()
            or query in command.source.casefold()
        ]
        self.matches.sort(key=lambda command: (command.name != query, not command.name.startswith(query)))
        options.clear_options()
        for command in self.matches:
            label = Content.styled(f"/{command.name}", "bold").truncate(max(12, width // 2), ellipsis=True)
            source = Content.styled(f"[{command.source}]", "$ngn-tool").truncate(
                min(24, max(10, width // 3)), ellipsis=True
            )
            room = max(0, width - label.cell_length - source.cell_length - 4)
            description = Content(command.description.replace("\n", " ")).truncate(room, ellipsis=True)
            label = label.append("  ").append(description)
            label = label.append(" " * max(2, width - label.cell_length - source.cell_length)).append(source)
            options.add_option(Option(label, id=command.name))
        options.styles.height = min(rows, max(1, len(self.matches)))
        names = [command.name for command in self.matches]
        options.highlighted = (
            names.index(selected.name) if selected and selected.name in names else (0 if names else None)
        )
        self._hint()

    def selected(self) -> Command | None:
        index = self.query_one(OptionList).highlighted
        return self.matches[index] if index is not None and index < len(self.matches) else None

    def move(self, direction: int) -> None:
        options = self.query_one(OptionList)
        if direction > 0:
            options.action_cursor_down()
        else:
            options.action_cursor_up()
        self._hint()

    def choose(self) -> None:
        selected = self.selected()
        if selected is not None:
            self.post_message(self.Chosen(selected))

    def _hint(self) -> None:
        selected = self.selected()
        suffix = f"  /{selected.name} {selected.argument_hint}" if selected and selected.argument_hint else ""
        self.query_one("#slash-hint", Static).update(
            "Up/Down select  Enter prefill  Esc close" + suffix
            if self.matches
            else "No matching commands. Backspace to search again; Esc closes."
        )

    @on(OptionList.OptionSelected)
    def clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.choose()
