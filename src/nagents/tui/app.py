"""A quiet, streaming terminal client for the production harness."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING
from typing import ClassVar

from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.screen import Screen
from textual.widgets import Button
from textual.widgets import Collapsible
from textual.widgets import Input
from textual.widgets import Markdown
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import RateLimitEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.commands import BUILTIN_COMMANDS
from nagents.harness.types import Notice
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage
from nagents.harness.types import TaskStarted
from nagents.harness.types import ToolOutput
from nagents.types import TextContent

from .clipboard import copy_native
from .commands import SlashMenu
from .dictation import DictationModal
from .login import DeviceLoginModal
from .login import LoginMethodModal
from .screens import ApprovalModal
from .screens import ChoiceModal
from .screens import DetailModal
from .screens import ModelModal
from .tasks import AgentTree
from .tasks import TaskScreen
from .themes import ACTIVITY_FPS
from .themes import activity_frame
from .themes import configure_theme
from .widgets import Composer
from .widgets import ToolCard
from .widgets import Turn

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import PurePath

    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.events import Click
    from textual.events import MouseDown
    from textual.events import Resize
    from textual.events import TextSelected
    from textual.widget import Widget

    from nagents.harness import Harness
    from nagents.harness.auth import DeviceAuthorization
    from nagents.harness.commands import Command
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import HarnessEvent

HELP = """KEYBOARD
Enter                 Send (never sends a multiline paste)
Shift+Enter           Insert a newline (Ctrl+J / Alt+Enter fallback)
Up / Down             Prompt history at start / end of text
While / menu is open  Up/Down select; Enter prefills; then Enter runs
Tab / Shift+Tab       Next / previous agent (configurable)
Esc                   Cancel work; deny an approval
Ctrl+P                Commands
Ctrl+N / Ctrl+L       New session / saved sessions
Ctrl+T                Agent tree / conversations
Ctrl+G                Dictation (opt-in; editable draft)
Mouse text selection  Copy automatically on release
Ctrl+C                Copy selection; otherwise cancel / exit
Ctrl+Shift+C           Copy selection without cancelling
Ctrl+Q                Quit

