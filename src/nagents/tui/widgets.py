"""Conversation widgets and the keyboard-first composer."""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import replace
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar
from urllib.parse import unquote
from urllib.parse import urlsplit

from rich.color import Color
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.content import Content
from textual.content import Span
from textual.highlight import highlight
from textual.message import Message
from textual.style import Style
from textual.widgets import Collapsible
from textual.widgets import Markdown
from textual.widgets import Static
from textual.widgets import TextArea
from textual.widgets._collapsible import CollapsibleTitle
from textual.widgets.markdown import MarkdownBlock
from textual.widgets.markdown import MarkdownFence

from nagents.harness.agents.updates import NOTIFICATION_PREFIX
from nagents.harness.agents.updates import WAKEUP_PREFIX
from nagents.harness.tools.discovery import added_tool_names
from nagents.tool_outcomes import shell_outcome
from nagents.tools.output import ToolOutput

from .themes import CodeTheme

if TYPE_CHECKING:
    from collections.abc import Callable

    from markdown_it.token import Token
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.events import Click
    from textual.events import Key
    from textual.events import Paste
    from textual.geometry import Size
    from textual.timer import Timer

OUTPUT_LIMIT = 16_000


def bounded(text: str) -> str:
    """Keep the latest output, including an explicit truncation indicator."""
    if len(text) > OUTPUT_LIMIT:
        return "[earlier output truncated]\n" + text[-OUTPUT_LIMIT:]
    return text


def format_value(value: object) -> str:
    if isinstance(value, ToolOutput):
        return str(value)
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=True, default=str)


def saved_context_title(text: str) -> str:
    """Recognize canonical saved model context for display, never task authority."""
    for prefix, title in ((NOTIFICATION_PREFIX, "Saved agent context"), (WAKEUP_PREFIX, "Saved wake-up context")):
        if not text.startswith(prefix):
            continue
        try:
            value: object = json.loads(text[len(prefix) :])
        except ValueError:
            return ""
        if not isinstance(value, dict):
            return ""
        if prefix == NOTIFICATION_PREFIX:
            if set(value) - {"sessions", "human_messages", "more"}:
                return ""
            sessions, notes = value.get("sessions"), value.get("human_messages")
            if not isinstance(sessions, list) or not isinstance(notes, list):
                return ""
            if not all(
                isinstance(item, dict)
                and isinstance(item.get("session_id"), str)
                and isinstance(item.get("status"), str)
                and (isinstance(item.get("result"), str) != isinstance(item.get("error"), str))
                for item in sessions
            ) or not all(
                isinstance(item, dict)
                and isinstance(item.get("session_id"), str)
                and isinstance(item.get("prompt"), str)
                for item in notes
            ):
                return ""
        else:
            wakeups = value.get("wakeups")
            if set(value) != {"wakeups"} or not isinstance(wakeups, list):
                return ""
            if not all(isinstance(item, dict) and isinstance(item.get("reason"), str) for item in wakeups):
                return ""
        return title
    return ""


class ToolNotice(Collapsible):
    """Compact availability summary with inspectable source groups."""

    def __init__(self, text: str) -> None:
        names = sorted(added_tool_names(text))
        groups: dict[str, list[str]] = {}
        for name in names:
            parts = name.split("__", 2)
            if len(parts) == 3 and parts[0] == "mcp" and parts[1] and parts[2]:
                label, tool = f"MCP server {parts[1]} added", parts[2]
            else:
                label, tool = "Custom tools", name
            groups.setdefault(label, []).append(tool)
        children: list[Collapsible | Static] = [
            Collapsible(
                Static("\n".join(tools), markup=False),
                title=f"{label}: {len(tools)}",
                collapsed=True,
                classes="tool-source",
            )
            for label, tools in groups.items()
        ]
        title = f"Tools added: {len(names)}" if names else "Tools added"
        super().__init__(
            *(children or [Static(text, markup=False)]),
            title=title,
            collapsed=True,
            classes="tool-added",
        )
        self.tooltip = text


class SavedContext(Collapsible):
    """Keep recorded background envelopes inspectable without a user bubble."""

    def __init__(self, title: str, text: str) -> None:
        super().__init__(Static(text, markup=False), title=title, collapsed=True, classes="saved-context")


