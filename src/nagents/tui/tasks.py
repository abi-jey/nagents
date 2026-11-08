"""Interactive, process-local subagent conversations; the harness owns execution."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from rich.style import Style
from textual import on
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message as UIMessage
from textual.screen import Screen
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea
from textual.widgets import Tree

from nagents.types import TextContent

from .activity import StateField
from .activity import StateName
from .sweep import MOTION_STYLE
from .widgets import Composer
from .widgets import ToolCard
from .widgets import Turn
from .widgets import bounded

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from rich.text import Text
    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.widgets.tree import TreeNode

    from nagents.harness.agents.subagents import TaskInfo
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
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    def __init__(self, *, id: str = "agent-tree", compact: bool = False) -> None:
        super().__init__("Agents / 0", data="", id=id)
        self.compact = compact
        self.task_nodes: dict[str, TreeNode[str]] = {}
        self._signature: tuple[object, ...] = ()
        self._tasks: dict[str, TaskInfo] = {}
        self._fields: dict[str, StateField] = {}
        self._activity_tick = monotonic()
        self.root.expand()

    def update_tasks(self, tasks: list[TaskInfo], parents: dict[str, str]) -> None:
        self._tasks = {task.session_id: task for task in tasks}
        self._fields = {key: field for key, field in self._fields.items() if key in self._tasks}
        signature = tuple((task.session_id, parents.get(task.session_id, "")) for task in tasks)
        if signature == self._signature:
            self.refresh_activity()
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
        known_ids = {task.session_id for task in tasks}
        pending = list(tasks)
        while pending:
            ready = [
                task
                for task in pending
                if parents.get(task.session_id, "") not in known_ids
                or parents.get(task.session_id, "") in self.task_nodes
            ]
            # Corrupt or incomplete ancestry must not hide otherwise inspectable tasks.
            if not ready:
                ready = pending[:1]
            for task in ready:
                parent = self.task_nodes.get(parents.get(task.session_id, ""), self.root)
                self.task_nodes[task.session_id] = parent.add(
                    "",
                    data=task.session_id,
                    expand=task.session_id in expanded or task.session_id not in previous_ids,
                    allow_expand=task.session_id in parents.values(),
                )
                pending.remove(task)
        if cursor in self.task_nodes:
            self.call_after_refresh(self.move_cursor, self.task_nodes[cursor])
        self.call_after_refresh(self.scroll_to, y=scroll, animate=False)
        self.refresh_activity()

    def refresh_activity(self) -> None:
        """Refresh labels in place from the app animation loop, without rebuilding nodes."""
        now = monotonic()
        seconds = max(0.0, now - self._activity_tick)
        self._activity_tick = now
        harness = getattr(self.app, "harness", None)
        config = getattr(harness, "config", None)
        animated = getattr(config, "animations", True) and self.app.animation_level != "none"
        phases: dict[str, StateName] = {
            "starting": "starting",
            "tool": "tool",
            "tool_call": "tool",
            "reasoning": "working",
            "responding": "responding",
            "waiting": "delegated",
            "approval": "approval",
        }
        statuses: dict[str, StateName] = {"completed": "done", "failed": "error", "cancelled": "idle"}
        for index, (session_id, node) in enumerate(self.task_nodes.items()):
            task = self._tasks[session_id]
            state = statuses.get(task.status, phases.get(task.phase, "working"))
            field = self._fields.get(session_id)
            if field is None:
                field = self._fields[session_id] = StateField(state, offset=index + 1.7)
            field.select(state, immediate=not animated)
            if animated:
                field.advance(seconds)
            label = field.render(width=3 if self.compact else 7)
            label.stylize(MOTION_STYLE)
            if self.compact:
                display = task.label
            else:
                display = f"{task.label} [{session_id}]" if task.title else session_id
                display = f"{display} ({task.profile})"
            label.append(" " + bounded(display), style=self.get_component_rich_style(f"agent-tree--{task.status}"))
            if label != node.label:
                node.set_label(label)

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


class TaskScreen(Screen[tuple[str, str] | None]):
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
        Binding("f6", "focus_pane", "Message / conversation", priority=True),
        Binding("alt+left", "close", "Back", priority=True),
    ]

    def __init__(
        self,
        tasks: Callable[[], list[TaskInfo]],
        history: Callable[[str], Awaitable[list[Message]]],
        *,
        parents: Callable[[], dict[str, str]],
        selected_session_id: str = "",
        drafts: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.get_tasks = tasks
        self.get_history = history
        self.get_parents = parents
        self.infos: dict[str, TaskInfo] = {}
        self.selected_session_id = selected_session_id
        self._signature: tuple[object, ...] = ()
        self._drafts = drafts if drafts is not None else {}
        self._history_signature: tuple[object, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog"):
            with Horizontal(id="task-navigation"):
                yield Button("← Back", id="task-close")
                yield Static("Subagent conversation", id="task-heading", markup=False)
            with Horizontal(id="task-body"):
                with Vertical(id="task-detail"), VerticalScroll(id="task-history-scroll"):
                    yield Vertical(id="task-history")
                yield AgentTree(id="task-options")
            with Vertical(id="task-compose-area"):
                composer = Composer(lambda _: False, id="task-followup")
                composer.load_text(self._drafts.get(self.selected_session_id, ""))
                composer.placeholder = "Message this agent…"
                yield composer
                yield Static("", id="task-hint", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Static("F6: message / conversation · Esc: back", id="task-caption", markup=False)
                yield Button("Continue Agent", id="task-send", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.set_class(self.app.size.height < 26, "compact")
        self.set_class(self.app.size.width < 80, "narrow")
        self.refresh_tasks()
        self.query_one("#task-options", AgentTree).focus()
        self.set_interval(0.5, self.refresh_tasks)

    def on_resize(self) -> None:
        self.set_class(self.app.size.height < 26, "compact")
        self.set_class(self.app.size.width < 80, "narrow")

    def refresh_tasks(self) -> None:
        infos = self.get_tasks()
        parents = self.get_parents()
        signature = tuple(
            (
                info.session_id,
                info.title,
                info.profile,
                info.status,
                info.result,
                info.error,
                parents.get(info.session_id, ""),
            )
            for info in infos
        )
        if signature == self._signature:
            # Polling must not repeatedly cancel a history render that takes
            # longer than the refresh interval on a busy terminal.
            loading = any(worker.node is self and worker.group == "task-history" for worker in self.workers)
            if self.selected_session_id and not loading:
                self.load_history(self.selected_session_id)
            return
        self._signature = signature
        self.infos = {info.session_id: info for info in infos}
        options = self.query_one("#task-options", AgentTree)
        options.update_tasks(infos, parents)
        ids = list(self.infos)
        if ids:
            selected = self.selected_session_id if self.selected_session_id in self.infos else ids[0]
            self.show_task(selected)
            options.call_after_refresh(options.move_cursor, options.task_nodes[selected])
        else:
            self.selected_session_id = ""
            self.query_one("#task-heading", Static).update("No subagents in this session.")
            self.query_one("#task-history", Vertical).remove_children()
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
        if event.session_id in self.infos:
            self.show_task(event.session_id)
            self.query_one("#task-followup", TextArea).focus()

    def show_task(self, session_id: str) -> None:
        composer = self.query_one("#task-followup", TextArea)
        changed = session_id != self.selected_session_id
        if changed:
            if self.selected_session_id:
                self._drafts[self.selected_session_id] = composer.text
            self.selected_session_id = session_id
            composer.load_text(self._drafts.get(session_id, ""))
            self._history_signature = ()
        info = self.infos[session_id]
        label = f"{info.title} [{info.session_id}]" if info.title else info.session_id
        self.query_one("#task-heading", Static).update(bounded(f"{label}  /  {info.profile}  /  {info.status}"))
        self.query_one("#task-send", Button).disabled = info.status in {"running", "cancelled"}
        self.query_one("#task-hint", Static).update(
            "Cancelled tasks cannot continue. Delegate a new task instead."
            if info.status == "cancelled"
            else "This agent is still working. You can draft a follow-up; send it after completion."
            if info.status == "running"
            else "Tab to write. Ctrl+Enter or Continue Agent sends to this agent and notifies the parent."
        )
        loading = any(worker.node is self and worker.group == "task-history" for worker in self.workers)
        if changed or not loading:
            self.load_history(session_id)

    @work(exclusive=True, group="task-history")
    async def load_history(self, session_id: str) -> None:
        if not self.is_attached or self.selected_session_id != session_id:
            return
        info = self.infos.get(session_id)
        if info is None:
            return
        try:
            history = self.query_one("#task-history", Vertical)
            scroll = self.query_one("#task-history-scroll", VerticalScroll)
        except NoMatches:
            return
        unavailable = False
        try:
            messages = await self.get_history(session_id)
        except (OSError, ValueError, RuntimeError):
            messages = []
            unavailable = True
        signature = (session_id, repr(messages), info.result, info.error, unavailable)
        if signature == self._history_signature:
            return
        # A mounted screen may already be losing its children. Update only the
        # original view, never a replacement mounted while history was awaited.
        if (
            self.is_attached
            and self.selected_session_id == session_id
            and history.is_attached
            and scroll.is_attached
            and self in history.ancestors
            and self in scroll.ancestors
        ):
            follow = scroll.max_scroll_y - scroll.scroll_y <= 3
            expanded = {card.call_id for card in history.query(ToolCard) if not card.collapsed}
            await history.remove_children()
            if not history.is_attached or self.selected_session_id != session_id:
                return
            cards: dict[str, ToolCard] = {}
            if unavailable:
                await history.mount(
                    Static("Saved history unavailable; showing the latest task snapshot.", markup=False)
                )
            if len(messages) > 40:
                await history.mount(Static("Showing the latest 40 messages.", markup=False))
            for message in messages[-40:]:
                if not history.is_attached or self.selected_session_id != session_id:
                    return
                if message.role in {"system", "developer"}:
                    continue
                content = (
                    message.content
                    if isinstance(message.content, str)
                    else "\n".join(part.text for part in message.content or [] if isinstance(part, TextContent))
                )
                if message.role == "tool":
                    call_id = message.tool_call_id or ""
                    card = cards.get(call_id)
                    if card is None:
                        card = ToolCard(call_id, message.name or "tool", animations=False)
                        cards[call_id] = card
                        await history.mount(card)
                    card.finish(content, content if content.startswith("Error:") else None, 0)
                else:
                    if content:
                        await history.mount(Turn(message.role, bounded(content)))
                    for call in message.tool_calls or []:
                        card = ToolCard(call.id, call.name, call.arguments, animations=False)
                        cards[call.id] = card
                        await history.mount(card)
            if not messages:
                await history.mount(Turn("user", bounded(info.prompt)))
                if info.result:
                    await history.mount(Turn("assistant", bounded(info.result)))
            if info.error:
                await history.mount(Static(bounded(info.error), classes="notice error", markup=False))
            for card in cards.values():
                card.collapsed = card.call_id not in expanded
                if info.status != "running" and card._outcome == "running":
                    card.cancel()
            self._history_signature = signature
            if follow:
                scroll.scroll_end(animate=False)

    def action_focus_pane(self) -> None:
        composer = self.query_one("#task-followup", Composer)
        if composer.has_focus:
            self.query_one("#task-history-scroll", VerticalScroll).focus(scroll_visible=False)
        else:
            composer.focus()

    @on(Composer.Submitted)
    def submitted(self, event: Composer.Submitted) -> None:
        event.stop()
        self.action_send()

    def action_send(self) -> None:
        info = self.infos.get(self.selected_session_id)
        text = self.query_one("#task-followup", TextArea).text.strip()
        if info is None or info.status in {"running", "cancelled"} or not text:
            self.query_one("#task-hint", Static).update("Choose a finished agent and enter a follow-up first.")
        elif len(text) > 16000:
            self.query_one("#task-hint", Static).update("Follow-ups are limited to 16,000 characters.")
        else:
            self._drafts[info.session_id] = text
            self.dismiss((info.session_id, text))

    def action_close(self) -> None:
        if self.selected_session_id:
            self._drafts[self.selected_session_id] = self.query_one("#task-followup", TextArea).text
        self.dismiss(None)

    @on(Button.Pressed, "#task-send")
    def send(self) -> None:
        self.action_send()

    @on(Button.Pressed, "#task-close")
    def close(self) -> None:
        self.action_close()
