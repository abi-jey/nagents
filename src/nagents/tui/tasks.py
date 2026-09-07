"""Interactive, process-local subagent conversations; the harness owns execution."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import on
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.message import Message as UIMessage
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea
from textual.widgets import Tree

from nagents.types import TextContent

from .widgets import bounded

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.widgets.tree import TreeNode

    from nagents.harness.subagents import TaskInfo
    from nagents.types import Message


class AgentTree(Tree[str]):
    """A bounded viewport over the whole task hierarchy, not one row per active job."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "agent-tree--running",
        "agent-tree--completed",
        "agent-tree--failed",
        "agent-tree--cancelled",
    }
    DEFAULT_CSS = """
    AgentTree {
        height: 1fr;
        min-height: 4;
        background: $ngn-background;
        color: $ngn-text;
        scrollbar-size: 1 1;
    }
    AgentTree .agent-tree--running { color: $ngn-accent; text-style: bold; }
    AgentTree .agent-tree--completed { color: $ngn-success; }
    AgentTree .agent-tree--failed { color: $ngn-error; }
    AgentTree .agent-tree--cancelled { color: $ngn-muted; }
    AgentTree > .tree--cursor { background: $ngn-selection; color: $ngn-text; text-style: $ngn-focus-style; }
    """

    class Opened(UIMessage):
        def __init__(self, task_id: str) -> None:
            super().__init__()
            self.task_id = task_id

    def __init__(self, *, id: str = "agent-tree") -> None:
        super().__init__("Agents / 0", data="", id=id)
        self.task_nodes: dict[str, TreeNode[str]] = {}
        self._signature: tuple[object, ...] = ()
        self.root.expand()

    def update_tasks(self, tasks: list[TaskInfo], parents: dict[str, str]) -> None:
        signature = tuple((task.id, task.name, task.status, parents.get(task.id, "")) for task in tasks)
        if signature == self._signature:
            return
        self._signature = signature
        cursor = self.cursor_node.data if self.cursor_node else ""
        expanded = {key for key, node in self.task_nodes.items() if node.is_expanded}
        previous_ids = set(self.task_nodes)
        root_expanded = self.root.is_expanded
        scroll = self.scroll_y
        self.clear()
        self.task_nodes.clear()
        self.root.set_label(f"Agents / {len(tasks)}")
        if root_expanded:
            self.root.expand()
        else:
            self.root.collapse()
        known_ids = {task.id for task in tasks}
        pending = list(tasks)
        while pending:
            ready = [
                task
                for task in pending
                if parents.get(task.id, "") not in known_ids or parents.get(task.id, "") in self.task_nodes
            ]
            # Corrupt or incomplete ancestry must not hide otherwise inspectable tasks.
            if not ready:
                ready = pending[:1]
            for task in ready:
                parent = self.task_nodes.get(parents.get(task.id, ""), self.root)
                state = {"running": "RUN", "completed": "DONE", "failed": "FAIL", "cancelled": "STOP"}[task.status]
                label = Text(state.ljust(4), style=self.get_component_rich_style(f"agent-tree--{task.status}"))
                label.append(" " + bounded(task.name))
                self.task_nodes[task.id] = parent.add(
                    label,
                    data=task.id,
                    expand=task.id in expanded or task.id not in previous_ids,
                    allow_expand=task.id in parents.values(),
                )
                pending.remove(task)
        if cursor in self.task_nodes:
            self.call_after_refresh(self.move_cursor, self.task_nodes[cursor])
        self.call_after_refresh(self.scroll_to, y=scroll, animate=False)

    def render_label(self, node: TreeNode[str], base_style: Style, style: Style) -> Text:
        label = super().render_label(node, base_style, style)
        if self.has_focus and node is self.cursor_node and self.app.current_theme.ansi:
            label.stylize(Style(color="default", bgcolor="default", reverse=True))
        return label

    @on(Tree.NodeSelected)
    def open_agent(self, event: Tree.NodeSelected[str]) -> None:
        event.stop()
        if event.node.data:
            self.post_message(self.Opened(event.node.data))