class Composer(TextArea):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "submit", show=False),
        Binding("tab", "complete", show=False),
        Binding("shift+tab", "ignore_tab", show=False),
        Binding("shift+enter,ctrl+j,alt+enter", "newline", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, complete: Callable[[str], bool], *, id: str = "composer") -> None:
        super().__init__(id=id, highlight_cursor_line=False)
        self.complete = complete
        self.prompt_history: list[str] = []
        self.history_index = 0
        self.draft = ""

    def action_submit(self) -> None:
        if self.complete("insert"):
            return
        self.post_message(self.Submitted(self.text))

    def action_complete(self) -> None:
        self.complete("insert")

    def action_ignore_tab(self) -> None:
        """Keep Shift+Tab from moving focus out of the composer."""

    def action_newline(self) -> None:
        self.replace("\n", *self.selection, maintain_selection_offset=False)

    def on_key(self, event: Key) -> None:
        # Keep editing shortcuts in the same queue as printable input. App-level
        # binding dispatch can otherwise reorder a fast burst of terminal keys.
        actions = {
            "enter": self.action_submit,
            "tab": lambda: self.complete("insert"),
            "shift+tab": lambda: None,
            "shift+enter": self.action_newline,
            "ctrl+j": self.action_newline,
            "alt+enter": self.action_newline,
            "up": self.action_cursor_up,
            "down": self.action_cursor_down,
        }
        if event.key in actions:
            event.stop()
            event.prevent_default()
            actions[event.key]()

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
        if not select and self.complete("up"):
            return
        if not select and self.selection.is_empty and self.cursor_location == (0, 0) and self.history_index > 0:
            if self.history_index == len(self.prompt_history):
                self.draft = self.text
            self.history_index -= 1
            self.load_text(self.prompt_history[self.history_index])
            self.move_cursor((0, 0))
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if not select and self.complete("down"):
            return
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


class CodeFence(MarkdownFence):
    @classmethod
    def highlight(cls, code: str, language: str, ansi: bool = False, dark: bool = False) -> Content:
        return highlight(code, language=language or "text", theme=CodeTheme)


def _terminal_link(href: str) -> str:
    """Accept web URLs only, never terminal control bytes or encoded controls."""
    if any(character.isspace() for character in href) or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in unquote(href)
    ):
        return ""
    try:
        target = urlsplit(href)
        if target.scheme.lower() in {"http", "https"} and target.hostname:
            return href
    except ValueError:
        pass
    return ""


class NativeLinkBlock(MarkdownBlock):
    """Keep Textual's inline formatting but hand web activation to the terminal."""

    def _token_to_content(self: MarkdownBlock, token: Token) -> Content:
        content = MarkdownBlock._token_to_content(self, token)
        spans: list[Span] = []
        for span in content.spans:
            style = span.style
            if isinstance(style, Style):
                action = style.meta.get("@click")
                if isinstance(action, str) and action.startswith("link("):
                    try:
                        href = ast.literal_eval(action[5:-1])
                    except (SyntaxError, ValueError):
                        href = ""
                    link = _terminal_link(href) if isinstance(href, str) else ""
                    # Drop the application click handler for every link, including
                    # unsafe schemes; the exact original URI is never unquoted.
                    style = replace(style, link=link or None, _meta=None, underline=bool(link))
                    if link:
                        style = Style.parse("underline $accent") + style
            spans.append(Span(span.start, span.end, style))
        return Content(content.plain, spans=spans)


_NATIVE_BLOCKS: dict[type[MarkdownBlock], type[MarkdownBlock]] = {}


class ConversationMarkdown(Markdown):
    def get_block_class(self, block_name: str) -> type[MarkdownBlock]:
        base = CodeFence if block_name in {"fence", "code_block"} else super().get_block_class(block_name)
        if base not in _NATIVE_BLOCKS:
            _NATIVE_BLOCKS[base] = type(
                f"Native{base.__name__}", (base,), {"_token_to_content": NativeLinkBlock._token_to_content}
            )
        return _NATIVE_BLOCKS[base]

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """No application-level browser launch, even for synthetic click events."""
        event.stop()


class Turn(Vertical):
    def __init__(self, role: str, text: str = "") -> None:
        super().__init__(classes=f"turn {role}")
        self.role = role
        self.body = ConversationMarkdown(text, open_links=False) if role == "assistant" else Static(text, markup=False)

    def compose(self) -> ComposeResult:
        if self.role != "user":
            yield Static("ngn", classes="turn-label", markup=False)
        yield self.body


