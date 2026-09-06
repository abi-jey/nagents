"""Conversation widgets and the keyboard-first composer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.markup import escape
from rich.syntax import Syntax
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Collapsible
from textual.widgets import Markdown
from textual.widgets import Static
from textual.widgets import TextArea

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.events import Paste

OUTPUT_LIMIT = 16_000


def bounded(text: str) -> str:
    """Keep the latest output, including an explicit truncation indicator."""
    if len(text) > OUTPUT_LIMIT:
        return "[earlier output truncated]\n" + text[-OUTPUT_LIMIT:]
    return text


def format_value(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=True, default=str)


class Composer(TextArea):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "submit", show=False, priority=True),
        Binding("ctrl+j,alt+enter", "newline", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self) -> None:
        super().__init__(id="composer", placeholder="Ask anything, or / for commands", highlight_cursor_line=False)
        self.prompt_history: list[str] = []
        self.history_index = 0
        self.draft = ""

    def action_submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        self.replace("\n", *self.selection, maintain_selection_offset=False)

    def on_paste(self, event: Paste) -> None:
        # TextArea inserts the entire paste; don't bubble it back to App for forwarding.
        event.stop()

    def remember(self, text: str) -> None:
        if not self.prompt_history or self.prompt_history[-1] != text:
            self.prompt_history.append(text)
        self.prompt_history = self.prompt_history[-200:]
        self.history_index = len(self.prompt_history)
        self.draft = ""

    def action_cursor_up(self, select: bool = False) -> None:
        if not select and self.selection.is_empty and self.cursor_location == (0, 0) and self.history_index > 0:
            if self.history_index == len(self.prompt_history):
                self.draft = self.text
            self.history_index -= 1
            self.load_text(self.prompt_history[self.history_index])
            self.move_cursor((0, 0))
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        end = (self.document.line_count - 1, len(self.document.lines[-1]))
        if (
            not select
            and self.selection.is_empty
            and self.cursor_location == end
            and self.history_index < len(self.prompt_history)
        ):
            self.history_index += 1
            self.load_text(
                self.prompt_history[self.history_index] if self.history_index < len(self.prompt_history) else self.draft
            )
            self.move_cursor((self.document.line_count - 1, len(self.document.lines[-1])))
        else:
            super().action_cursor_down(select)


class Turn(Vertical):
    def __init__(self, role: str, text: str = "") -> None:
        super().__init__(classes=f"turn {role}")
        self.role = role
        self.body = Markdown(text, open_links=False) if role == "assistant" else Static(text, markup=False)

    def compose(self) -> ComposeResult:
        yield Static("YOU" if self.role == "user" else "ngn", classes="turn-label", markup=False)
        yield self.body


class ToolCard(Collapsible):
    def __init__(self, call_id: str, tool: str, arguments: object = None) -> None:
        self.call_id = call_id
        self.tool = tool
        self.output_text = ""
        self.arguments = Static(
            Syntax(bounded(format_value(arguments)), "json", background_color="default", word_wrap=True),
            classes="tool-arguments",
        )
        self.output = Static("Waiting for output", markup=False, classes="tool-output")
        self.diff = Static(classes="tool-diff")
        self.diff.display = False
        super().__init__(
            self.arguments,
            self.output,
            self.diff,
            title=escape(f"{tool}  /  running"),
            collapsed_symbol=">",
            expanded_symbol="v",
            classes="tool-card",
        )

    def append_output(self, text: str) -> None:
        self.output_text = bounded(self.output_text + text)
        self.output.update(self.output_text)

    def set_arguments(self, arguments: object) -> None:
        self.arguments.update(
            Syntax(bounded(format_value(arguments)), "json", background_color="default", word_wrap=True)
        )

    def finish(self, result: object, error: str | None, duration_ms: float) -> None:
        state = "error" if error else "complete"
        duration = f"  {duration_ms / 1000:.2f}s" if duration_ms > 0 else ""
        self.title = escape(f"{self.tool}  /  {state}{duration}")
        self.set_class(bool(error), "failed")
        if isinstance(result, dict) and isinstance(result.get("diff"), str):
            self.diff.update(Syntax(bounded(result["diff"]), "diff", background_color="default", word_wrap=True))
            self.diff.display = True
            result = {key: value for key, value in result.items() if key != "diff"}
        if error:
            self.append_output(("\n" if self.output_text else "") + error)
            self.collapsed = False
        elif result is not None and result != {}:
            self.append_output(("\n" if self.output_text else "") + format_value(result))
        elif not self.output_text:
            self.output.update("No text output" if not self.diff.display else "Diff preview below")

    def cancel(self) -> None:
        self.title = escape(f"{self.tool}  /  cancelled")
