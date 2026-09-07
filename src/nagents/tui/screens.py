"""Small modal surfaces. All backend decisions remain in the harness."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.highlight import highlight
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import OptionList
from textual.widgets import Static
from textual.widgets.option_list import Option

from .commands import SlashOptions
from .themes import CodeTheme
from .widgets import format_value

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from nagents.harness.types import ApprovalRequest


class ApprovalModal(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "deny", show=False, priority=True)]

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog approval-dialog"):
            yield Static("PERMISSION REQUIRED", classes="dialog-title", markup=False)
            with VerticalScroll(classes="approval-content"):
                yield Static(self.request.tool, classes="approval-tool", markup=False)
                yield Static(self.request.description, markup=False)
                yield Static("EXACT ARGUMENTS", classes="section-label", markup=False)
                yield Static(highlight(format_value(self.request.arguments), language="json", theme=CodeTheme))
                if self.request.preview:
                    yield Static("PROPOSED CHANGE / PREVIEW", classes="section-label", markup=False)
                    yield Static(highlight(self.request.preview, language="diff", theme=CodeTheme))
            yield Static("Only this request. Nothing is allowed by default.", classes="dialog-hint", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Deny", id="deny")
                yield Button("Allow once", id="allow", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#deny", Button).focus()

    def action_deny(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def decide(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")


class ChoiceModal(ModalScreen[str]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", show=False)]

    def __init__(self, title: str, choices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.heading = title
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog choice-dialog"):
            yield Static(self.heading, classes="dialog-title", markup=False)
            yield Input(placeholder="Type to filter", id="filter")
            yield SlashOptions(id="choices")
            yield Static("Up/down select   Enter open   Esc back", classes="dialog-hint", markup=False)

    def on_mount(self) -> None:
        self.filter_choices("")
        self.query_one(Input).focus()

    def filter_choices(self, value: str) -> None:
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            Option(Text(label), id=key) for key, label in self.choices if value.casefold() in label.casefold()
        )
        options.highlighted = 0 if options.option_count else None

    @on(Input.Changed)
    def changed(self, event: Input.Changed) -> None:
        self.filter_choices(event.value)

    @on(Input.Submitted)
    def submit(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            self.dismiss(options.get_option_at_index(options.highlighted).id or "")

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "")

    def key_down(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def key_up(self) -> None:
        self.query_one(OptionList).action_cursor_up()


class ModelModal(ModalScreen[str]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", show=False)]

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog model-dialog"):
            yield Static("CHANGE MODEL", classes="dialog-title", markup=False)
            yield Static("Enter a model ID supported by your configured provider.", markup=False)
            yield Input(self.model, placeholder="Model ID", id="model-input")
            yield Static("Enter apply   Esc keep current", classes="dialog-hint", markup=False)

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())


class DetailModal(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", show=False)]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.heading = title
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog detail-dialog"):
            yield Static(self.heading, classes="dialog-title", markup=False)
            with VerticalScroll():
                yield Static(self.text, markup=False)
            yield Button("Close", id="close-detail")

    @on(Button.Pressed)
    def close_detail(self) -> None:
        self.dismiss()