class SelectableToolTitle(CollapsibleTitle):
    """A tool header is both a disclosure control and selectable output."""

    ALLOW_SELECT = True

    async def _on_click(self, event: Click) -> None:
        event.stop()
        event.prevent_default()
        # Textual may deliver a click after a drag. Never collapse the selected
        # text out from under the pointer or clear an established selection.
        if self.screen.get_selected_text():
            return
        self.post_message(self.Toggle())


class ToolCard(Collapsible):
    def __init__(
        self,
        call_id: str,
        tool: str,
        arguments: object = None,
        *,
        default_timeout: float = 0,
        animations: bool = True,
        preview: bool = False,
        restored: bool = False,
    ) -> None:
        self.call_id = call_id
        self.tool = tool
        self._hint = self._argument_hint(arguments)
        self._file_change = tool in {"edit", "write"}
        self._outcome = "interrupted" if restored else "preparing" if preview else "running"
        self._animations = animations and not restored
        self._started_at = 0.0 if restored else monotonic()
        self._default_timeout = default_timeout
        self._timeout = 0.0 if preview else self._argument_timeout(arguments)
        self._duration = "" if preview or restored else "0.00s"
        self.output_text = ""
        self._summary_text = Text()
        self._completion_source = Text()
        self._completion_started = 0.0
        self._completion_timer: Timer | None = None
        self.tool_name = Static(tool, markup=False, classes="tool-name")
        self.arguments = Static(
            highlight(
                bounded("" if preview and arguments is None else format_value(arguments)),
                language="json",
                theme=CodeTheme,
            ),
            classes="tool-arguments",
        )
        self.arguments.display = preview or not self._file_change
        self.file_path = Static(self._hint, markup=False, classes="tool-file-path")
        self.file_path.display = not preview and self._file_change and bool(self._hint)
        self.output = Static(
            "No saved result" if restored else "Waiting for output", markup=False, classes="tool-output"
        )
        self.output.display = not preview
        self.diff = Static(classes="tool-diff")
        self.diff.display = False
        super().__init__(
            self.tool_name,
            self.arguments,
            self.file_path,
            self.output,
            self.diff,
            title="",
            collapsed=True,
            collapsed_symbol=">",
            expanded_symbol="v",
            classes="tool-card" if preview or restored else "tool-card running",
        )
        self._title = SelectableToolTitle(
            label="",
            collapsed_symbol=">",
            expanded_symbol="v",
            collapsed=True,
        )
        self._update_summary()

    _elapsed_timer: Timer | None = None
    _allocated_width: int = 0

    def on_mount(self) -> None:
        if self._outcome in {"preparing", "ready", "running"}:
            self._start_elapsed()

    def _start_elapsed(self) -> None:
        self._stop_elapsed()
        animated = self._animations and self.app.animation_level != "none"
        self._elapsed_timer = self.set_interval(1 / 30 if animated else 0.1, self._tick_elapsed)

    def set_preview(self, tool: str, arguments_text: str, *, ready: bool = False, truncated: bool = False) -> None:
        """Render incomplete streamed arguments without attempting to decode JSON."""
        self._preserve_inspection_position()
        self.tool = tool
        self.tool_name.update(tool)
        self._file_change = tool in {"edit", "write"}
        self._outcome = "ready" if ready else "preparing"
        self._duration = ""
        self._timeout = 0.0
        self._hint = self._plain_line(arguments_text)
        if truncated:
            self._hint += " [truncated]"
        text = bounded(arguments_text)
        if truncated:
            text += "\n[arguments truncated]"
        self.arguments.update(highlight(text, language="json", theme=CodeTheme))
        self.arguments.display = True
        self.file_path.display = False
        self.output.display = False
        self.diff.display = False
        self.remove_class("running", "complete", "failed", "cancelled")
        self._update_summary()

    def start_call(self, call_id: str, tool: str, arguments: object) -> None:
        """Promote this card in place once the streamed call starts executing."""
        self._stop_completion()
        self._preserve_inspection_position()
        self._stop_elapsed()
        self.call_id = call_id
        self.tool = tool
        self.tool_name.update(tool)
        self._file_change = tool in {"edit", "write"}
        self._outcome = "running"
        self._started_at = monotonic()
        self._duration = "0.00s"
        self.arguments.display = not self._file_change
        self.output_text = ""
        self.output.update("Waiting for output")
        self.output.display = True
        self.diff.display = False
        self.remove_class("complete", "failed", "cancelled")
        self.add_class("running")
        self.set_arguments(arguments)
        if self.is_mounted:
            self._start_elapsed()

    def on_unmount(self) -> None:
        self._stop_elapsed()
        self._stop_completion()

    def _stop_completion(self) -> None:
        if self._completion_timer is not None:
            self._completion_timer.stop()
            self._completion_timer = None

    def _paint_completion(self) -> None:
        if self._outcome != "complete":
            self._stop_completion()
            return
        text = self._summary_text.copy()
        animated = self._animations and self.is_mounted and self.app.animation_level != "none"
        progress = min(1.0, max(0.0, (monotonic() - self._completion_started) / 1.25)) if animated else 1.0

        def ease(value: float) -> float:
            value = min(1.0, max(0.0, value))
            return value**3 * (value * (value * 6 - 15) + 10)

        head = -5 + ease(progress) * (len(text) + 10)
        foreground = self.app.get_css_variables().get("ngn-composer-text", "") if self.is_mounted else ""
        light = foreground.startswith("#") and sum(Color.parse(foreground).get_truecolor()) < 384
        final = Color.parse("#396347" if light else "#8fae98").get_truecolor()
        gleam_color = Color.parse("#4e7c58" if light else "#b6d8bb").get_truecolor()
        for index in range(len(text)):
            source = (
                self._completion_source.get_style_at_offset(self.app.console, index).color if self.is_mounted else None
            )
            base = (
                source.get_truecolor()
                if source is not None
                else Color.parse("#464646" if light else "#888888").get_truecolor()
            )
            settled = ease((head - index + 3) / 6)
            gleam = ease(1 - abs(index - head) / 5) * (1 - ease((progress - 0.85) / 0.15)) * 0.8
            channels = [a + (b - a) * settled for a, b in zip(base, final, strict=True)]
            color = "#" + "".join(
                f"{round(a + (b - a) * gleam):02x}" for a, b in zip(channels, gleam_color, strict=True)
            )
            text.stylize("not dim " + color, index, index + 1)
        self._title.set_reactive(type(self._title).label, Content.from_rich_text(text))
        self._title.mutate_reactive(type(self._title).label)
        if progress >= 1:
            self._stop_completion()

    def _stop_elapsed(self) -> None:
        if self._elapsed_timer is not None:
            self._elapsed_timer.stop()
            self._elapsed_timer = None

    def _tick_elapsed(self) -> None:
        if self._outcome == "running":
            self._duration = f"{max(0, monotonic() - self._started_at):.2f}s"
        if self._outcome in {"preparing", "ready", "running"}:
            self._update_summary()

    def _argument_timeout(self, arguments: object) -> float:
        # Only explicit unit-bearing names or the conventional seconds timeout.
        # Never infer deadlines from arbitrary argument strings or nested payloads.
        if isinstance(arguments, dict):
            for key, scale in (("timeout", 1), ("timeout_seconds", 1), ("timeout_ms", 0.001)):
                value = arguments.get(key)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value > 0
                ):
                    return value * scale
        return self._default_timeout

    @staticmethod
    def _plain_line(text: str) -> str:
        return " ".join(
            "".join(char for char in text if char.isprintable() or char.isspace() or char == "\u200d").split()
        )

    def _argument_hint(self, arguments: object) -> str:
        # Only recognizable targets; never summarize arbitrary payloads or errors.
        keys = ("name",) if self.tool == "skill" else ("command", "path", "file_path", "pattern", "url")
        if isinstance(arguments, dict):
            for key in keys:
                value = arguments.get(key)
                if isinstance(value, str) and value.strip():
                    return self._plain_line(value)
        return ""

    @staticmethod
    def _shorten(text: str, width: int) -> str:
        line = Text(text)
        line.truncate(max(0, width), overflow="ellipsis")
        return line.plain

    def _update_summary(self) -> None:
        # Reserve the outcome first. Cell-aware clipping also handles wide Unicode
        # names without wrapping or pushing failures off the right edge.
        width = max(0, (self._allocated_width or self.content_size.width or 72) - 4)
        suffix = f"  {self._outcome:<10}"
        preview = self._outcome in {"preparing", "ready"}
        timing = "" if preview else self._duration
        if not preview and self._timeout > 0:
            timing += f" / {self._timeout:g}s"
        if width < len(suffix) + len(timing) + 4:
            # Outcome wins on tiny terminals; timeout is supplementary metadata.
            suffix = self._shorten(self._outcome, width)
            timing = self._duration if not preview and width >= len(suffix) + len(self._duration) + 1 else ""
            if timing:
                suffix += " "
            remaining = max(0, width - Text(suffix + timing).cell_len)
            summary = Text(" " * remaining + suffix + timing)
            self._summary_text = summary
            self.title = summary.plain
            self._title.label = Content.from_rich_text(summary)
            if self._outcome == "complete":
                self._paint_completion()
            return
        # Use the same elapsed and timeout slots for every row, even when no
        # timeout applies, so neither the outcome nor elapsed column shifts.
        elapsed = "" if preview else self._duration
        timeout = f" / {self._timeout:g}s" if not preview and self._timeout > 0 else ""
        timing_width = min(22, max(len(timing), width - len(suffix) - 8))
        if timing_width >= 22:
            timing = elapsed.rjust(10) + timeout.ljust(max(12, len(timeout)))
        else:
            timing = timing.rjust(timing_width)
        timer_width = Text(timing).cell_len + 2 if timing else 0
        available = max(0, width - Text(suffix).cell_len - timer_width)
        name = self._plain_line(self.tool)
        if self._hint and available >= 16:
            name = self._shorten(name, max(12, available // 2))
            hint = self._shorten(self._hint, available - Text(name).cell_len - 3)
            name = f"{name} — {hint}"
        else:
            name = self._shorten(name, available)
        gap = max(2, width - Text(name + suffix + timing).cell_len)
        summary = Text(name + " " * gap + suffix + timing)
        # Foreground-only shimmer across the header; layout and background stay put.
        if (
            self._outcome in {"preparing", "ready", "running"}
            and self._animations
            and self.is_mounted
            and self.app.animation_level != "none"
        ):
            age = max(0.0, monotonic() - self._started_at)
            count = len(summary)
            phase = (age % 1.8) / 1.8
            eased = phase**3 * (phase * (phase * 6 - 15) + 10)
            head = -4 + eased * (count + 12)
            variables = self.app.get_css_variables()
            foreground = variables.get("ngn-composer-text", "")
            light_surface = foreground.startswith("#") and sum(Color.parse(foreground).get_truecolor()) < 384
            teal = Color.parse("#17665d" if light_surface else "#81c7be").get_truecolor()
            lavender = Color.parse("#67508b" if light_surface else "#b5a5df").get_truecolor()
            baseline = 70 if light_surface else 136
            for index in range(count):
                amount = max(0.0, 1 - abs(index - head) / 5)
                amount = amount**3 * (amount * (amount * 6 - 15) + 10)
                blend = min(1.0, max(0.0, (index - head + 5) / 10))
                tint = [a + (b - a) * blend for a, b in zip(teal, lavender, strict=True)]
                color = "#" + "".join(f"{round(baseline + (channel - baseline) * amount):02x}" for channel in tint)
                summary.stylize(color, index, index + 1)
        self._summary_text = summary
        self.title = summary.plain
        # Content equality compares plain text only. A normal assignment silently
        # drops style-only frames (including the first shimmer after title watch).
        # Store explicitly and notify the label watcher even when text is equal.
        self._title.set_reactive(type(self._title).label, Content.from_rich_text(summary))
        self._title.mutate_reactive(type(self._title).label)
        if self._outcome == "complete":
            self._paint_completion()

    def _watch_title(self, title: str) -> None:
        # Collapsible normally parses title strings as markup. In particular,
        # truncation may leave unmatched brackets, so bypass parsing entirely.
        self._title.label = Content(title)

    def _size_updated(self, size: Size, virtual_size: Size, container_size: Size, layout: bool = True) -> bool:
        changed = super()._size_updated(size, virtual_size, container_size, layout)
        # Synchronize with allocation, not the queued Resize event: the compositor
        # may paint the new region before that message (or a timer) is processed.
        # Its region lookup can still be stale here, so retain the supplied width.
        width = max(0, size.width - self.styles.gutter.width)
        if width != self._allocated_width:
            self._allocated_width = width
            self._update_summary()
        return changed

    def on_key(self, event: Key) -> None:
        if event.key == "space" and self._title.has_focus:
            event.stop()
            event.prevent_default()
            self.collapsed = not self.collapsed

    def _watch_collapsed(self, collapsed: bool) -> None:
        # Keep Collapsible's state and notifications, but target its header
        # rather than starting a scroll animation toward the entire large card.
        if self.is_mounted and not collapsed and isinstance(self.parent, VerticalScroll):
            # Release bottom-follow before exposing the contents. A deferred
            # header scroll can run before layout updates the scroll bounds.
            self.parent.release_anchor()
        self._update_collapsed(collapsed)
        self.post_message(self.Collapsed(self) if collapsed else self.Expanded(self))
        if self.is_mounted:
            self.call_after_refresh(lambda: self._title.scroll_visible(animate=False))

    def append_output(self, text: str) -> None:
        self._preserve_inspection_position()
        self.output_text = bounded(self.output_text + text)
        rendered = Text(self.output_text)
        for match in re.finditer(r"file:///[^\s]+", self.output_text):
            rendered.stylize(f"link {match.group()}", match.start(), match.end())
        # Content preserves link spans without requiring an active Textual app.
        # Restored cards and unmounted result previews are populated before mount.
        self.output.update(Content.from_rich_text(rendered))

    def _preserve_inspection_position(self) -> None:
        conversation = self.parent
        if self.collapsed or not self._title.has_focus or not isinstance(conversation, VerticalScroll):
            return
        header = self._title.region
        viewport = conversation.content_region
        if header.height and viewport.y <= header.y and header.bottom <= viewport.bottom:
            # Stop bottom-follow before layout grows. Do not schedule a scroll:
            # it could override a subsequent user scroll or chase an offscreen
            # header. Returning to the bottom resumes normal app following.
            conversation.release_anchor()

    def set_arguments(self, arguments: object) -> None:
        self._preserve_inspection_position()
        self.arguments.update(highlight(bounded(format_value(arguments)), language="json", theme=CodeTheme))
        self._hint = self._argument_hint(arguments)
        self._timeout = self._argument_timeout(arguments)
        self.file_path.update(self._hint)
        self.file_path.display = self._file_change and bool(self._hint)
        self._update_summary()

    def finish(self, result: object, error: str | None, duration_ms: float) -> None:
        self._preserve_inspection_position()
        previous_outcome = self._outcome
        self._completion_source = self._summary_text.copy()
        self._stop_completion()
        process_outcome = "" if error else shell_outcome(self.tool, result)
        failed = bool(error or process_outcome)
        state = "failed" if error else process_outcome or "complete"
        self._stop_elapsed()
        self._duration = f"{duration_ms / 1000:.2f}s" if duration_ms > 0 else ""
        self._outcome = state
        animate_completion = (
            state == "complete"
            and previous_outcome != "complete"
            and self.is_mounted
            and self._animations
            and self.app.animation_level != "none"
        )
        self._completion_started = monotonic() if animate_completion else monotonic() - 2
        self._update_summary()
        if animate_completion:
            self._completion_timer = self.set_interval(1 / 30, self._paint_completion)
        self.remove_class("running", "cancelled")
        self.set_class(failed, "failed")
        self.set_class(not failed, "complete")
        if isinstance(result, dict) and isinstance(result.get("diff"), str):
            self.diff.update(highlight(bounded(result["diff"]), language="diff", theme=CodeTheme))
            self.diff.display = True
            result = {key: value for key, value in result.items() if key != "diff"}
        if self._file_change and isinstance(result, dict):
            path = result.get("path")
            if isinstance(path, str):
                self.file_path.update(path)
                self.file_path.display = True
            # Suppress only known success metadata; preserve unexpected details.
            if not error and self.diff.display:
                result = {key: value for key, value in result.items() if key not in {"path", "changed"}}
                self.output.display = bool(self.output_text or result)
        if error:
            self.output.display = True
            self.append_output(("\n" if self.output_text else "") + error)
        elif result is not None and result != {}:
            self.append_output(("\n" if self.output_text else "") + format_value(result))
        elif not self.output_text:
            self.output.update("No text output" if not self.diff.display else "Diff preview below")

    def cancel(self) -> None:
        self._stop_completion()
        self._tick_elapsed()
        self._stop_elapsed()
        self._outcome = "cancelled"
        self._update_summary()
        self.remove_class("running", "complete", "failed")
        self.add_class("cancelled")