Tool cards expand with Enter or a click. Scroll up to pause
following the response; return to the bottom to follow again.
"""


class NagentsApp(App[None]):
    """Textual front end; the harness owns tools, credentials and persistence."""

    TITLE = "ngn"
    CSS_PATH: ClassVar[list[str | PurePath]] = ["app.tcss", "commands.tcss"]
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+l", "sessions", "Sessions", priority=True),
        Binding("ctrl+t", "tasks", "Agents", priority=True),
        Binding("ctrl+g", "dictation", "Dictation", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "interrupt", "Cancel / exit", priority=True),
        Binding("ctrl+shift+c,super+c", "copy_selection", "Copy", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, harness: Harness, *, resume_session: str = "", continue_session: bool = False) -> None:
        if resume_session and continue_session:
            raise ValueError("Choose either resume_session or continue_session, not both.")
        super().__init__()
        self.harness = harness
        configure_theme(self, harness.config.theme, background=harness.config.theme_background)
        self._resume_session = resume_session
        self._continue_session = continue_session
        self._active: asyncio.Task[None] | None = None
        self._backend_ready = False
        self._shutting_down = False
        self._last_error = ""
        self._compacting = False
        self._interrupt_at = 0.0
        self._answer: Markdown | None = None
        self._pending: list[str] = []
        self._answer_text = ""
        self._run_text = ""
        self._tools: dict[str, ToolCard] = {}
        self._running_tools: set[str] = set()
        self._usage = ""
        self._flush_lock = asyncio.Lock()
        self._dismissed_completion = ""
        self._status_text = "Starting..."
        self._status_error = False
        self._activity_started = monotonic()
        self._queued_prompts: deque[str] = deque()
        self._queue_paused = False
        self._task_drafts: dict[str, str] = {}
        self._clipboard_task: asyncio.Task[None] | None = None
        self._clipboard_pending: str | None = None
        self._mouse_selection_editor: TextArea | Input | None = None

    @property
    def busy(self) -> bool:
        return self._active is not None and not self._active.done()

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("ngn", id="brand", markup=False)
            yield Static(id="workspace", markup=False)
            yield Static(id="model", markup=False)
            yield Static(id="profile", markup=False)
        yield Static(id="mode", markup=False)
        with Horizontal(id="main"):
            with VerticalScroll(id="conversation", can_focus=True), Vertical(id="welcome"):
                yield Static("A little context.\nA clear next step.", id="welcome-title", markup=False)
                yield Static(
                    "An offline walkthrough. No API key or workspace writes."
                    if self.harness.config.demo
                    else "Work with your code, one conversation at a time.",
                    id="welcome-subtitle",
                    markup=False,
                )
                yield Button("Explain this workspace", id="prompt-explain", classes="suggestion")
                yield Button(
                    "Preview an approval" if self.harness.config.demo else "Review the current changes",
                    id="prompt-review",
                    classes="suggestion",
                )
                yield Static("/ for commands   ctrl+l to pick up where you left off", id="welcome-hint", markup=False)
            with Vertical(id="rail"):
                yield Static("AGENTS  /  CTRL+T", classes="section-label", markup=False)
                yield AgentTree()
                with (
                    Collapsible(title="Workspace & context", collapsed=True, id="rail-details"),
                    VerticalScroll(id="rail-metadata"),
                ):
                    yield Static("WORKSPACE", classes="section-label", markup=False)
                    yield Static(id="rail-workspace", markup=False)
                    yield Static("SESSION", classes="section-label", markup=False)
                    yield Static(id="rail-session", markup=False)
                    yield Static("CONFIGURATION", classes="section-label", markup=False)
                    yield Static(id="rail-config", markup=False)
                    yield Static("AUTHENTICATION", classes="section-label", markup=False)
                    yield Static(id="rail-auth", markup=False)
                yield Static(id="rail-activity", markup=False)
                yield Static("Arrows navigate\nEnter inspect / follow up", id="rail-note", markup=False)
        with Vertical(id="compose-area"):
            yield SlashMenu()
            yield Static(id="queue-status", markup=False)
            yield Static("MESSAGE", id="compose-label", markup=False)
            yield Composer(self.complete)
            yield Static(id="shortcuts", markup=False)
            yield Static("Starting...", id="status", markup=False)

    def on_mount(self) -> None:
        self.harness.approval_handler = self.request_approval
        self._refresh_context()
        self._resize_layout(self.size.width, self.size.height)
        self.query_one(Composer).focus()
        self.set_interval(0.075, self._flush_stream)
        self.set_interval(1 / ACTIVITY_FPS, self._animate_activity)
        self.set_interval(0.5, self._refresh_tasks)
        self._launch(self._initialize, "Opening workspace...")

    def on_resize(self, event: Resize) -> None:
        if self.query("#composer"):
            self._resize_layout(event.size.width, event.size.height)

    def _resize_layout(self, width: int, height: int) -> None:
        self.query_one("#rail").display = width > 110
        self.default_screen.set_class(width < 72 or height < 23, "small")
        hints = (
            f"Enter send  Shift+Enter newline  Tab {self.harness.config.tab_action}  Esc stop"
            if width < 72
            else f"Enter send   Shift+Enter newline   Tab {self.harness.config.tab_action}   Ctrl+P commands   Esc stop"
        )
        self.query_one("#shortcuts", Static).update(hints)
        self._size_composer()
        self._refresh_completions()

    @on(TextArea.Changed, "#composer")
    def composer_changed(self) -> None:
        if self._shutting_down:
            return
        self._interrupt_at = 0
        if self.query_one(Composer).text != self._dismissed_completion:
            self._dismissed_completion = ""
        self._size_composer()
        self._refresh_completions()

    def _refresh_tasks(self) -> None:
        if self._shutting_down:
            return
        tasks = self.harness.tasks.list()
        self.default_screen.query_one("#agent-tree", AgentTree).update_tasks(
            tasks, {task.id: task.parent_task_id for task in tasks}
        )

    @on(TextArea.SelectionChanged, "#composer")
    def composer_selection_changed(self) -> None:
        self._refresh_completions()

    def copy_to_clipboard(self, text: str) -> None:
        if self._shutting_down:
            return
        super().copy_to_clipboard(text)
        self._interrupt_at = 0
        if self.is_headless or not self.is_running:
            return
        self._clipboard_pending = text
        if self._clipboard_task is None or self._clipboard_task.done():
            self._clipboard_task = asyncio.create_task(self._copy_native_pending(), name="ngn-clipboard")

    async def _copy_native_pending(self) -> None:
        try:
            while self._clipboard_pending is not None:
                text, self._clipboard_pending = self._clipboard_pending, None
                # The terminal copy was already requested; never log clipboard content.
                with suppress(OSError, ValueError):
                    await copy_native(text, self.harness.workspace)
        finally:
            self._clipboard_pending = None

    def _copy_selection(self, *, editor_only: bool = False) -> bool:
        if self._shutting_down or not self.screen_stack:
            return False
        focused = self.screen.focused
        if isinstance(focused, TextArea | Input):
            if isinstance(focused, Input) and focused.password:
                return False
            text = focused.selected_text
            if text:
                self.copy_to_clipboard(text)
                return True
        if not editor_only and (screen_text := self.screen.get_selected_text()):
            self.copy_to_clipboard(screen_text)
            return True
        return False

    def action_copy_selection(self) -> None:
        self._copy_selection()

    def on_mouse_down(self, event: MouseDown) -> None:
        if self._shutting_down:
            return
        focused = self.focused if self.screen_stack else None
        self._mouse_selection_editor = (
            focused
            if isinstance(focused, TextArea | Input) and focused.region.contains(event.screen_x, event.screen_y)
            else None
        )

    def on_text_selected(self, event: TextSelected) -> None:
        if not self._shutting_down and self.screen_stack:
            # Mouse release precedes double-click word selection and editor
            # mouse-up handling. Read the completed selection after that work.
            self.call_after_refresh(self._copy_mouse_selection, self.screen)

    def on_mouse_up(self) -> None:
        if not self._shutting_down and self.screen_stack and self._mouse_selection_editor is not None:
            self.call_after_refresh(self._copy_mouse_selection, self.screen)

    def on_click(self, event: Click) -> None:
        if event.chain >= 2 and not self._shutting_down and self.screen_stack:
            self._mouse_selection_editor = event.widget if isinstance(event.widget, TextArea | Input) else None
            self.call_after_refresh(self._copy_mouse_selection, self.screen)

    def _copy_mouse_selection(self, screen: Screen[object]) -> None:
        focused, self._mouse_selection_editor = self._mouse_selection_editor, None
        if self._shutting_down or not self.screen_stack or screen is not self.screen:
            return
        if text := screen.get_selected_text():
            self.copy_to_clipboard(text)
        elif focused is not None and focused is screen.focused and self.mouse_captured is None:
            self._copy_selection(editor_only=True)

    def _refresh_completions(self) -> None:
        if self._shutting_down or not self.query("#slash-menu"):
            return
        composer = self.query_one(Composer)
        menu = self.query_one(SlashMenu)
        if composer.text != self._dismissed_completion:
            self._dismissed_completion = ""
        text = composer.text.lstrip()
        eligible = (
            not isinstance(self.screen, ModalScreen)
            and text.startswith("/")
            and not any(character.isspace() for character in text)
            and composer.selection.is_empty
            and composer.cursor_location == (0, len(composer.text))
            and composer.text != self._dismissed_completion
        )
        menu.display = eligible
        if eligible:
            menu.update_commands(self.harness.commands.list(), text[1:])

    def _hide_completions(self) -> None:
        composer = self.query_one(Composer)
        self._dismissed_completion = composer.text
        self.query_one(SlashMenu).display = False

    def complete(self, action: str) -> bool:
        # Resolve against current text and prefill before the next composer key.
        # Changed/Chosen messages can lag behind an entire terminal input burst.
        if action == "tab" and self.harness.config.tab_action != "complete":
            return False
        self._refresh_completions()
        menu = self.query_one(SlashMenu)
        if not menu.display:
            return False
        if action in {"up", "down"}:
            menu.move(1 if action == "down" else -1)
            return True
        if selected := menu.selected():
            self._use_command(selected)
            return True
        return False

    @on(SlashMenu.Chosen)
    def completion_chosen(self, event: SlashMenu.Chosen) -> None:
        self._use_command(event.command)

    def _use_command(self, command: Command) -> None:
        composer = self.query_one(Composer)
        text = f"/{command.name} "
        composer.load_text(text)
        composer.move_cursor((0, len(text)))
        self._hide_completions()
        composer.focus()

    @on(Composer.TabPressed)
    def tab_pressed(self, event: Composer.TabPressed) -> None:
        action = self.harness.config.tab_action
        if action in {"complete", "focus"}:
            if event.reverse:
                self.screen.focus_previous()
            else:
                self.screen.focus_next()
        elif self.busy:
            self._status("Finish or stop the current run before switching agents. Your draft is kept.")
        else:
            profiles = ["build", "agent", "reviewer", *self.harness.config.profiles]
            current = profiles.index(self.harness.config.agent)
            name = profiles[(current + (-1 if event.reverse else 1)) % len(profiles)]
            self._hide_completions()
            self._launch(lambda: self._change("/agent", name), f"Switching to {name}...")

    def _size_composer(self) -> None:
        composer = self.query_one(Composer)
        composer.styles.height = min(5 if self.size.height < 25 else 7, max(3, composer.wrapped_document.height + 2))

    def _status(self, text: str, *, error: bool = False, state: str = "") -> None:
        if self._shutting_down:
            return
        if error:
            self._last_error = text
        self._status_text = text
        self._status_error = error
        for selector in ("#status", "#rail-activity"):
            status = self.query_one(selector, Static)
            status.update(text)
            status.remove_class("error", "working", "ready", "warning")
            status.add_class("error" if error else state or ("working" if self.busy else "ready"))

    def _animate_activity(self) -> None:
        if self._shutting_down:
            return
        animated = self.busy and self.harness.config.animations and not self._status_error
        frame = (
            activity_frame(self.harness.config.theme, monotonic() - self._activity_started) + " " if animated else ""
        )
        self.query_one("#status", Static).update(frame + self._status_text)
        if self.query_one(SlashMenu).display:
            self._refresh_completions()

    def _refresh_context(self) -> None:
        config = self.harness.config
        auth_status = self.harness.auth_status()
        self.query_one("#workspace", Static).update(self.harness.workspace.name or str(self.harness.workspace))
        self.query_one("#model", Static).update(config.model)
        self.query_one("#profile", Static).update(config.agent)
        mode = self.query_one("#mode", Static)
        mode.display = config.demo
        mode.update("OFFLINE DEMO  /  no provider calls; no workspace writes" if config.demo else "")
        mode.set_class(config.demo, "demo")
        self.query_one("#rail-workspace", Static).update(str(self.harness.workspace))
        self.query_one("#rail-session", Static).update(self.harness.session_id or "New session")
        self.query_one("#rail-auth", Static).update(auth_status)
        self.query_one("#rail-config", Static).update(
            f"{config.provider}\n{config.model}\nProfile: {config.agent}\n"
            f"Plugins: {', '.join(config.plugins) or 'none'}\n"
            f"Project trust: {'enabled' if config.trust_project else 'not granted'}"
        )
        self._refresh_completions()

    def _launch(self, operation: Callable[[], Awaitable[None]], status: str) -> None:
        if self._shutting_down:
            return
        if self.busy:
            self._status("Already working. Esc cancels; your draft is kept.")
            return
        self._last_error = ""
        self._activity_started = monotonic()
        self._status(status, state="working")
        self._active = asyncio.create_task(self._work(operation), name="ngn-backend")

    async def _work(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
            await self._flush_stream()
            if self._last_error:
                self._status(self._last_error, error=True)
            else:
                self._status("Ready" + (f"  /  {self._usage}" if self._usage else ""), state="ready")
        except asyncio.CancelledError:
            if not self._shutting_down:
                await self._flush_stream()
                for call_id in self._running_tools:
                    self._tools[call_id].cancel()
                await self._notice("Cancelled. You can send another message.")
                self._status("Cancelled  /  ready for your next message", state="warning")
        except Exception as exc:
            self._queue_paused = True
            if not self._shutting_down:
                await self._flush_stream()
                await self._notice(str(exc) or type(exc).__name__, error=True)
        finally:
            self._compacting = False
            self._running_tools.clear()
            if not self._shutting_down:
                self._refresh_context()
                self._refresh_tasks()
            self._active = None
            if not self._shutting_down:
                self._update_queue()
                self.call_later(self._drain_queue)

    async def _initialize(self) -> None:
        if self._backend_ready:
            return
        await self.harness.initialize()
        resume, self._resume_session = self._resume_session, ""
        continue_session, self._continue_session = self._continue_session, False
        if resume:
            await self.harness.resume(resume)
        elif continue_session:
            sessions = await self.harness.list_sessions()
            if sessions:
                await self.harness.resume(sessions[0].id)
        self._backend_ready = True
        await self._load_history()

    async def _load_history(self) -> None:
        history = await self.harness.history()
        for message in history:
            content = message.content
            text = (
                content
                if isinstance(content, str)
                else "\n".join(part.text for part in content or [] if isinstance(part, TextContent))
            )
            if message.role in ("user", "assistant"):
                if text:
                    await self._add(Turn(message.role, text))
                if message.role == "user":
                    self.query_one(Composer).remember(text)
                for call in message.tool_calls:
                    await self._tool(call.id, call.name, call.arguments)
            elif message.role == "tool":
                card = await self._tool(message.tool_call_id or "", message.name or "tool")
                # Core history persists tool failures with this literal prefix.
                if text.startswith("Error:"):
                    card.finish(None, text, 0)
                else:
                    card.finish(text, None, 0)
            elif message.role == "compaction_summary":
                await self._notice("Earlier context was compacted.")

    async def _add(self, widget: Widget) -> None:
        conversation = self.query_one("#conversation", VerticalScroll)
        if conversation.max_scroll_y - conversation.scroll_y <= 3:
            conversation.anchor()
        welcome = conversation.query("#welcome")
        if welcome:
            welcome.first().display = False
        await conversation.mount(widget)

    async def _notice(self, text: str, *, error: bool = False, warning: bool = False) -> None:
        await self._add(
            Static(
                ("Error: " if error else "Warning: " if warning else "") + text,
                markup=False,
                classes="notice error" if error else "notice warning" if warning else "notice",
            )
        )
        if error:
            self._status(text, error=True)

    @on(Composer.Submitted)
    def submit(self, event: Composer.Submitted) -> None:
        command = event.text.strip()
        if self._shutting_down:
            return
        if not command:
            if self._queued_prompts and not self.busy:
                self._queue_paused = False
                self._drain_queue()
            return
        is_command = command.startswith("/")
        name = command.split(maxsplit=1)[0]
        custom_command = (
            is_command
            and self.harness.commands.get(name[1:]) is not None
            and all(builtin.name != name[1:] for builtin in BUILTIN_COMMANDS)
        )
        if (
            self.busy
            and is_command
            and not custom_command
            and name not in {"/", "/help", "/context", "/plugins", "/tasks", "/queue", "/quit"}
        ):
            self._status("Already working. Esc cancels; your draft is kept.")
            return
        if (not is_command or custom_command) and (self.busy or self._queued_prompts):
            if len(self._queued_prompts) >= 20:
                self._status(
                    "The queue is full (20 prompts). Your draft is kept; use /queue to inspect it.", error=True
                )
                return
            composer = self.query_one(Composer)
            composer.remember(event.text)
            composer.clear()
            self._queued_prompts.append(event.text)
            self._queue_paused = False
            self._update_queue()
            if self.busy:
                if self.harness.config.submit_mode == "interrupt" and self._backend_ready:
                    self._status("Stopping current work before running the new prompt...")
                    if self._active is not None and not self._active.cancelling():
                        self._active.cancel()
                else:
                    self._status(f"Queued prompt {len(self._queued_prompts)}  /  current work continues")
            else:
                self._drain_queue()
            return
        composer = self.query_one(Composer)
        self._hide_completions()
        composer.remember(event.text)
        composer.clear()
        if command.startswith("/"):
            self.command(command)
        else:
            self._launch(lambda: self._send(event.text), "Working  /  Esc to cancel")

    def _update_queue(self) -> None:
        label = self.query_one("#queue-status", Static)
        label.display = bool(self._queued_prompts)
        if self._queued_prompts:
            state = "paused; empty Enter resumes" if self._queue_paused else "waiting"
            preview = " ".join(self._queued_prompts[0].split())[:70]
            label.update(f"{len(self._queued_prompts)} queued ({state})  /  {preview}")

    def _drain_queue(self) -> None:
        if self._shutting_down or self.busy or self._queue_paused or not self._queued_prompts:
            return
        prompt = self._queued_prompts.popleft()
        self._update_queue()
        if prompt.lstrip().startswith("/"):
            self.command(prompt.strip())
            if not self.busy:
                self._queue_paused = True
                self._update_queue()
        else:
            self._launch(lambda: self._send(prompt), "Working on queued prompt  /  Esc to stop")

    async def _send(self, text: str, *, display_text: str = "", task_id: str = "") -> None:
        if not self._backend_ready:
            await self._initialize()
        self._answer = None
        self._answer_text = ""
        self._run_text = ""
        self._pending.clear()
        self._tools.clear()
        self._usage = ""
        if not task_id:
            await self._add(Turn("user", display_text or text))
        # Close the producer even if cancellation happens while rendering an event.
        stream = self.harness.continue_task(task_id, text) if task_id else self.harness.run(text)
        async with aclosing(stream) as events:
            async for event in events:
                await self._event(event)

    async def _text(self, text: str) -> None:
        if not text:
            return
        if self._answer is None:
            turn = Turn("assistant")
            await self._add(turn)
            assert isinstance(turn.body, Markdown)
            self._answer = turn.body
            self._answer_text = ""
        self._answer_text += text
        self._run_text += text
        self._pending.append(text)

    async def _flush_stream(self) -> None:
        async with self._flush_lock:
            if self._shutting_down or self._answer is None or not self._pending:
                return
            fragment = "".join(self._pending)
            self._pending.clear()
            conversation = self.query_one("#conversation", VerticalScroll)
            if conversation.max_scroll_y - conversation.scroll_y <= 3:
                conversation.anchor()
            await self._answer.append(fragment)

    async def _tool(self, call_id: str, tool: str, arguments: object = None) -> ToolCard:
        if call_id not in self._tools:
            await self._flush_stream()
            self._answer = None
            self._answer_text = ""
            card = ToolCard(call_id, tool, arguments)
            self._tools[call_id] = card
            await self._add(card)
        elif arguments is not None:
            self._tools[call_id].set_arguments(arguments)
        return self._tools[call_id]

    async def _event(self, event: HarnessEvent) -> None:
        if isinstance(event, TaskStarted):
            key = f"task:{event.task_id}" + (f":{event.followup}" if event.followup else "")
            await self._tool(
                key, f"subagent / {event.name}", {"task": event.prompt, "profile": event.profile, "depth": event.depth}
            )
            self._running_tools.add(key)
            self._refresh_tasks()
            self._status(f"{event.name} started in the background  /  /tasks for details")
        elif isinstance(event, TaskMessage):
            await self._flush_stream()
            self._answer = None
            self._answer_text = ""
            await self._add(Turn("user", f"Follow-up to {event.name}:\n\n{event.prompt}"))
            if self._task_drafts.get(event.task_id) == event.prompt:
                self._task_drafts.pop(event.task_id, None)
            self._refresh_tasks()
        elif isinstance(event, TaskCompleted):
            key = f"task:{event.task_id}" + (f":{event.followup}" if event.followup else "")
            card = await self._tool(key, f"subagent / {event.name}")
            if event.status == "cancelled":
                card.cancel()
            else:
                card.finish(event.result, event.error or None, 0)
            self._running_tools.discard(key)
            self._refresh_tasks()
            # Completion notifications are delivered between parent runs. Keep
            # the parent's follow-up separate from its previous streamed answer.
            await self._flush_stream()
            self._answer = None
            self._answer_text = ""
            self._run_text = ""
            self._status(f"{event.name} responded  /  parent continuing")
        elif isinstance(event, CompactionStartedEvent):
            self._compacting = True
            self._status("Compacting context...")
        elif isinstance(event, CompactionDoneEvent):
            self._compacting = False
            await self._notice(
                f"Context compacted: {event.original_message_count} -> {event.new_message_count} messages."
            )
        elif self._compacting and isinstance(event, (TextChunkEvent, TextDoneEvent, ReasoningChunkEvent, DoneEvent)):
            return
        elif isinstance(event, TextChunkEvent):
            await self._text(event.chunk)
        elif isinstance(event, TextDoneEvent):
            if not event.text or event.text == self._run_text:
                pass
            elif self._run_text and event.text.startswith(self._run_text):
                await self._text(event.text[len(self._run_text) :])
            elif not self._answer_text:
                await self._text(event.text)
            elif self._answer_text and event.text.startswith(self._answer_text):
                await self._text(event.text[len(self._answer_text) :])
            elif self._answer is not None:
                await self._flush_stream()
                self._run_text = self._run_text[: -len(self._answer_text)] + event.text
                self._answer_text = event.text
                await self._answer.update(event.text)
            await self._flush_stream()
        elif isinstance(event, ReasoningChunkEvent):
            self._status("Thinking  /  Esc to cancel")
        elif isinstance(event, ToolCallEvent):
            await self._tool(event.id, event.name, event.arguments)
            self._running_tools.add(event.id)
            self._status(f"Running {event.name}  /  Esc to cancel")
        elif isinstance(event, ToolOutput):
            card = await self._tool(event.call_id, event.tool)
            self._running_tools.add(event.call_id)
            card.append_output(event.text)
        elif isinstance(event, ToolResultEvent):
            card = await self._tool(event.id, event.name)
            card.finish(event.result, event.error, event.duration_ms)
            self._running_tools.discard(event.id)
            if event.error:
                self._status(f"Tool error: {event.error}", error=True)
        elif isinstance(event, Notice):
            await self._notice(event.text, error=event.level == "error", warning=event.level == "warning")
        elif isinstance(event, ErrorEvent):
            await self._notice(event.message, error=True)
        elif isinstance(event, RateLimitEvent):
            self._status(
                event.message or f"Rate limited; retry {event.attempt}/{event.max_retries} in {event.retry_after:g}s",
                state="warning",
            )
        elif isinstance(event, DoneEvent):
            if event.usage.has_usage():
                if event.usage.prompt_tokens or event.usage.completion_tokens:
                    self._usage = f"{event.usage.prompt_tokens:,} in / {event.usage.completion_tokens:,} out tokens"
                else:
                    self._usage = f"{event.usage.total_tokens:,} tokens"
            await self._flush_stream()

    async def request_approval(self, request: ApprovalRequest) -> bool:
        if self._shutting_down:
            return False
        self._hide_completions()
        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        modal = ApprovalModal(request)

        def decide(allowed: bool | None) -> None:
            if not result.done():
                result.set_result(allowed is True)

        self._status(f"Approval needed: {request.tool}", state="warning")
        try:
            await self.push_screen(modal, decide)
            return await result
        finally:
            if not self._shutting_down:
                if modal in self.screen_stack:
                    modal.dismiss(False)
                self._status("Working  /  Esc to cancel")
                self.query_one(Composer).focus()

    def action_cancel(self) -> None:
        if isinstance(self.screen, DictationModal):
            self.screen.run_worker(self.screen.action_cancel_dictation())
        elif isinstance(self.screen, TaskScreen):
            self.screen.action_close()
        elif isinstance(self.screen, DeviceLoginModal):
            self.screen.action_cancel_login()
        elif isinstance(self.screen, ApprovalModal):
            self.screen.action_deny()
        elif isinstance(self.screen, ModalScreen):
            self.screen.dismiss()
        elif self.query_one(SlashMenu).display:
            self._hide_completions()
        elif self.default_screen.query_one("#agent-tree", AgentTree).has_focus:
            self.query_one(Composer).focus()
        elif self.busy and self._active is not None and not self._active.cancelling():
            self._queue_paused = True
            self._update_queue()
            self._status("Cancelling...")
            self._active.cancel()

    def action_interrupt(self) -> None:
        if self._copy_selection():
            return
        if isinstance(self.screen, DictationModal):
            self.screen.run_worker(self.screen.action_cancel_dictation())
            return
        if isinstance(self.screen, DeviceLoginModal):
            self.screen.action_cancel_login()
            return
        if isinstance(self.screen, LoginMethodModal):
            self.screen.dismiss()
            return
        if self.busy:
            self._queue_paused = True
            self._update_queue()
            if self._active is not None and not self._active.cancelling():
                self._active.cancel()
            return
        now = monotonic()
        if now - self._interrupt_at < 2:
            self.exit()
        else:
            self._interrupt_at = now
            self._status("Press Ctrl+C again to exit, or Ctrl+Q to quit.")

    async def _shutdown(self) -> None:
        # Callbacks use mounted widgets: stop the backend before Textual tears them down.
        self._shutting_down = True
        self._queued_prompts.clear()
        try:
            for screen in reversed(self.screen_stack):
                if isinstance(screen, DictationModal):
                    await screen.close()
            if self.screen_stack and isinstance(self.screen, DeviceLoginModal):
                self.screen.clear_code()
            if self._active is not None:
                if not self._active.cancelling():
                    self._active.cancel()
                await asyncio.gather(self._active, return_exceptions=True)
            await self.harness.close()
        finally:
            try:
                if self._clipboard_task is not None:
                    await self._clipboard_task
            finally:
                await super()._shutdown()

    def action_commands(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self._hide_completions()
            commands = [
                (command.name, f"/{command.name}  {command.description}  [{command.source}]")
                for command in self.harness.commands.list()
            ]
            self.push_screen(ChoiceModal("COMMANDS", commands), self._chosen_command)

    def _chosen_command(self, command: str | None) -> None:
        if command:
            selected = self.harness.commands.get(command)
            if selected is not None:
                self._use_command(selected)

    def action_new_session(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.command("/new")

    def action_sessions(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.command("/sessions")

    def action_tasks(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._hide_completions()
        if self.query_one("#rail").display:
            tree = self.default_screen.query_one("#agent-tree", AgentTree)
            self._refresh_tasks()
            if tree.has_focus:
                self.query_one(Composer).focus()
            else:
                tree.focus()
        else:
            self._show_tasks()

    @on(AgentTree.Opened)
    def open_task(self, event: AgentTree.Opened) -> None:
        event.stop()
        self._show_tasks(event.task_id)

    def _show_tasks(self, selected_id: str = "") -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._hide_completions()
        self.push_screen(
            TaskScreen(
                self.harness.tasks.list,
                self.harness.task_history,
                parents=lambda: {task.id: task.parent_task_id for task in self.harness.tasks.list()},
                selected_id=selected_id,
                drafts=self._task_drafts,
            ),
            self._chosen_task_followup,
        )

    def _chosen_task_followup(self, result: tuple[str, str] | None) -> None:
        self.query_one(Composer).focus()
        if not result or self._shutting_down:
            return
        task_id, prompt = result
        if self.busy:
            self.run_worker(self._busy_task_followup(task_id, prompt), group="task-followups", exit_on_error=False)
        else:
            self._launch(lambda: self._send(prompt, task_id=task_id), "Continuing agent conversation...")

    async def _busy_task_followup(self, task_id: str, prompt: str) -> None:
        # A model run may settle between dismissing the inspector and scheduling
        # this worker. Keep any new idle run owned by the normal cancel/queue path.
        if not self.busy:
            self._launch(lambda: self._send(prompt, task_id=task_id), "Continuing agent conversation...")
            return
        if self.harness._busy != "run":
            self._status("Wait for the current operation to finish before continuing an agent. Your draft is kept.")
            return
        try:
            async with aclosing(self.harness.continue_task(task_id, prompt)) as events:
                async for event in events:
                    await self._event(event)
        except (ValueError, RuntimeError, PermissionError) as exc:
            self._status(f"Follow-up not sent; draft kept. {exc}", error=True)

    def action_dictation(self) -> None:
        if isinstance(self.screen, ModalScreen) or self._shutting_down:
            return
        self._hide_completions()
        if self.busy:
            self._status("Finish or stop current work before recording. Your draft is kept.")
        elif self.harness.config.demo:
            self._status("Dictation is disabled in offline demo; no microphone or transcription request was started.")
        elif not self.harness.config.dictation_enabled:
            self.push_screen(
                DetailModal(
                    "DICTATION / OPT-IN",
                    "Enable with --dictation or dictation_enabled = true in trusted TOML.\n\n"
                    "Install the voice extra and provide a separate transcription API key. "
                    "Ctrl+G or /dictate opens explicit recording controls; audio is never captured automatically. "
                    "The resulting text is an editable draft, never an automatically submitted message.",
                )
            )
        else:
            self.push_screen(DictationModal(self.harness.config), self._dictation_result)

    def _dictation_result(self, text: str | None) -> None:
        if self._shutting_down:
            return
        composer = self.query_one(Composer)
        if text:
            composer.insert(text)
            self._status("Dictation inserted into your draft. Review it, then Enter sends.")
        composer.focus()

    def command(self, text: str) -> None:
        self._hide_completions()
        parts = text.split(maxsplit=1)
        name = parts[0] if parts else "/"
        argument = parts[1].strip() if len(parts) > 1 else ""
        if name == "/":
            self.action_commands()
        elif name == "/quit":
            self.exit()
        elif name == "/help":
            commands = "\n".join(
                f"/{command.name} {command.argument_hint}\n  {command.description} [{command.source}]"
                for command in self.harness.commands.list()
            )
            self.push_screen(DetailModal("NGN / HELP", f"COMMANDS\n{commands}\n\n{HELP}"))
        elif name in ("/context", "/plugins"):
            try:
                self.push_screen(DetailModal(name[1:].upper(), self.harness.describe()))
            except Exception as exc:
                self._status(str(exc), error=True)
        elif name == "/tasks":
            self._show_tasks()
        elif name == "/dictate":
            self.action_dictation()
        elif name == "/queue":
            if argument == "clear":
                self._queued_prompts.clear()
                self._queue_paused = False
                self._update_queue()
                self._status("Queued prompts cleared. Current work was not stopped.")
            elif argument == "resume":
                self._queue_paused = False
                self._update_queue()
                self._drain_queue()
            elif argument:
                self._status("Usage: /queue [resume|clear]", error=True)
            else:
                text = (
                    "\n\n".join(f"{index}. {prompt}" for index, prompt in enumerate(self._queued_prompts, 1))
                    or "No queued prompts."
                )
                self.push_screen(DetailModal("PROMPT QUEUE", text))
        elif self.busy:
            self._status("Already working. Esc cancels before changing the session or configuration.")
        elif name == "/sessions":
            self._launch(self._sessions, "Loading sessions...")
        elif name == "/login":
            self.push_screen(LoginMethodModal(), self._chosen_login)
        elif name == "/logout":
            self._launch(self._logout, "Signing out...")
        elif name == "/agent" and not argument:
            profiles = [
                (name, f"{name}  /  {self.harness.config.profile(name).mode}")
                for name in ("build", "agent", "reviewer", *self.harness.config.profiles)
            ]
            self.push_screen(
                ChoiceModal("AGENT PROFILE", profiles),
                lambda value: self.command(f"/agent {value}") if value else None,
            )
        elif name == "/model" and not argument:
            self.push_screen(
                ModelModal(self.harness.config.model), lambda value: self.command(f"/model {value}") if value else None
            )
        elif name in ("/new", "/compact", "/agent", "/model"):
            self._launch(lambda: self._change(name, argument), f"{name[1:].capitalize()}...")
        elif self.harness.commands.get(name[1:]) is not None:
            self._launch(lambda: self._custom_command(name[1:], argument), f"Running {name}...")
        else:
            self._status(f"Unknown command: {name}. Use /help.", error=True)

    async def _custom_command(self, name: str, argument: str) -> None:
        if not self._backend_ready:
            await self._initialize()
        result = await self.harness.commands.execute(name, argument)
        if result.prompt:
            await self._send(result.prompt, display_text=f"/{name}{' ' + argument if argument else ''}")
        elif result.message:
            await self._notice(result.message)

    def _chosen_login(self, method: str | None) -> None:
        if method == "chatgpt":
            self._launch(self._login, "Starting ChatGPT/Codex device login...")
        elif method == "api-key":
            self.push_screen(
                DetailModal(
                    "API KEY / ENVIRONMENT SETUP",
                    "Provider API access uses usage-based billing, separate from ChatGPT subscriptions.\n\n"
                    f"Provide {self.harness.config.api_key_env} in ngn's launching environment using your secret manager. "
                    'Set auth = "api-key" in your ngn configuration, then restart ngn with the matching provider/model.\n\n'
                    "This dialog does not accept or store API keys. Do not paste a key into the conversation or "
                    "a command saved in shell history.\n\n"
                    "ChatGPT/Codex device login instead uses eligible subscription access for Codex; "
                    "it is not OAuth access to the general OpenAI API.",
                )
            )

    def _cancel_login(self) -> None:
        if self._active is not None and not self._active.cancelling():
            self._active.cancel()

    async def _login(self) -> None:
        if self.harness.config.demo:
            await self._notice(
                "Restart without --demo to sign in. Offline demo never starts network login.", error=True
            )
            return
        modal = DeviceLoginModal(self._cancel_login)
        finishing = False
        error = ""

        def closed(result: None) -> None:
            if not finishing:
                modal.cancel_requested = True
                modal.clear_code()
                self._cancel_login()

        try:
            await self.push_screen(modal, closed)
            async with asyncio.timeout(900) as timeout:

                async def show_code(authorization: DeviceAuthorization) -> None:
                    if modal.cancel_requested or self._shutting_down:
                        raise asyncio.CancelledError
                    remaining = modal.show_code(authorization)
                    if remaining <= 0:
                        raise TimeoutError
                    timeout.reschedule(asyncio.get_running_loop().time() + remaining)

                await self.harness.login(show_code)
                if modal.cancel_requested or self._shutting_down:
                    raise asyncio.CancelledError
        except TimeoutError:
            error = "Device login expired. Run /login for a new code and approve it before the timer ends."
        except Exception:
            # Network exceptions may embed credentials or response bodies. Never render them.
            error = (
                "Device login did not complete; the request may have been denied or expired. "
                "Use the default OpenAI provider without a custom endpoint, check your connection, "
                "and enable device code login in ChatGPT security/workspace settings, "
                "then retry /login."
            )
        finally:
            finishing = True
            modal.clear_code()
            if not self._shutting_down and modal in self.screen_stack:
                await modal.dismiss()
                self.query_one(Composer).focus()
        if error:
            await self._notice(error, error=True)
        else:
            self._usage = ""
            await self._notice(
                f"Signed in to ChatGPT/Codex. Model: {self.harness.config.model}. {self.harness.auth_status()}"
            )

    async def _logout(self) -> None:
        if self.harness.config.demo:
            await self._notice(
                "Restart without --demo to sign out. Demo does not change saved credentials.", error=True
            )
            return
        try:
            await self.harness.logout()
        except Exception:
            await self._notice(
                "Could not remove saved sign-in. Check local credential-store permissions and retry /logout.",
                error=True,
            )
        else:
            self._usage = ""
            await self._notice(
                f"Saved ChatGPT/Codex sign-in removed. API-key environment variables are unchanged. {self.harness.auth_status()}"
            )

    async def _clear_conversation(self) -> None:
        await self._flush_stream()
        self._answer = None
        self._answer_text = ""
        self._tools.clear()
        self._usage = ""
        conversation = self.query_one("#conversation", VerticalScroll)
        await conversation.remove_children(".turn, .tool-card, .notice")
        self.query_one("#welcome").display = True

    async def _change(self, name: str, argument: str) -> None:
        if not self._backend_ready:
            await self._initialize()
        if name == "/new":
            await self.harness.new_session()
            await self._clear_conversation()
        elif name == "/compact":
            await self._event(await self.harness.compact())
        elif name == "/agent":
            await self.harness.set_agent(argument)
            await self._notice(f"Profile changed to {self.harness.config.agent}.")
        elif name == "/model":
            await self.harness.set_model(argument)
            await self._notice(f"Model changed to {self.harness.config.model}.")

    async def _sessions(self) -> None:
        sessions = await self.harness.list_sessions()
        choices = [("__new__", "+ New session")]
        choices.extend(
            (session.id, f"{session.title or 'Untitled'}  /  {session.updated_at}  /  {session.id[:8]}")
            for session in sessions
        )
        self.push_screen(ChoiceModal("SESSIONS", choices), self._chosen_session)

    def _chosen_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        if session_id == "__new__":
            self.command("/new")
        else:
            self._launch(lambda: self._resume(session_id), "Resuming session...")

    async def _resume(self, session_id: str) -> None:
        await self.harness.resume(session_id)
        await self._clear_conversation()
        await self._load_history()

    @on(Button.Pressed, ".suggestion")
    def suggestion(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-explain":
            text = "Explain this workspace and its main entry points."
        else:
            text = "demo approval" if self.harness.config.demo else "Review the current changes for bugs and risks."
        composer = self.query_one(Composer)
        composer.load_text(text)
        composer.move_cursor((0, len(text)))
        composer.focus()