class TaskScreen(ModalScreen[tuple[str, str] | None]):
    """Return an explicit follow-up request, never execute work from a selection."""

    CSS_PATH = "tasks.tcss"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "task-dialog--user",
        "task-dialog--assistant",
        "task-dialog--tool",
        "task-dialog--status",
    }
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", show=False, priority=True),
        Binding("ctrl+enter", "send", show=False),
    ]

    def __init__(
        self,
        tasks: Callable[[], list[TaskInfo]],
        history: Callable[[str], Awaitable[list[Message]]],
        *,
        parents: Callable[[], dict[str, str]],
        selected_id: str = "",
        drafts: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.get_tasks = tasks
        self.get_history = history
        self.get_parents = parents
        self.infos: dict[str, TaskInfo] = {}
        self.selected_id = selected_id
        self._signature: tuple[object, ...] = ()
        self._drafts = drafts if drafts is not None else {}

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog", classes="dialog"):
            yield Static("SUBAGENT CONVERSATIONS", classes="dialog-title", markup=False)
            yield Static(
                "Inspect work, then continue a saved conversation. The parent receives the exchange.",
                id="task-caption",
                markup=False,
            )
            with Horizontal(id="task-body"):
                yield AgentTree(id="task-options")
                with Vertical(id="task-detail"):
                    yield Static("No subagents in this session.", id="task-heading", markup=False)
                    with VerticalScroll(id="task-history-scroll"):
                        yield Static("", id="task-history", markup=False)
            yield TextArea(
                self._drafts.get(self.selected_id, ""),
                id="task-followup",
                soft_wrap=True,
                placeholder="Write a follow-up for this agent...",
            )
            yield Static(
                "Up/Down select a task. Tab to write. Ctrl+Enter sends; Esc closes.", id="task-hint", markup=False
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Close", id="task-close")
                yield Button("Continue Agent", id="task-send", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.set_class(self.app.size.height < 26, "compact")
        self.refresh_tasks()
        self.query_one("#task-options", AgentTree).focus()
        self.set_interval(0.5, self.refresh_tasks)

    def on_resize(self) -> None:
        self.set_class(self.app.size.height < 26, "compact")

    def refresh_tasks(self) -> None:
        infos = self.get_tasks()
        parents = self.get_parents()
        signature = tuple(
            (info.id, info.name, info.status, info.result, info.error, parents.get(info.id, "")) for info in infos
        )
        if signature == self._signature:
            return
        self._signature = signature
        self.infos = {info.id: info for info in infos}
        options = self.query_one("#task-options", AgentTree)
        options.update_tasks(infos, parents)
        ids = list(self.infos)
        if ids:
            selected = self.selected_id if self.selected_id in self.infos else ids[0]
            self.show_task(selected)
            options.call_after_refresh(options.move_cursor, options.task_nodes[selected])
        else:
            self.selected_id = ""
            self.query_one("#task-heading", Static).update("No subagents in this session.")
            self.query_one("#task-history", Static).update("")
            self.query_one("#task-followup", TextArea).load_text("")
            self.query_one("#task-send", Button).disabled = True

    @on(Tree.NodeHighlighted, "#task-options")
    def highlighted(self, event: Tree.NodeHighlighted[str]) -> None:
        event.stop()
        if event.node.data in self.infos:
            self.show_task(event.node.data)

    @on(AgentTree.Opened)
    def selected(self, event: AgentTree.Opened) -> None:
        event.stop()
        if event.task_id in self.infos:
            self.show_task(event.task_id)
            self.query_one("#task-followup", TextArea).focus()

    def show_task(self, task_id: str) -> None:
        composer = self.query_one("#task-followup", TextArea)
        if task_id != self.selected_id:
            if self.selected_id:
                self._drafts[self.selected_id] = composer.text
            self.selected_id = task_id
            composer.load_text(self._drafts.get(task_id, ""))
            self.query_one("#task-history", Static).update("Loading saved conversation...")
        info = self.infos[task_id]
        self.query_one("#task-heading", Static).update(bounded(f"{info.name}  /  {info.status}"))
        self.query_one("#task-send", Button).disabled = info.status in {"running", "cancelled"}
        self.query_one("#task-hint", Static).update(
            "Cancelled tasks cannot continue. Delegate a new task instead."
            if info.status == "cancelled"
            else "This agent is still working. You can draft a follow-up; send it after completion."
            if info.status == "running"
            else "Tab to write. Ctrl+Enter or Continue Agent sends to this agent and notifies the parent."
        )
        self.load_history(task_id)

    @work(exclusive=True, group="task-history")
    async def load_history(self, task_id: str) -> None:
        info = self.infos[task_id]
        unavailable = False
        try:
            messages = await self.get_history(task_id)
        except (OSError, ValueError, RuntimeError):
            messages = []
            unavailable = True
        lines: list[str] = []
        if unavailable:
            lines.append("Saved history unavailable; showing the latest task snapshot.")
        for message in messages[-40:]:
            if message.role in {"system", "developer"}:
                continue
            if isinstance(message.content, str):
                content = message.content
            elif isinstance(message.content, list):
                content = "\n".join(part.text for part in message.content if isinstance(part, TextContent))
            else:
                content = ""
            if message.tool_calls:
                content += "\n" + "\n".join(f"Tool call: {call.name}" for call in message.tool_calls)
            label = message.role.upper() + (f" / {message.name}" if message.name else "")
            lines.append(f"{label}\n{bounded(content[:4000])}")
        if not messages:
            lines.append(f"USER\n{bounded(info.prompt)}")
            if info.result:
                lines.append(f"ASSISTANT\n{bounded(info.result)}")
        if info.error:
            lines.append(f"STATUS\n{bounded(info.error)}")
        if self.is_mounted and self.selected_id == task_id:
            transcript = Text()
            if len(messages) > 40:
                transcript.append("Showing the latest 40 messages.\n\n")
            for line in lines:
                label, separator, content = line.partition("\n")
                role = label.split(" / ", 1)[0].lower()
                component = role if role in {"user", "assistant", "tool"} else "status"
                transcript.append(label, style=self.get_component_rich_style(f"task-dialog--{component}"))
                transcript.append(separator + content + "\n\n")
            self.query_one("#task-history", Static).update(transcript)
            self.query_one("#task-history-scroll", VerticalScroll).scroll_end(animate=False)

    def action_send(self) -> None:
        info = self.infos.get(self.selected_id)
        text = self.query_one("#task-followup", TextArea).text.strip()
        if info is None or info.status in {"running", "cancelled"} or not text:
            self.query_one("#task-hint", Static).update("Choose a finished agent and enter a follow-up first.")
        elif len(text) > 16000:
            self.query_one("#task-hint", Static).update("Follow-ups are limited to 16,000 characters.")
        else:
            self._drafts[info.id] = text
            self.dismiss((info.id, text))

    def action_close(self) -> None:
        if self.selected_id:
            self._drafts[self.selected_id] = self.query_one("#task-followup", TextArea).text
        self.dismiss(None)

    @on(Button.Pressed, "#task-send")
    def send(self) -> None:
        self.action_send()

    @on(Button.Pressed, "#task-close")
    def close(self) -> None:
        self.action_close()
