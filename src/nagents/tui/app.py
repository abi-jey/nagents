"""A quiet, streaming terminal client for the production harness."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections import deque
from contextlib import aclosing
from contextlib import nullcontext
from contextlib import suppress
from time import monotonic
from time import time
from typing import TYPE_CHECKING
from typing import ClassVar
from uuid import uuid4

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import App
from textual.binding import Binding
from textual.color import Color
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

from nagents._async import join_owned
from nagents._version import framework_version
from nagents.error_presentation import transport_error_message
from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import RateLimitEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.events import ToolResultEvent
from nagents.harness import Harness
from nagents.harness.config import save_tui_performance
from nagents.harness.providers import login as provider_login
from nagents.harness.providers.credentials import ProviderLogin
from nagents.harness.runtime.commands import BUILTIN_COMMANDS
from nagents.harness.runtime.commands import Command
from nagents.harness.runtime.commands import split_command
from nagents.harness.runtime.followups import RunInput
from nagents.harness.tools.discovery import TOOL_ADDED_MESSAGE_NAME
from nagents.harness.tools.execution import host_run
from nagents.harness.types import Notice
from nagents.harness.types import TaskActivity
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskDeliveryWarning
from nagents.harness.types import TaskMessage
from nagents.harness.types import TaskStarted
from nagents.harness.types import ToolAddedNotice
from nagents.harness.types import ToolOutput
from nagents.harness.types import TranscriptAbandoned
from nagents.harness.types import TranscriptAnchor
from nagents.session import forks
from nagents.types import TextContent
from nagents.web.execution.approvals import ToolApprovals

from .activity import ACTIVITY_FPS
from .activity import STATES
from .activity import StateField
from .activity import StateName
from .activity import duration
from .channel import TuiChannelHost
from .clipboard import copy_native
from .commands import SlashMenu
from .delivery import DeliveryWidget
from .diagnostics import logger
from .lifecycle import Lifecycle
from .login import DeviceLoginModal
from .login import LoginMethodModal
from .login import OpenRouterLoginModal
from .login import ProviderKeyModal
from .performance import PerformanceMonitor
from .performance import PerformancePanel
from .providers import ProviderEditor
from .screens import ApprovalModal
from .screens import ChoiceModal
from .screens import DetailModal
from .screens import ModelModal
from .sweep import MOTION_STYLE
from .sweep import Sweep
from .tasks import AgentTree
from .tasks import TaskScreen
from .terminal_colors import TerminalColorReported
from .terminal_colors import TerminalColors
from .themes import configure_theme
from .voice import VoicePanel
from .voice import VoicePrompt
from .voice_settings import VoiceEdits
from .voice_settings import VoiceSettingsModal
from .wakeups import Wakeup
from .wakeups import Wakeups
from .widgets import Composer
from .widgets import SavedContext
from .widgets import ToolCard
from .widgets import ToolNotice
from .widgets import Turn
from .widgets import saved_context_title

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Sequence
    from pathlib import PurePath

    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.driver import Driver
    from textual.events import Click
    from textual.events import DescendantFocus
    from textual.events import Key
    from textual.events import MouseDown
    from textual.events import Paste
    from textual.events import Resize
    from textual.events import TextSelected
    from textual.filter import LineFilter
    from textual.timer import Timer
    from textual.widget import Widget
    from textual.worker import Worker

    from nagents.channels.delivery_types import DeliveryReceipt
    from nagents.channels.types import ChannelValue
    from nagents.harness.agents.subagents import TaskInfo
    from nagents.harness.providers.auth import DeviceAuthorization
    from nagents.harness.providers.login import LoginMethod
    from nagents.harness.providers.registry import ProviderProfile
    from nagents.harness.runtime.followups import RunTurn
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import HarnessEvent

    from .voice_runtime import TuiVoice

HELP = """KEYBOARD
Enter                 Send (never sends a multiline paste)
Shift+Enter           Insert a newline (Ctrl+J / Alt+Enter fallback)
Up / Down             Prompt history at start / end of text
While / menu is open  Up/Down select; Enter prefills; then Enter runs
Tab                   Complete selected slash command in the message editor
Shift+Tab             Keep focus in the message editor
F6                    Switch between message and conversation
In conversation       Tab/Shift+Tab navigate; PgUp/PgDn scroll
Esc                   Cancel work; deny an approval
Ctrl+P                Commands
Ctrl+N / Ctrl+L       New session / saved sessions
Ctrl+T                Agent tree / conversations
Ctrl+G                Start / end Voice (also /voice)
Mouse text selection  Copy automatically on release
Ctrl+C                Copy selection; otherwise cancel / exit
Ctrl+Shift+C           Copy selection without cancelling
Ctrl+Q                Quit

Tool cards expand with Enter, Space, or a click. Approval previews
scroll with PgUp/PgDn even while a decision button is focused.
Scroll up to pause
following the response; return to the bottom to follow again.
"""

TUI_COMMANDS = (
    Command(
        "perf", "TUI/runtime performance diagnostics", source="tui", argument_hint="[on|off|hide|detail] [global|local]"
    ),
    Command(
        "voice",
        "Live voice, local audio devices and shared Voice settings",
        source="tui",
        argument_hint="[start|stop|mute|settings|devices|status]",
    ),
)


class _AppLifecycle(Lifecycle):
    """Keep the production entrance brief without changing the motion studies."""

    @property
    def seconds(self) -> float:
        if self.phase == "intro":
            return 0.25
        if self.phase == "opening":
            return 0.5
        return super().seconds


class NagentsApp(App[None]):
    """Textual front end; the harness owns tools, credentials and persistence."""

    TITLE = "ngn"
    CSS_PATH: ClassVar[list[str | PurePath]] = ["app.tcss", "commands.tcss"]
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter,space", "startup_continue", "Continue", priority=True, show=False),
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+l", "sessions", "Sessions", priority=True),
        Binding("ctrl+t", "tasks", "Agents", priority=True),
        Binding("ctrl+g", "voice", "Voice", priority=True),
        Binding("ctrl+s", "send_queue", "Send queued", priority=True, show=False),
        Binding("f6", "focus_pane", "Message / conversation", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "interrupt", "Cancel / exit", priority=True),
        Binding("ctrl+shift+c,super+c", "copy_selection", "Copy", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        harness: Harness,
        *,
        resume_session: str = "",
        continue_session: bool = False,
        pick_session: bool = False,
        startup: bool = False,
    ) -> None:
        if sum(bool(value) for value in (resume_session, continue_session, pick_session)) > 1:
            raise ValueError("Choose one of resume_session, continue_session, or pick_session.")
        self._startup_pending = startup and harness.config.animations
        self.startup_opened = not self._startup_pending
        self._startup_complete = asyncio.Event()
        if not self._startup_pending:
            self._startup_complete.set()
        self._sweep = Sweep(
            0.0 if harness.config.animations else 1.0,
            enabled=harness.config.animations,
            frame=self._startup_pending,
        )
        # NO_COLOR must use native defaults, rather than guessed RGB black.
        # Once the theme is registered below, return ANSI mode to theme control.
        super().__init__(ansi_color=True)
        self.harness = harness
        self._tool_approvals: ToolApprovals | None = None
        if not harness.config.animations:
            self.animation_level = "none"
        self._terminal_colors = TerminalColors()
        configure_theme(self, harness.config.theme, background=harness.config.theme_background)
        self.ansi_color = None
        self._resume_session = resume_session
        self._continue_session = continue_session
        self._pick_session = pick_session
        self._active: asyncio.Task[None] | None = None
        self._skill_completion_worker: Worker[None] | None = None
        self._skill_commands_enabled = False
        self._backend_ready = False
        self._preinitialized: asyncio.Task[None] | None = None
        self._entrance_text = ""
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
        self.started_at = self._activity_started
        self.exit_session_id = ""
        self.exit_session_saved = False
        self._motion_tick = monotonic()
        self._motion = StateField()
        self._motion_lifecycle = (_AppLifecycle if self._startup_pending else Lifecycle)(
            "sweep",
            "intro" if self._startup_pending else "opening" if harness.config.animations else "ready",
            self._motion_tick,
        )
        self._rendered_status = Text()
        self._closing_requested = False
        self._done_at = 0.0
        self._task_fields: dict[str, StateField] = {}
        self._task_states: dict[str, StateName] = {}
        self._task_tools: dict[str, str] = {}
        self._visible_task_ids: set[str] = set()
        self._activity_tasks: list[TaskInfo] = []
        self._recent_task_id = ""
        self._recent_task_until = 0.0
        self._wakeups = Wakeups()
        self._wake_field = StateField("sleeping", offset=3.4)
        self._owns_wakeups = False
        self._queued_prompts: deque[str | VoicePrompt] = deque()
        self._queue_paused = False
        self._queue_send_armed = False
        self._task_drafts: dict[str, str] = {}
        self._pending_task_notes: dict[tuple[str, str], tuple[str, str]] = {}
        self._task_note_run = ""
        self._clipboard_task: asyncio.Task[None] | None = None
        self._clipboard_pending: str | None = None
        self._mouse_selection_editor: TextArea | Input | None = None
        self._channel_host: TuiChannelHost | None = None
        self._delivery_ids: set[str] = set()
        self._delivery_anchors: dict[tuple[int, int], Widget] = {}
        self._unanchored_tools: dict[int, tuple[ToolCallEvent, ToolCard]] = {}
        self._called_tools: set[str] = set()
        self._tool_previews: dict[tuple[str, int], ToolCard] = {}
        self._delivery_dirty = False
        self._delivery_session = ""
        self._performance = PerformanceMonitor(interval=0.25, max_samples=240)
        self._performance_enabled = harness.config.tui_performance
        self._performance_visible = self._performance_enabled
        self._performance_last_log = monotonic()
        self._performance_last_render = 0.0
        self._performance_timer: Timer | None = None
        self._voice: TuiVoice | None = None
        self._voice_jobs: set[asyncio.Task[None]] = set()
        self._voice_inputs: dict[tuple[str, str], VoicePrompt] = {}
        self._voice_turns: deque[VoicePrompt] = deque()

    @property
    def busy(self) -> bool:
        return self._active is not None and not self._active.done()

    def get_driver_class(self) -> type[Driver]:
        driver = super().get_driver_class()
        if os.name == "posix":
            from textual.drivers.linux_driver import LinuxDriver

            from .terminal_driver import TerminalDriver

            if driver is LinuxDriver:
                return TerminalDriver
        return driver

    def on_terminal_color_reported(self, event: TerminalColorReported) -> None:
        if self._shutting_down or self._terminal_colors == event.colors:
            return
        self._terminal_colors = event.colors
        configure_theme(
            self,
            self.harness.config.theme,
            background=self.harness.config.theme_background,
            terminal=event.colors,
        )
        self.refresh(layout=False)

    def compose(self) -> ComposeResult:
        with Vertical(id="app-shell"):
            yield from self._compose_workspace()

    def _compose_workspace(self) -> ComposeResult:
        yield Static(id="mode", markup=False)
        with Horizontal(id="main"):
            with VerticalScroll(id="conversation", can_focus=True) as conversation, Vertical(id="welcome"):
                conversation.border_title = "Conversation / F6"
                if self.harness.config.demo:
                    yield Static(
                        "An offline walkthrough. No API key or workspace writes.", id="welcome-subtitle", markup=False
                    )
                yield Button("Explain this workspace", id="prompt-explain", classes="suggestion")
                yield Button(
                    "Preview an approval" if self.harness.config.demo else "Review the current changes",
                    id="prompt-review",
                    classes="suggestion",
                )
            with Vertical(id="rail") as rail:
                rail.border_title = "Agents / Ctrl+T"
                yield AgentTree(compact=True)
                yield Static(id="background-status", markup=False)
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
                yield PerformancePanel(self._performance, id="performance-panel")
        with Vertical(id="compose-area") as compose_area:
            compose_area.border_title = "Message"
            yield SlashMenu()
            yield Static(id="queue-status", markup=False)
            yield VoicePanel()
            yield Composer(self.complete)
            with Vertical(id="footer"):
                yield Static("Starting...", id="status", markup=False)
                yield Static(id="provider-model", markup=False)

    def _complete_inline_entrance(self) -> None:
        """Prepare a fully visible first frame before Textual mounts.

        The normal-screen prelude already supplied startup motion. Do not run
        another foreground sweep (or a collapsed startup shell) on handoff.
        """
        self._startup_pending = False
        self.startup_opened = True
        self._startup_complete.set()
        self._motion_lifecycle.phase = "ready"
        self._sweep = Sweep()

    def on_mount(self) -> None:
        self.harness.approval_handler = self.request_approval
        if isinstance(self.harness, Harness) and self.harness.wakeup_handler is None:
            self.harness.wakeup_handler = self._schedule_wakeup
            self._owns_wakeups = True
        self._motion_lifecycle.since = self._motion_tick = monotonic()
        self.query_one("#background-status").display = False
        self._refresh_context()
        self._resize_layout(self.size.width, self.size.height)
        self.query_one(Composer).focus()
        self.set_interval(0.075, self._flush_stream)
        self.set_interval(1 / ACTIVITY_FPS, self._animate_activity)
        self.set_interval(0.5, self._refresh_tasks)
        self._set_performance(self._performance_enabled)
        if self._startup_pending:
            self.query_one("#app-shell").add_class("startup-shell")
            self.query_one("#app-shell").border_title = f"ngn {framework_version()}"
            self.query_one("#app-shell").border_subtitle = "Enter / Space to continue · Esc to exit"
            self.query_one("#app-shell").disabled = True
            self.set_focus(None)
            self._animate_activity()
        if self._entrance_text:
            self.query_one(Composer).insert(self._entrance_text)
            self._entrance_text = ""
        self._launch(self._initialize, "Opening workspace...")

    def _finish_startup(self) -> None:
        if not self._startup_pending or self._shutting_down or self._closing_requested:
            return
        self._startup_pending = False
        self.startup_opened = True
        self.query_one("#app-shell").styles.height = "100%"
        self.query_one("#app-shell").disabled = False
        self.query_one("#app-shell").border_subtitle = ""
        self.query_one(Composer).focus()
        self._motion_lifecycle.phase = "ready"
        self._sweep = Sweep()
        self.screen.refresh(layout=True)
        self._startup_complete.set()

    def action_startup_continue(self) -> None:
        self._finish_startup()

    def on_key(self, event: Key) -> None:
        if self._startup_pending and event.is_printable:
            self._finish_startup()
            if self.startup_opened:
                self.query_one(Composer).insert(event.character or "")
                event.stop()
                event.prevent_default()

    def on_paste(self, event: Paste) -> None:
        if self._startup_pending:
            self._finish_startup()
            if self.startup_opened:
                self.query_one(Composer).insert(event.text)
                event.stop()
                event.prevent_default()

    def _set_performance(self, enabled: bool) -> None:
        self._performance_enabled = enabled
        self._performance.enabled = enabled
        if self._performance_timer is not None:
            self._performance_timer.stop()
            self._performance_timer = None
        if enabled:
            self._performance_timer = self.set_interval(self._performance.interval, self._sample_performance)
        self.query_one("#performance-panel").display = enabled and self._performance_visible

    def _sample_performance(self) -> None:
        if self._shutting_down or not self._performance_enabled:
            return
        counters = {
            "running_tools": len(self._running_tools),
            "running_agents": sum(task.status == "running" for task in self._activity_tasks),
            "queued_prompts": len(self._queued_prompts),
            "backlog_chars": sum(map(len, self._pending)),
        }
        self._performance.tick(counters)
        now = monotonic()
        if now - self._performance_last_render >= 1:
            self._performance_last_render = now
            self.query_one("#performance-panel", PerformancePanel).refresh_metrics()
        if now - self._performance_last_log >= 30:
            self._performance_last_log = now
            snapshot = self._performance.snapshot()
            logger.info(
                "Performance summary",
                extra={
                    "phase": "performance",
                    "session_id": self.harness.session_id,
                    "lag_max_ms": snapshot.loop_lag.max_ms,
                    "cpu_percent": snapshot.cpu_percent,
                    **counters,
                },
            )

    def _performance_command(self, argument: str) -> None:
        parts = argument.split()
        action = parts[0] if parts else "detail"
        if action in {"on", "off"} and len(parts) <= 2:
            scope = parts[1] if len(parts) == 2 else "local"
            if scope not in {"global", "local"}:
                self._status("Usage: /perf on|off [global|local]", error=True)
                return
            try:
                path = save_tui_performance(
                    self.harness.config, action == "on", "global" if scope == "global" else "local"
                )
            except (OSError, ValueError) as exc:
                self._status(f"Could not save performance setting: {exc}", error=True)
                return
            self._performance_visible = action == "on"
            self._set_performance(action == "on")
            self._status(f"Performance {action}; saved {scope}: {path}")
        elif action == "hide" and len(parts) == 1:
            self._performance_visible = False
            self.query_one("#performance-panel").display = False
        elif action == "detail" and len(parts) <= 1:
            panel = self.query_one("#performance-panel", PerformancePanel)
            self.push_screen(DetailModal("PERFORMANCE / process-local", panel.render().plain))
        else:
            self._status("Usage: /perf [on|off [global|local]|hide|detail]", error=True)

    def on_unmount(self) -> None:
        if self._performance_timer is not None:
            self._performance_timer.stop()
            self._performance_timer = None
        self._performance.enabled = False

    def get_line_filters(self) -> Sequence[LineFilter]:
        filters = super().get_line_filters()
        return [self._sweep, *filters] if self._sweep.enabled else filters

    def _motion_state(self, state: StateName) -> None:
        if self._motion.state != state:
            self._motion.select(state, immediate=not self.harness.config.animations or self.animation_level == "none")

    def on_resize(self, event: Resize) -> None:
        if self.query("#composer"):
            self._resize_layout(event.size.width, event.size.height)

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        # Editor blink timers are independent of Textual's scroll animator.
        if self.animation_level == "none" and isinstance(event.widget, TextArea | Input):
            event.widget.cursor_blink = False

    def _resize_layout(self, width: int, height: int) -> None:
        self.query_one("#rail").display = width > 110
        self.default_screen.set_class(width < 72 or height < 23, "small")
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
        with self._performance.measure("task refresh") if self._performance_enabled else nullcontext():
            self._refresh_task_content()

    def _refresh_task_content(self) -> None:
        if self._shutting_down:
            return
        tasks = self.harness.tasks.list()
        self._activity_tasks = [task for task in tasks if task.depth == 1]
        self.default_screen.query_one("#agent-tree", AgentTree).update_tasks(
            tasks, {task.session_id: task.parent_session_id for task in tasks}
        )
        if (
            self._owns_wakeups
            and self._backend_ready
            and not self.busy
            and not self._queued_prompts
            and not self._closing_requested
        ):
            due = self._wakeups.claim(self.harness.session_id)
            if due:
                self._launch(lambda: self._wake(due[0]), "Waking up...")
                self._motion_state("waking")
            if self._wakeups.error:
                self._status(self._wakeups.error, error=True)
                self._wakeups.error = ""

    async def _schedule_wakeup(self, session_id: str, delay: float, reason: str) -> dict[str, str]:
        if self._shutting_down or self._closing_requested or not self.busy:
            raise RuntimeError("Wake-ups require an active TUI run")
        return self._wakeups.schedule(self.harness.session_id, session_id, delay, reason)

    async def _wake(self, wakeup: Wakeup) -> None:
        await self._notice(f"Wake-up: {wakeup.reason}")
        await self._send(wakeup.reason, session_id=wakeup.session_id, wakeup=True)

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
            menu.update_commands(self._completion_commands(), text[1:])
            if self._backend_ready:
                self._skill_completion_worker = self.run_worker(
                    self._discover_command_skills, group="skill-completions", exclusive=True, exit_on_error=False
                )

    async def _discover_command_skills(self) -> None:
        """Refresh suggestions in an owned, replaceable worker, never in list()."""
        try:
            await self.harness.agent.refresh_skills()
            self._skill_commands_enabled = (
                any(command.source == "skill" for command in self.harness.commands.list())
                and self.harness.commands.skills_enabled()
            )
        except Exception:
            # Command execution reports discovery failures explicitly. A menu
            # refresh must not interrupt an in-flight run or discard its draft.
            self._skill_commands_enabled = False
        if self._shutting_down or not self.query("#slash-menu"):
            return
        menu = self.query_one(SlashMenu)
        if menu.display:
            text = self.query_one(Composer).text.lstrip()
            if text.startswith("/") and not any(character.isspace() for character in text):
                menu.update_commands(self._completion_commands(), text[1:])

    def _client_commands(self) -> list[Command]:
        local = {command.name for command in TUI_COMMANDS}
        return [command for command in self.harness.commands.list() if command.name not in local] + list(TUI_COMMANDS)

    def _completion_commands(self) -> list[Command]:
        return [
            command for command in self._client_commands() if command.source != "skill" or self._skill_commands_enabled
        ]

    def _hide_completions(self) -> None:
        composer = self.query_one(Composer)
        self._dismissed_completion = composer.text
        self.query_one(SlashMenu).display = False

    def complete(self, action: str) -> bool:
        # Resolve against current text and prefill before the next composer key.
        # Changed/Chosen messages can lag behind an entire terminal input burst.
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

    def action_focus_pane(self) -> None:
        if isinstance(self.screen, TaskScreen):
            self.screen.action_focus_pane()
            return
        if self.screen is not self.default_screen:
            return
        composer = self.query_one(Composer)
        if composer.has_focus:
            self._hide_completions()
            self.query_one("#conversation", VerticalScroll).focus(scroll_visible=False)
        else:
            composer.focus()

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
        if error:
            self._motion_state("error")
        elif state == "ready":
            self._motion_state("done")
            self._done_at = monotonic()
        elif state == "working":
            self._motion_state("working" if self._backend_ready else "starting")
        logger.info("TUI status: %s", text, extra={"session_id": self.harness.session_id, "phase": self._motion.state})
        for selector in ("#status", "#rail-activity"):
            status = self.query_one(selector, Static)
            if selector == "#status":
                field = self._motion.render(aperture=self._motion_lifecycle.aperture(monotonic()))
                field.stylize(MOTION_STYLE)
                field.append(" " + self._status_label(monotonic()))
                status.update(field, layout=False)
                self._rendered_status = field
            else:
                status.update(text)
            status.remove_class("error", "working", "ready", "warning")
            status.add_class("error" if error else state or ("working" if self.busy else "ready"))

    def _animate_activity(self) -> None:
        if self._shutting_down or not self.is_running:
            return
        now = monotonic()
        animated = self.harness.config.animations and self.animation_level != "none"
        if self._startup_pending:
            self.query_one("#app-shell").border_subtitle = Text(
                f"{self._status_label(now)} · Enter to skip · Esc to exit", style=MOTION_STYLE
            )
            if not animated:
                self._finish_startup()
            else:
                from .lifecycle import ease

                progress = self._motion_lifecycle.progress(now)
                if self._motion_lifecycle.phase == "intro":
                    self.query_one("#app-shell").styles.height = max(2, round(self.size.height * ease(progress)))
        if not animated and self._motion_lifecycle.phase == "opening":
            self._motion_lifecycle.phase = "ready"
        if self._motion_lifecycle.advance(now):
            self.exit()
            return
        if self._motion_lifecycle.phase == "closed":
            return
        if self._startup_pending and self._motion_lifecycle.phase == "ready":
            self._finish_startup()
        if self._motion.state == "done" and not self.busy and now - self._done_at >= 2:
            self._motion_state("idle")
        if animated:
            self._motion.advance(now - self._motion_tick)
            for identifier in self._visible_task_ids:
                self._task_fields[identifier].advance(now - self._motion_tick)
            self._wake_field.advance(now - self._motion_tick)
        self._motion_tick = now
        field = self._motion.render(aperture=self._motion_lifecycle.aperture(now))
        field.stylize(MOTION_STYLE)
        field.append(" " + self._status_label(now))
        if field != self._rendered_status:
            self.default_screen.query_one("#status", Static).update(field, layout=False)
            self._rendered_status = field
        self._render_background_activity(now)
        was_sweeping = self._sweep.enabled
        sweeping = animated and self._motion_lifecycle.phase in {"intro", "opening", "closing"}
        if sweeping:
            self._sweep = Sweep(
                self._motion_lifecycle.visibility(now),
                accent=self._motion.stops[1],
                enabled=True,
                frame=self._startup_pending,
            )
        elif was_sweeping:
            self._sweep = Sweep()
        if sweeping or was_sweeping:
            self.screen.refresh()
        if self.query_one(SlashMenu).display:
            self._refresh_completions()

    def _status_label(self, now: float) -> str:
        elapsed = f" · {duration(now - self._activity_started)}" if self.busy and not self._status_error else ""
        detail = f" · {self._status_text}{elapsed}" if self._status_text else ""
        return self.harness.config.agent + detail

    def _render_background_activity(self, now: float) -> None:
        running = [task for task in self._activity_tasks if task.status == "running"]
        pending = self._wakeups.upcoming(self.harness.session_id)
        shown = running[: 2 if pending else 3] or [
            task
            for task in self._activity_tasks
            if task.session_id == self._recent_task_id and now < self._recent_task_until
        ]
        content = Text()
        self._visible_task_ids = {task.session_id for task in shown}
        phases: dict[str, StateName] = {
            "starting": "starting",
            "tool": "tool",
            "tool_call": "tool",
            "reasoning": "working",
            "responding": "responding",
            "waiting": "delegated",
            "approval": "approval",
        }
        for index, task in enumerate(shown):
            if index:
                content.append("\n")
            state: StateName = (
                "error"
                if task.status == "failed"
                else "idle"
                if task.status == "cancelled"
                else "done"
                if task.status != "running"
                else self._task_states.get(task.session_id, phases.get(task.phase, "working"))
            )
            if task.session_id not in self._task_fields:
                self._task_fields[task.session_id] = StateField(state, offset=index + 1.7)
            field = self._task_fields[task.session_id]
            field.select(state, immediate=not self.harness.config.animations or self.animation_level == "none")
            dots = field.render(aperture=self._motion_lifecycle.aperture(now))
            dots.stylize(MOTION_STYLE)
            content.append_text(dots)
            detail = (
                "Cancelled"
                if task.status == "cancelled"
                else self._task_tools.get(task.session_id)
                if state == "tool"
                else STATES[state].label
            )
            end = time() if task.status == "running" else task.completed_at or task.updated_at
            elapsed = f" · {duration(max(0, end - task.started_at))}" if task.started_at > 0 else ""
            content.append(self._inline(f" {task.profile} · {task.label} · {detail or STATES[state].label}") + elapsed)
        if pending:
            if shown:
                content.append("\n")
            dots = self._wake_field.render(aperture=self._motion_lifecycle.aperture(now))
            dots.stylize(MOTION_STYLE)
            content.append_text(dots)
            nearest = pending[0]
            remaining = nearest.deadline - now
            when = f"in {duration(remaining + 0.99)}" if remaining > 0 else "due · waiting for the current turn"
            content.append(self._inline(f" Wake-up {when} · {nearest.reason}"))
        widget = self.default_screen.query_one("#background-status", Static)
        widget.display = bool(shown or pending)
        widget.tooltip = Text("\n".join(self._inline(f"{item.due_at} · {item.reason}") for item in pending))
        if widget.content != content:
            widget.update(content)

    @staticmethod
    def _inline(text: str) -> str:
        return "".join(character if character.isprintable() else " " for character in text)[:240]

    def _refresh_context(self) -> None:
        config = self.harness.config
        auth_status = self.harness.auth_status()
        mode = self.query_one("#mode", Static)
        mode.display = config.demo
        mode.update("OFFLINE DEMO  /  no provider calls; no workspace writes" if config.demo else "")
        mode.set_class(config.demo, "demo")
        self.query_one("#rail-workspace", Static).update(str(self.harness.workspace))
        self.query_one("#rail-session", Static).update(self.harness.session_id or "New session")
        self.query_one("#rail-auth", Static).update(auth_status)
        self._refresh_model()
        self.query_one("#rail-config", Static).update(
            f"{config.provider_profile().kind}\n{config.model}\nProfile: {config.agent}\n"
            f"Plugins: {', '.join(config.plugins) or 'none'}\n"
            f"Project trust: {'enabled' if config.trust_project else 'not granted'}"
        )
        self._refresh_completions()

    def _refresh_model(self) -> None:
        config = self.harness.config
        provider = config.provider or config.provider_profile().kind
        model = self.harness.agent.provider.model if isinstance(self.harness, Harness) else config.model
        variables = self.get_css_variables()
        text = Text(self._inline(provider), style=Style(color=Color.parse(variables["ngn-info"]).rich_color))
        text.append(" · ", style="dim")
        text.append(
            self._inline(model or config.model),
            style=Style(color=Color.parse(variables["ngn-assistant"]).rich_color, bold=True),
        )
        widget = self.query_one("#provider-model", Static)
        if widget.content != text:
            widget.update(text, layout=False)

    def _launch(self, operation: Callable[[], Awaitable[None]], status: str) -> None:
        if self._shutting_down or self._closing_requested:
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
                self._status(self._usage, state="ready")
        except asyncio.CancelledError:
            if not self._shutting_down:
                await self._flush_stream()
                for call_id in self._running_tools:
                    self._tools[call_id].cancel()
                await self._notice("Cancelled. You can send another message.")
                self._status("Cancelled", state="warning")
                self._motion_state("idle")
        except Exception as exc:
            self._queue_paused = True
            if not self._shutting_down:
                await self._flush_stream()
                await self._notice(str(exc) or type(exc).__name__, error=True)
        finally:
            self._compacting = False
            self._cancel_tool_previews()
            self._running_tools.clear()
            if not self._shutting_down:
                self._refresh_context()
                self._refresh_tasks()
            self._active = None
            if not self._shutting_down:
                # Final status was painted while this task still counted as busy.
                # Repaint now, rather than leave elapsed time until the next tick.
                self._animate_activity()
                self._update_queue()
                self.call_later(self._drain_queue)

    async def _initialize(self) -> None:
        if self._backend_ready:
            return
        resume, self._resume_session = self._resume_session, ""
        continue_session, self._continue_session = self._continue_session, False
        pick, self._pick_session = self._pick_session, False
        # Resuming or choosing must not first register an empty session for this run.
        if self._preinitialized is None:
            await self.harness.initialize(create_session=not (resume or continue_session or pick))
        else:
            await self._preinitialized
        if self._channel_host is None and isinstance(self.harness, Harness):
            host = TuiChannelHost(self.harness, self._delivery_notice)
            host.open()
            self._channel_host = host
        if resume:
            await self.harness.resume(resume)
        elif continue_session:
            sessions = await self.harness.list_sessions()
            if sessions:
                await self.harness.resume(sessions[0].id)
        self._backend_ready = True
        await self._load_history()
        if pick:
            await self._sessions()

    async def _load_history(self) -> None:
        self._cancel_tool_previews()
        self._delivery_ids.clear()
        self._delivery_anchors.clear()
        self._unanchored_tools.clear()
        self._called_tools.clear()
        self._delivery_session = self.harness.session_id
        deliveries: tuple[DeliveryReceipt, ...] = ()
        host = self._channel_host
        if host is not None and host.supports_history and self.harness._session_created:
            snapshot = await host.snapshot(self._delivery_session)
            rows = snapshot.rows
            deliveries = snapshot.deliveries.current
            if snapshot.deliveries.earlier:
                await self._earlier_deliveries(snapshot.deliveries.earlier)
        else:
            rows = tuple((0, message) for message in await self.harness.history())
        # Build the saved transcript off-DOM. Mounting each row separately causes
        # repeated layout of the growing conversation and replays live tool effects.
        widgets: list[Widget] = []
        composer = self.query_one(Composer)
        receipts: dict[tuple[int, int], list[DeliveryReceipt]] = {}
        for receipt in deliveries:
            key = (receipt.origin.anchor_message_id, receipt.origin.call_position)
            receipts.setdefault(key, []).append(receipt)

        def tool(call_id: str, name: str, arguments: object = None) -> ToolCard:
            if call_id not in self._tools:
                card = ToolCard(
                    call_id,
                    name,
                    arguments,
                    default_timeout=self.harness.config.shell_timeout if name == "shell" else 0,
                    restored=True,
                )
                self._tools[call_id] = card
                widgets.append(card)
            return self._tools[call_id]

        for index, (row_id, message) in enumerate(rows):
            if index % 32 == 0:
                # Let input and shutdown run while constructing large histories,
                # without paying for an intermediate conversation layout.
                await asyncio.sleep(0)
                if self._shutting_down:
                    return
            content = message.content
            text = (
                content
                if isinstance(content, str)
                else "\n".join(part.text for part in content or [] if isinstance(part, TextContent))
            )
            if message.role in ("user", "assistant"):
                if message.role == "user" and message.name == TOOL_ADDED_MESSAGE_NAME:
                    widgets.append(ToolNotice(text))
                    continue
                context_title = saved_context_title(text) if message.role == "user" else ""
                if text:
                    widgets.append(SavedContext(context_title, text) if context_title else Turn(message.role, text))
                if message.role == "user" and not context_title:
                    composer.remember(text)
                for position, call in enumerate(message.tool_calls):
                    self._tools.pop(call.id, None)
                    card = tool(call.id, call.name, call.arguments)
                    key = (row_id, position)
                    self._delivery_anchors[key] = card
                    for receipt in receipts.get(key, ()):
                        if receipt.delivery_id not in self._delivery_ids:
                            widget = DeliveryWidget(receipt)
                            widgets.append(widget)
                            self._delivery_anchors[key] = widget
                            self._delivery_ids.add(receipt.delivery_id)
            elif message.role == "tool":
                card = tool(message.tool_call_id or "", message.name or "tool")
                # Core history persists tool failures with this literal prefix.
                if text.startswith("Error:"):
                    card.finish(None, text, 0)
                else:
                    card.finish(text, None, 0)
            elif message.role == "compaction_summary":
                widgets.append(Static("Earlier context was compacted.", markup=False, classes="notice"))

        if widgets:
            conversation = self.query_one("#conversation", VerticalScroll)
            with self.batch_update():
                conversation.anchor()
                welcome = conversation.query("#welcome")
                if welcome:
                    welcome.first().display = False
                await conversation.mount(*widgets)

    async def _delivery_notice(self, notice: dict[str, ChannelValue]) -> None:
        # Observer tasks never mutate widgets or route sends through selection.
        if notice.get("session_id") == self._delivery_session:
            self._delivery_dirty = True

    async def _delivery(self, receipt: DeliveryReceipt) -> None:
        if receipt.delivery_id in self._delivery_ids:
            return
        await self._flush_stream()
        self._answer = None
        self._answer_text = ""
        widget = DeliveryWidget(receipt)
        key = (receipt.origin.anchor_message_id, receipt.origin.call_position)
        anchor = self._delivery_anchors.get(key)
        if anchor is not None and anchor.is_attached:
            await self.query_one("#conversation", VerticalScroll).mount(widget, after=anchor)
            self._delivery_anchors[key] = widget
        else:
            await self._add(widget)
        self._delivery_ids.add(receipt.delivery_id)

    async def _reconcile_deliveries(self, *, force: bool = False) -> None:
        host = self._channel_host
        if self._shutting_down or host is None or not host.supports_history or not self._delivery_session:
            return
        if not force and not self._delivery_dirty:
            return
        self._delivery_dirty = False
        history = await host.journal.history(self._delivery_session)
        if history.earlier:
            await self._earlier_deliveries(history.earlier)
        for receipt in history.current:
            key = (receipt.origin.anchor_message_id, receipt.origin.call_position)
            if key in self._delivery_anchors:
                await self._delivery(receipt)
            elif receipt.delivery_id not in self._delivery_ids:
                # The producer may be ahead of the UI queue. Wait for its exact
                # committed anchor event rather than guessing from call IDs.
                self._delivery_dirty = True

    async def _earlier_deliveries(self, receipts: tuple[DeliveryReceipt, ...]) -> None:
        conversation = self.query_one("#conversation", VerticalScroll)
        headers = conversation.query(".earlier-deliveries")
        if headers:
            previous: Widget = headers.first()
        else:
            previous = Static("Deliveries from earlier context", markup=False, classes="notice earlier-deliveries")
            await conversation.mount(previous, before=0)
        widgets = {widget.receipt.delivery_id: widget for widget in conversation.query(DeliveryWidget)}
        for receipt in receipts:
            widget = widgets.get(receipt.delivery_id)
            if widget is None:
                await self._delivery(receipt)
                widget = next(
                    w for w in conversation.query(DeliveryWidget) if w.receipt.delivery_id == receipt.delivery_id
                )
            # Move existing widgets, never rebuild tool disclosures or focus.
            conversation.move_child(widget, after=previous)
            previous = widget

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
        if self._shutting_down or self._closing_requested:
            return
        if not command:
            if self._queued_prompts and not self.busy:
                self._queue_paused = False
                self._drain_queue()
            return
        is_command = command.startswith("/")
        name = command.split(maxsplit=1)[0]
        if is_command and name not in {"/voice", "/perf"}:
            name = "/" + self.harness.commands.canonical_name(name[1:])
        custom_command = (
            is_command
            and name not in {"/", "/perf", "/voice"}
            and all(builtin.name != name[1:] for builtin in BUILTIN_COMMANDS)
        )
        if (
            self.busy
            and is_command
            and not custom_command
            and name not in {"/", "/help", "/context", "/plugins", "/tasks", "/queue", "/quit", "/perf", "/voice"}
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
            self._queue_send_armed = False
            self._queue_paused = False
            self._update_queue()
            if self.busy:
                if self.harness.config.submit_mode == "interrupt" and self._backend_ready:
                    self._status("Stopping current work before running the new prompt...")
                    if self._active is not None and not self._active.cancelling():
                        self._wakeups.cancel_turn()
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
            self.command(event.text)
        else:
            self._launch(lambda: self._send(event.text), "Working  /  Esc to cancel")

    def action_send_queue(self) -> None:
        # Never act through a modal (especially an approval dialog), or submit
        # the composer draft: this shortcut operates only on accepted prompts.
        if self.screen is not self.default_screen or self._shutting_down or self._closing_requested:
            return
        if not self._queued_prompts:
            self._queue_send_armed = False
            return
        interrupt = self._queue_send_armed and not self._queue_paused
        self._queue_paused = False
        self._queue_send_armed = True
        if self.busy:
            if interrupt and self._active is not None and not self._active.cancelling():
                self._status("Stopping current work before sending queued messages...")
                self._wakeups.cancel_turn()
                self._active.cancel()
            else:
                self._status("Queued messages will run next  /  Ctrl+S again to interrupt and send")
            self._update_queue()
        else:
            self._drain_queue()

    def _update_queue(self) -> None:
        if not self._queued_prompts or self._queue_paused:
            self._queue_send_armed = False
        label = self.query_one("#queue-status", Static)
        label.display = bool(self._queued_prompts)
        if self._queued_prompts:
            state = "paused; empty Enter resumes" if self._queue_paused else "waiting"
            hint = "Ctrl+S again: interrupt & send" if self._queue_send_armed else "Ctrl+S: send next"
            lines = []
            for index, prompt in enumerate(self._queued_prompts, start=1):
                preview = " ".join((prompt if isinstance(prompt, str) else "Voice: " + prompt.text).split())
                prefix = f"{len(self._queued_prompts)} queued ({state})  /  {hint}  /  " if index == 1 else ""
                lines.append(f"{prefix}{index}. {preview}")
            label.update(Text("\n".join(lines), no_wrap=True, overflow="ellipsis"))

    def _drain_queue(self) -> None:
        if self._shutting_down or self.busy or self._queue_paused or not self._queued_prompts:
            return
        prompt = self._queued_prompts.popleft()
        self._queue_send_armed = False
        self._update_queue()
        if isinstance(prompt, VoicePrompt):
            if prompt.session_id != self.harness.session_id:
                prompt.bridge.discard(prompt)
                self._status("A queued voice request belongs to another session and was not run.", error=True)
                self.call_later(self._drain_queue)
                return
            self._launch(
                lambda: self._send(prompt.text, voice_prompt=prompt), "Working on voice request  /  Esc to stop"
            )
        elif prompt.lstrip().startswith("/"):
            self.command(prompt)
            if not self.busy:
                self._queue_paused = True
                self._update_queue()
        else:
            self._launch(lambda: self._send(prompt), "Working on queued prompt  /  Esc to stop")

    async def _send(
        self,
        text: str,
        *,
        display_text: str = "",
        session_id: str = "",
        wakeup: bool = False,
        voice_prompt: VoicePrompt | None = None,
    ) -> None:
        if not self._backend_ready:
            await self._initialize()
        if not wakeup:
            self._wakeups.human_turn()
        run_id = uuid4().hex
        self._task_note_run = run_id
        if self._voice is not None:
            for bridge in self._voice.bridges:
                bridge.begin(run_id, voice_prompt)
        if voice_prompt is not None and (self._voice is None or voice_prompt.bridge not in self._voice.bridges):
            voice_prompt.bridge.begin(run_id, voice_prompt)
        self._cancel_tool_previews()
        self._answer = None
        self._answer_text = ""
        self._run_text = ""
        self._pending.clear()
        self._tools.clear()
        self._unanchored_tools.clear()
        self._called_tools.clear()
        self._delivery_session = self.harness.session_id
        self._usage = ""
        if not session_id and not wakeup:
            # A new human submission resumes live following even if inspecting
            # the previous tool card released the conversation's bottom anchor.
            self.query_one("#conversation", VerticalScroll).anchor()
            await self._add(Turn("user", display_text or text))
        # Close the producer even if cancellation happens while rendering an event.
        stream = (
            self.harness.wake(text, session_id=session_id)
            if wakeup
            else self.harness.continue_task(session_id, text, message=self._task_input(session_id, text))
            if session_id
            else self.harness.run(text)
        )
        pending_events: deque[HarnessEvent] = deque()

        async def present(event: HarnessEvent) -> None:
            try:
                await self._event(event)
            finally:
                # Identity, never equality: repeated tool calls and identical
                # text events remain distinct. Diagnostics need not be observed.
                for index, pending in enumerate(pending_events):
                    if pending is event:
                        del pending_events[index]
                        break

        cancelled = failed = False
        try:
            with (
                host_run(self.harness, run_id, observe=pending_events.append),
                self.harness.followups.observe(self._voice_turn)
                if isinstance(self.harness, Harness)
                else nullcontext(),
            ):
                async with aclosing(stream) as events:
                    async for event in events:
                        await join_owned(asyncio.create_task(present(event)))
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            failed = True
            raise
        finally:
            # Closing the stream joins the producer. Recover its bounded tail,
            # including an anchor already dequeued when cancellation arrived.
            # Finish only incremental presentation; existing cards retain state.
            try:
                while pending_events and not self._shutting_down:
                    await present(pending_events[0])
                await self._reconcile_deliveries(force=True)
            finally:
                # This root producer has joined all of its children. Any note
                # without pickup was not consumed; keep its draft for retry.
                self._pending_task_notes = {
                    key: note for key, note in self._pending_task_notes.items() if note[0] != run_id
                }
                if self._task_note_run == run_id:
                    self._task_note_run = ""
                if self._voice is not None:
                    for bridge in self._voice.bridges:
                        bridge.finish(run_id, cancelled=cancelled, failed=failed)
                for prompt in self._voice_inputs.values():
                    prompt.bridge.discard(prompt)
                self._voice_inputs.clear()

    def _task_input(self, session_id: str, prompt: str) -> RunInput:
        identifier = uuid4().hex
        self._pending_task_notes[session_id, identifier] = (self._task_note_run, prompt)
        return RunInput(prompt, identifier=identifier, message_id=identifier)

    def _picked_up_task_note(self, event: TaskMessage) -> str:
        _, prompt = self._pending_task_notes.pop((event.session_id, event.message_id), ("", ""))
        if not prompt:
            return event.prompt
        # An inspector may have reopened while pickup waited on the parent
        # stream. Its editor is newer than the draft saved on its last close.
        if isinstance(self.screen, TaskScreen) and self.screen.selected_session_id == event.session_id:
            composer = self.screen.query_one("#task-followup", TextArea)
            if composer.text == prompt:
                composer.clear()
                self._task_drafts.pop(event.session_id, None)
            else:
                self._task_drafts[event.session_id] = composer.text
        elif self._task_drafts.get(event.session_id) == prompt:
            self._task_drafts.pop(event.session_id, None)
        return prompt

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

    def _cancel_tool_previews(self) -> None:
        for card in self._tool_previews.values():
            card.cancel()
        self._tool_previews.clear()

    async def _tool_progress(self, event: ToolCallProgressEvent) -> None:
        key = (event.generation_id, event.index)
        if event.status == "abandoned":
            card = self._tool_previews.pop(key, None)
            if card is not None:
                card.cancel()
            return
        card = self._tool_previews.get(key)
        if card is None:
            await self._flush_stream()
            self._answer = None
            self._answer_text = ""
            card = ToolCard("", event.name or "...", preview=True, animations=self.harness.config.animations)
            self._tool_previews[key] = card
            await self._add(card)
        card.set_preview(
            event.name or "...",
            event.arguments_text,
            ready=event.status == "ready",
            truncated=event.arguments_truncated,
        )
        self._motion_state("working")
        self._status(f"Preparing {event.name or 'tool call'}  /  Esc to cancel")

    async def _tool(self, call_id: str, tool: str, arguments: object = None) -> ToolCard:
        if call_id not in self._tools:
            await self._flush_stream()
            self._answer = None
            self._answer_text = ""
            card = ToolCard(
                call_id,
                tool,
                arguments,
                default_timeout=self.harness.config.shell_timeout if tool == "shell" else 0,
                animations=self.harness.config.animations,
            )
            self._tools[call_id] = card
            await self._add(card)
        elif arguments is not None:
            self._tools[call_id].set_arguments(arguments)
        return self._tools[call_id]

    async def _event(self, event: HarnessEvent) -> None:
        while self._voice_turns:
            voice_turn = self._voice_turns.popleft()
            await self._flush_stream()
            self._answer = None
            self._answer_text = self._run_text = ""
            await self._add(Turn("user", voice_turn.text))
        if self._voice is not None:
            for bridge in self._voice.bridges:
                bridge.event(event)
        if isinstance(event, TaskActivity):
            if event.depth == 1:
                kind = event.payload.get("type")
                if not isinstance(kind, str):
                    return
                phase = event.payload.get("phase")
                if kind in {"tool_call", "tool_call_progress", "tool_execution_started", "tool_output"}:
                    self._task_states[event.session_id] = "tool"
                    name = event.payload.get("name")
                    if isinstance(name, str):
                        self._task_tools[event.session_id] = name
                elif kind in {"text_chunk", "text_done"}:
                    self._task_states[event.session_id] = "responding"
                elif kind == "tool_result":
                    self._task_states[event.session_id] = "error" if event.payload.get("error") else "working"
                elif kind == "status" and isinstance(phase, str):
                    phase_states: dict[str, StateName] = {
                        "waiting": "delegated",
                        "failed": "error",
                        "starting": "starting",
                    }
                    self._task_states[event.session_id] = phase_states.get(phase, "working")
            return
        if isinstance(event, (ReasoningChunkEvent, TextChunkEvent, ToolCallEvent, DoneEvent)):
            self._refresh_model()
        if isinstance(event, ToolCallEvent | ToolResultEvent):
            logger.info(
                "Tool call" if isinstance(event, ToolCallEvent) else "Tool result",
                extra={
                    "session_id": self.harness.session_id,
                    "tool": event.name,
                    "duration_ms": event.duration_ms if isinstance(event, ToolResultEvent) else 0,
                    "status": ("failed" if event.error else "completed")
                    if isinstance(event, ToolResultEvent)
                    else "started",
                },
            )
        elif isinstance(event, TaskStarted | TaskCompleted):
            logger.info(
                type(event).__name__,
                extra={"session_id": event.session_id, "provider": event.provider, "model": event.model},
            )
        if isinstance(event, TranscriptAbandoned):
            if event.session_id == self._delivery_session:
                for call in event.calls:
                    pending = self._unanchored_tools.pop(id(call), None)
                    if pending is not None:
                        _, card = pending
                        card.cancel()
                        if self._tools.get(call.id) is card:
                            self._tools.pop(call.id)
                            self._called_tools.discard(call.id)
                            self._running_tools.discard(call.id)
            return
        if isinstance(event, TranscriptAnchor):
            if event.session_id == self._delivery_session:
                cards: list[ToolCard] = []
                for call in event.calls:
                    pending = self._unanchored_tools.get(id(call))
                    if pending is None or pending[0] is not call:
                        raise RuntimeError("Committed transcript anchor has no matching streamed tool card")
                    cards.append(pending[1])
                for position, (call, card) in enumerate(zip(event.calls, cards, strict=True)):
                    self._delivery_anchors[event.message_id, position] = card
                    self._unanchored_tools.pop(id(call))
                await self._reconcile_deliveries(force=True)
            return
        if isinstance(event, (TextChunkEvent, TextDoneEvent, ToolResultEvent, DoneEvent)):
            await self._reconcile_deliveries(force=not isinstance(event, TextChunkEvent))
        if isinstance(event, TaskStarted):
            self._motion_state("waking" if event.trigger == "wakeup" else "delegating")
            key = f"session:{event.session_id}" + (f":{event.followup}" if event.followup else "")
            label = f"{event.title} [{event.session_id}]" if event.title else event.session_id
            await self._tool(
                key,
                f"agent / {label} ({event.profile})",
                {"task": event.prompt, "profile": event.profile, "depth": event.depth},
            )
            self._running_tools.add(key)
            self._refresh_tasks()
            self._status(f"{label} started in the background  /  /tasks for details")
        elif isinstance(event, TaskMessage):
            prompt = self._picked_up_task_note(event)
            await self._flush_stream()
            self._answer = None
            self._answer_text = ""
            label = f"{event.title} [{event.session_id}]" if event.title else event.session_id
            await self._add(Turn("user", f"Follow-up to {label}:\n\n{prompt}"))
            self._refresh_tasks()
        elif isinstance(event, TaskCompleted):
            self._motion_state("returned")
            self._recent_task_id, self._recent_task_until = event.session_id, monotonic() + 2
            key = f"session:{event.session_id}" + (f":{event.followup}" if event.followup else "")
            label = f"{event.title} [{event.session_id}]" if event.title else event.session_id
            card = await self._tool(key, f"agent / {label} ({event.profile})")
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
            self._status(f"{label} responded  /  parent continuing")
        elif isinstance(event, CompactionStartedEvent):
            self._motion_state("working")
            self._compacting = True
            self._status("Compacting context...")
        elif isinstance(event, CompactionDoneEvent):
            self._compacting = False
            await self._reconcile_deliveries(force=True)
            await self._notice(
                f"Context compacted: {event.original_message_count} -> {event.new_message_count} messages."
            )
        elif self._compacting and isinstance(event, (TextChunkEvent, TextDoneEvent, ReasoningChunkEvent, DoneEvent)):
            return
        elif isinstance(event, TextChunkEvent):
            self._response_activity()
            await self._text(event.chunk)
        elif isinstance(event, TextDoneEvent):
            if event.text:
                self._response_activity()
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
            self._motion_state("working")
            self._status("Thinking  /  Esc to cancel")
        elif isinstance(event, ToolCallProgressEvent):
            await self._tool_progress(event)
        elif isinstance(event, ToolCallEvent):
            self._motion_state("delegating" if event.name == "delegate" else "tool")
            if event.id in self._called_tools:
                self._tools.pop(event.id, None)
            self._called_tools.add(event.id)
            generation = event.extra.get("generation_id")
            index = event.extra.get("index")
            preview = (
                self._tool_previews.pop((generation, index), None)
                if isinstance(generation, str) and isinstance(index, int)
                else None
            )
            if preview is not None:
                await self._flush_stream()
                self._answer = None
                self._answer_text = ""
                preview._default_timeout = self.harness.config.shell_timeout if event.name == "shell" else 0
                preview.start_call(event.id, event.name, event.arguments)
                self._tools[event.id] = preview
            card = await self._tool(event.id, event.name, event.arguments)
            self._unanchored_tools[id(event)] = (event, card)
            self._running_tools.add(event.id)
            self._status(f"Running {event.name}  /  Esc to cancel")
        elif isinstance(event, ToolOutput):
            card = await self._tool(event.call_id, event.tool)
            self._running_tools.add(event.call_id)
            card.append_output(event.text)
        elif isinstance(event, ToolResultEvent):
            card = await self._tool(event.id, event.name)
            card.finish(event.result, event.error, event.duration_ms)
            if self._performance_enabled:
                self._performance.record_tool_duration(event.name, max(0, event.duration_ms) / 1000)
            self._running_tools.discard(event.id)
            if not self._running_tools and self._motion.state == "tool":
                self._motion_state("working")
        elif isinstance(event, ToolAddedNotice):
            await self._add(ToolNotice(event.text))
        elif isinstance(event, Notice):
            await self._notice(event.text, error=event.level == "error", warning=event.level == "warning")
        elif isinstance(event, TaskDeliveryWarning):
            label = f"{event.title} [{event.session_id}]" if event.title else event.session_id
            parent = event.parent_session_id or "Main"
            await self._notice(f"{label} could not deliver its result to parent {parent}.", error=True)
        elif isinstance(event, ErrorEvent):
            await self._notice(transport_error_message(event) or event.message, error=True)
        elif isinstance(event, RateLimitEvent):
            self._status(
                event.message or f"Rate limited; retry {event.attempt}/{event.max_retries} in {event.retry_after:g}s",
                state="warning",
            )
        elif isinstance(event, DoneEvent):
            logger.info(
                "Response completed",
                extra={
                    "session_id": self.harness.session_id,
                    "provider": self.harness.config.provider,
                    "model": self.harness.agent.provider.model
                    if isinstance(self.harness, Harness)
                    else self.harness.config.model,
                },
            )
            if not self._status_error and any(task.status == "running" for task in self._activity_tasks):
                self._motion_state("delegated")
                self._status("Waiting for agents  /  Esc to cancel")
            if event.usage.has_usage():
                if event.usage.prompt_tokens or event.usage.completion_tokens:
                    self._usage = f"{event.usage.prompt_tokens:,} in / {event.usage.completion_tokens:,} out tokens"
                else:
                    self._usage = f"{event.usage.total_tokens:,} tokens"
            await self._flush_stream()

    def _response_activity(self) -> None:
        if not self._status_error and self._motion.state != "responding":
            self._motion_state("responding")
            self._status("Responding  /  Esc to cancel")

    async def request_approval(self, request: ApprovalRequest) -> bool:
        if self._shutting_down:
            return False
        # Lightweight/fake harnesses still support one-shot decisions, but cannot
        # establish the resource identity required for a workspace grant.
        approvals = self._tool_approvals
        if approvals is None and isinstance(self.harness, Harness) and not request.mandatory:
            candidate = ToolApprovals(
                self.harness.agent.session.db_path.with_name("tool-approvals.db"), self.harness.workspace
            )
            try:
                candidate.initialize()
            except (OSError, sqlite3.Error):
                logger.exception("Could not initialize workspace tool permissions")
            else:
                approvals = self._tool_approvals = candidate
        binding = None if approvals is None or request.mandatory else approvals.requested_binding(self.harness, request)
        task = asyncio.current_task()

        def live() -> bool:
            return not self._shutting_down and task is not None and not task.done() and not task.cancelling()

        if binding is not None and approvals is not None:
            try:
                allowed = await approvals.check_allowed(binding)
            except sqlite3.Error:
                allowed = False  # An unreadable saved policy requires an explicit decision.
            if not live() or approvals.requested_binding(self.harness, request) != binding:
                return False
            if allowed:
                return True
        previous_screen = self.screen
        if self._voice is not None:
            for bridge in self._voice.bridges:
                bridge.approval(request.tool)
        previous_focus = previous_screen.focused
        self._hide_completions()
        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        modal = ApprovalModal(
            request,
            workspace=approvals.workspace if approvals is not None and binding is not None else "",
            persistent=binding.persistent if binding is not None else False,
        )

        def decide(allowed: bool | str | None) -> None:
            if result.done():
                return
            accepted = allowed is True and live()
            if (
                allowed == "always"
                and live()
                and not request.mandatory
                and approvals is not None
                and binding is not None
            ):
                # No await between live definition validation, commit and answer.
                # A changed definition or cancelled worker must never save a grant.
                if approvals.requested_binding(self.harness, request) == binding:
                    try:
                        approvals.grant(binding)
                    except sqlite3.Error:
                        logger.exception("Could not save workspace tool permission; request denied")
                        self.notify(
                            "Could not save permission. Nothing was approved; retry or allow once.", severity="error"
                        )
                    else:
                        accepted = True
                else:
                    self.notify("Tool definition or permissions changed. Request denied; retry.", severity="warning")
            result.set_result(accepted)

        self._motion_state("approval")
        self._status(f"Approval needed: {request.tool}", state="warning")
        try:
            await self.push_screen(modal, decide)
            return await result
        finally:
            if not self._shutting_down:
                if modal in self.screen_stack:
                    modal.dismiss(False)
                self._motion_state("working")
                self._status("Working  /  Esc to cancel")
                if (
                    previous_focus is not None
                    and previous_focus.is_attached
                    and previous_focus in previous_screen.focus_chain
                ):
                    previous_focus.focus(scroll_visible=False)
                else:
                    self.query_one(Composer).focus(scroll_visible=False)

    def action_cancel(self) -> None:
        if self._startup_pending:
            self._request_quit()
            return
        if self._closing_requested:
            self.exit()
            return
        if isinstance(self.screen, TaskScreen):
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
            self._wakeups.cancel_turn()
            self._active.cancel()

    def action_interrupt(self) -> None:
        if self._startup_pending:
            self._request_quit()
            return
        if self._copy_selection():
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
                self._wakeups.cancel_turn()
                self._active.cancel()
            return
        now = monotonic()
        if now - self._interrupt_at < 2:
            self._request_quit()
        else:
            self._interrupt_at = now
            self._status("Press Ctrl+C again to exit, or Ctrl+Q to quit.")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "startup_continue":
            return self._startup_pending
        return not (self._startup_pending and action not in {"cancel", "interrupt", "quit"})

    async def action_quit(self) -> None:
        self._request_quit()

    def _request_quit(self) -> None:
        if (
            self._startup_pending
            or self._closing_requested
            or not self.harness.config.animations
            or self.animation_level == "none"
        ):
            self._closing_requested = True
            if self._active is not None and not self._active.done():
                self._active.cancel()
            self.exit()
            return
        self._closing_requested = True
        self.query_one(Composer).disabled = True
        self._queued_prompts.clear()
        self._queue_paused = True
        if self._active is not None and not self._active.done():
            self._active.cancel()
        self._motion_lifecycle.begin("closing", monotonic())
        self._animate_activity()

    async def _shutdown(self) -> None:
        # Callbacks use mounted widgets: stop the backend before Textual tears them down.
        self._shutting_down = True
        self.exit_session_id = self.harness.session_id
        self.exit_session_saved = isinstance(self.harness, Harness) and self.harness._session_created
        self._wakeups.close()
        if self._owns_wakeups and self.harness.wakeup_handler == self._schedule_wakeup:
            self.harness.wakeup_handler = None
        self._queued_prompts.clear()
        try:
            if self._voice is not None:
                try:
                    await self._voice.shutdown()
                except Exception as error:
                    logger.warning("Voice cleanup could not be confirmed", extra={"error_type": type(error).__name__})
            for job in self._voice_jobs:
                job.cancel()
            await asyncio.gather(*self._voice_jobs, return_exceptions=True)
            if self.screen_stack and isinstance(self.screen, DeviceLoginModal):
                self.screen.clear_code()
            if self._active is not None:
                if not self._active.cancelling():
                    self._active.cancel()
                await asyncio.gather(self._active, return_exceptions=True)
            if self._preinitialized is not None:
                if not self._preinitialized.done():
                    self._preinitialized.cancel()
                await asyncio.gather(self._preinitialized, return_exceptions=True)
            if self._channel_host is not None:
                self._channel_host.close()
            await self.harness.close()
            logger.info(
                "TUI stopped",
                extra={
                    "session_id": self.exit_session_id,
                    "duration_ms": round((monotonic() - self.started_at) * 1000, 3),
                },
            )
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
                for command in self._client_commands()
            ]
            self.push_screen(ChoiceModal("COMMANDS", commands), self._chosen_command)

    def _chosen_command(self, command: str | None) -> None:
        if command:
            selected = next((item for item in TUI_COMMANDS if item.name == command), None) or self.harness.commands.get(
                command
            )
            if selected is not None:
                self._use_command(selected)

    def action_new_session(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.command("/new")

    def action_sessions(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.command("/sessions")

    def action_tasks(self) -> None:
        if isinstance(self.screen, TaskScreen):
            self.screen.action_focus_pane()
            return
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
        self._show_tasks(event.session_id)

    def _show_tasks(self, selected_session_id: str = "") -> None:
        if isinstance(self.screen, TaskScreen):
            if selected_session_id in self.screen.infos:
                self.screen.show_task(selected_session_id)
            return
        if isinstance(self.screen, ModalScreen):
            return
        self._hide_completions()
        self.push_screen(
            TaskScreen(
                self.harness.tasks.list,
                self.harness.task_history,
                parents=lambda: {task.session_id: task.parent_session_id for task in self.harness.tasks.list()},
                selected_session_id=selected_session_id,
                drafts=self._task_drafts,
            ),
            self._chosen_task_followup,
        )

    def _chosen_task_followup(self, result: tuple[str, str] | None) -> None:
        self.query_one(Composer).focus()
        if not result or self._shutting_down:
            return
        session_id, prompt = result
        if self.busy:
            self.run_worker(self._busy_task_followup(session_id, prompt), group="task-followups", exit_on_error=False)
        else:
            self._launch(lambda: self._send(prompt, session_id=session_id), "Continuing agent conversation...")

    async def _busy_task_followup(self, session_id: str, prompt: str) -> None:
        # A model run may settle between dismissing the inspector and scheduling
        # this worker. Keep any new idle run owned by the normal cancel/queue path.
        if not self.busy:
            self._launch(lambda: self._send(prompt, session_id=session_id), "Continuing agent conversation...")
            return
        if self.harness._busy != "run":
            self._status("Wait for the current operation to finish before continuing an agent. Your draft is kept.")
            return
        message = self._task_input(session_id, prompt)
        accepted = False
        try:
            async with aclosing(self.harness.continue_task(session_id, prompt, message=message)) as events:
                async for event in events:
                    # The busy continuation yields only its acceptance notice;
                    # its pickup belongs to the already-active root stream.
                    accepted = True
                    await self._event(event)
        except (ValueError, RuntimeError, PermissionError) as exc:
            self._status(f"Follow-up not sent; draft kept. {exc}", error=True)
        finally:
            if not accepted:
                self._pending_task_notes.pop((session_id, message.message_id), None)

    def command(self, text: str) -> None:
        if self._closing_requested:
            return
        self._hide_completions()
        if text.strip() in {"", "/"}:
            name, command_name, raw_argument = "/", "", ""
        else:
            try:
                command_name, raw_argument = split_command(text.lstrip())
            except ValueError as exc:
                self._status(str(exc), error=True)
                return
            name = "/" + (
                command_name
                if command_name in {"voice", "perf"}
                else self.harness.commands.canonical_name(command_name)
            )
        argument = raw_argument.strip()
        if name == "/":
            self.action_commands()
        elif name == "/quit":
            self._request_quit()
        elif name == "/help":
            commands = "\n".join(
                f"/{command.name} {command.argument_hint}\n  {command.description} [{command.source}]"
                for command in self._client_commands()
            )
            self.push_screen(DetailModal("NGN / HELP", f"COMMANDS\n{commands}\n\n{HELP}"))
        elif name in ("/context", "/plugins"):
            try:
                self.push_screen(DetailModal(name[1:].upper(), self.harness.describe()))
            except Exception as exc:
                self._status(str(exc), error=True)
        elif name == "/perf":
            self._performance_command(argument)
        elif name == "/voice":
            self._voice_command(argument)
        elif name == "/tasks":
            self._show_tasks()
        elif name == "/queue":
            if argument == "clear":
                for prompt in self._queued_prompts:
                    if isinstance(prompt, VoicePrompt):
                        prompt.bridge.discard(prompt)
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
                    "\n\n".join(
                        f"{index}. {prompt if isinstance(prompt, str) else 'Voice: ' + prompt.text}"
                        for index, prompt in enumerate(self._queued_prompts, 1)
                    )
                    or "No queued prompts."
                )
                self.push_screen(DetailModal("PROMPT QUEUE", text))
        elif self._voice is not None and self._voice.active:
            self._status(
                "End Voice before changing the session, agent, model or provider. Use /voice stop.", error=True
            )
        elif self.busy:
            self._status("Already working. Esc cancels before changing the session or configuration.")
        elif name == "/sessions":
            self._launch(self._sessions, "Loading sessions...")
        elif name == "/skills":
            self._launch(lambda: self._skills(argument), "Discovering skills...")
        elif name == "/login":
            self.push_screen(LoginMethodModal(), self._chosen_login)
        elif name == "/provider":
            self._providers(argument)
        elif name == "/logout":
            self._launch(self._logout, "Signing out...")
        elif name == "/agent" and not argument:
            profiles = [
                (
                    name,
                    name
                    + ("  /  read-only" if self.harness.config.profile(name).mode == "reviewer" else "")
                    + (
                        f"  /  {self.harness.config.profile(name).provider}"
                        if self.harness.config.profile(name).provider
                        else ""
                    )
                    + (
                        f"  /  {self.harness.config.profile(name).description}"
                        if self.harness.config.profile(name).description
                        else ""
                    )
                    + (
                        f"  /  {self.harness.config.profile(name).source}"
                        if self.harness.config.profile(name).source
                        else ""
                    ),
                )
                for name in self.harness.config.selectable_profile_names
            ]
            self.push_screen(
                ChoiceModal("AGENT PROFILE", profiles),
                lambda value: self.command(f"/agent {value}") if value else None,
            )
        elif name == "/model" and not argument:
            self.push_screen(
                ModelModal(self.harness.config.model), lambda value: self.command(f"/model {value}") if value else None
            )
        elif name in ("/new", "/fork", "/rename", "/compact", "/agent", "/model"):
            self._launch(lambda: self._change(name, argument), f"{name[1:].capitalize()}...")
        else:
            # Freshly installed skills may not be in the last completion snapshot.
            # The shared registry refreshes discovery before resolving or rejecting.
            self._launch(lambda: self._custom_command(command_name, raw_argument), f"Running /{command_name}...")

    def action_voice(self) -> None:
        if self._shutting_down or self._closing_requested:
            return
        if self._voice is not None and self._voice.active:
            self._voice_job(self._voice.stop)
        elif self.screen is self.default_screen:
            self._voice_command("start")

    def _ensure_voice(self) -> TuiVoice:
        if self.harness.config.demo:
            raise ValueError("Live voice is unavailable in offline demo mode. Restart ngn without --demo.")
        if not self._backend_ready:
            raise ValueError("Wait for the workspace to finish opening before using Voice.")
        if self._voice is None:
            try:
                from .voice_runtime import TuiVoice
            except ImportError:
                raise ValueError(
                    "Install nagents[tui,voice] to enable local voice. No web server is required."
                ) from None
            self._voice = TuiVoice(self.harness, self._accept_voice, self._voice_changed, lambda: self._task_note_run)
        return self._voice

    def _voice_changed(self, payload: dict[str, object], muted: bool) -> None:
        if not self._shutting_down:
            self.default_screen.query_one(VoicePanel).snapshot(payload, muted=muted)

    def _voice_job(self, operation: Callable[[], Awaitable[None]]) -> None:
        async def run() -> None:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._shutting_down:
                    return
                from .voice_runtime import error_message

                message = error_message(error)
                await self._notice(message, error=True)
                if self._voice is None or not self._voice.active:
                    self._voice_changed({"status": "error", "message": message}, False)

        task = asyncio.create_task(run(), name="ngn-tui-voice-action")
        self._voice_jobs.add(task)
        task.add_done_callback(self._voice_jobs.discard)

    def _voice_command(self, argument: str) -> None:
        parts = argument.split()
        action = parts[0] if parts else ""
        if not action:
            options = [
                (
                    "stop" if self._voice is not None and self._voice.active else "start",
                    "End Voice" if self._voice is not None and self._voice.active else "Start Voice",
                ),
                ("mute", "Mute / unmute microphone"),
                ("settings", "Voice settings / selected agent"),
                ("settings workspace", "Voice settings / workspace"),
                ("settings global", "Voice settings / global"),
                ("devices", "Choose local microphone and speaker"),
                ("status", "Inspect voice status, captions, delegation and startup context"),
            ]
            self.push_screen(ChoiceModal("VOICE", options), lambda value: self._voice_command(value) if value else None)
            return
        try:
            voice = self._ensure_voice()
            if action == "settings" and len(parts) <= 2:
                scope = parts[1] if len(parts) == 2 else "agent"

                async def settings() -> None:
                    snapshot = await voice.snapshot(scope)
                    if not self._shutting_down:
                        self.push_screen(VoiceSettingsModal(snapshot), self._voice_edits)

                self._voice_job(settings)
            elif len(parts) != 1:
                raise ValueError("Usage: /voice [start|stop|mute|settings [agent|global|workspace]|devices|status]")
            elif action == "start":
                self._voice_job(voice.start)
            elif action in {"stop", "end"}:
                self._voice_job(voice.stop)
            elif action == "mute":
                voice.mute()
            elif action == "devices":
                if voice.active:
                    raise ValueError("End Voice before changing audio devices.")
                self._voice_job(self._voice_devices)
            elif action in {"status", "context"}:

                async def details() -> None:
                    text = await voice.details()
                    if not self._shutting_down:
                        self.push_screen(DetailModal("VOICE / STATUS & CONTEXT", text))

                self._voice_job(details)
            else:
                raise ValueError("Usage: /voice [start|stop|mute|settings [agent|global|workspace]|devices|status]")
        except ValueError as error:
            self._status(str(error), error=True)

    def _voice_edits(self, edits: VoiceEdits | None) -> None:
        if edits is None or self._voice is None:
            return
        voice = self._voice

        async def save() -> None:
            await voice.save(edits.snapshot, edits.changes, reset=edits.reset)
            await self._notice("Voice preferences saved. They apply to the next call.")

        self._voice_job(save)

    async def _voice_devices(self) -> None:
        from .voice_audio import devices

        available = await asyncio.to_thread(devices)
        voice = self._ensure_voice()

        def output(selected: str | None) -> None:
            if selected is not None and not voice.active:
                voice.output_device = int(selected)
                self._status("Local voice devices selected. Use /voice start.")

        def microphone(selected: str | None) -> None:
            if selected is None or voice.active:
                return
            voice.input_device = int(selected)
            choices = [
                ("-1", "System default speaker"),
                *((str(item.id), item.name) for item in available if item.output),
            ]
            self.push_screen(ChoiceModal("VOICE / SPEAKER", choices), output)

        choices = [
            ("-1", "System default microphone"),
            *((str(item.id), item.name) for item in available if item.input),
        ]
        self.push_screen(ChoiceModal("VOICE / MICROPHONE", choices), microphone)

    async def _accept_voice(self, prompt: VoicePrompt) -> None:
        if self._shutting_down or self._closing_requested or prompt.session_id != self.harness.session_id:
            raise RuntimeError("The voice conversation is no longer selected")
        if len(self._queued_prompts) + len(self._voice_inputs) >= 20:
            raise ValueError("The assistant input queue is full")
        self._wakeups.human_turn()
        if self.busy and isinstance(self.harness, Harness) and self.harness._busy == "run":
            message = RunInput(
                prompt.text, identifier=prompt.identifier, voice_session_id=prompt.bridge.voice_session_id
            )
            if self.harness.submit_followup(prompt.session_id, message):
                self._voice_inputs[prompt.bridge.voice_session_id, prompt.identifier] = prompt
                self._status("Voice request accepted for the next response boundary; current work continues.")
                return
        self._queued_prompts.append(prompt)
        self._queue_paused = False
        self._update_queue()
        self._drain_queue()

    def _voice_turn(self, marker: RunTurn) -> None:
        if self._voice is not None:
            for bridge in self._voice.bridges:
                bridge.turn(marker)
        prompt = self._voice_inputs.pop((marker.voice_session_id, marker.identifier), None)
        if prompt is not None:
            self._voice_turns.append(prompt)

    async def _skills(self, query: str) -> None:
        if not self._backend_ready:
            await self._initialize()
        await self.harness.agent.refresh_skills()
        skills = [command for command in self.harness.commands.list() if command.source == "skill"]
        if skills and not self.harness.commands.skills_enabled():
            await self._notice("Skill loading is disabled for this agent. Enable its skill tool in .ngn/tools.yaml.")
            return
        direct = {command.name for command in skills if not command.name.startswith("skill:")}
        choices = [
            (command.name, f"/{command.name}  {command.description}")
            for command in skills
            if not command.name.startswith("skill:") or command.name[6:] not in direct
        ]
        if not choices:
            await self._notice("No skills are available for this agent.")
            return
        self.push_screen(ChoiceModal("SKILLS", choices, initial_filter=query.strip()), self._chosen_command)

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
        elif method == "openrouter":
            if hasattr(self.harness, "provider_store"):
                from nagents.harness.providers.registry import ProviderProfile

                self.push_screen(
                    ProviderEditor(profile=ProviderProfile(kind="openrouter")),
                    self._save_provider,
                )
            else:
                self._launch(self._login_openrouter, "Starting OpenRouter sign-in...")
        elif method == "api-key":
            if hasattr(self.harness, "provider_store"):
                self._providers("")
            else:
                # Older external Harness adapters can still provide the
                # original sign-in implementation without named connections.
                choices = [
                    (entry.token, entry.label)
                    for entry in provider_login.methods()
                    if not entry.device and not entry.pkce
                ]
                self.push_screen(ChoiceModal("SIGN IN / PROVIDER", choices), self._chosen_provider)

    def _providers(self, name: str) -> None:
        registry = self.harness.provider_store.load()
        if name:
            if name not in registry.providers:
                self._status(f"Unknown provider connection {name!r}", error=True)
                return
            self._provider_action(name)
            return
        local = self.harness.provider_store.load_scope("workspace")
        choices = [("__add__", "+ Add workspace connection"), ("__add_global__", "+ Add global connection")]
        if local.active and self.harness.provider_store.load_scope("global").active:
            choices.append(("__inherit__", "Use global default in this workspace"))
        choices.extend(
            (
                key,
                f"{key}  [{'workspace' if key in local.providers else 'global'}]  /  "
                f"{profile.kind} / {profile.credential_source} / {profile.effective_endpoint}"
                + ("  [active]" if key == registry.active else ""),
            )
            for key, profile in registry.providers.items()
        )
        self.push_screen(
            ChoiceModal("PROVIDER CONNECTIONS", choices),
            lambda selected: (
                self._add_provider("workspace")
                if selected == "__add__"
                else self._add_provider("global")
                if selected == "__add_global__"
                else self._launch(self._inherit_provider, "Using global default...")
                if selected == "__inherit__"
                else self._provider_action(selected)
                if selected
                else None
            ),
        )

    def _add_provider(self, scope: str = "workspace") -> None:
        self.push_screen(ProviderEditor(), lambda result: self._save_provider(result, scope))

    def _provider_action(self, name: str) -> None:
        scope = "workspace" if name in self.harness.provider_store.load_scope("workspace").providers else "global"
        self.push_screen(
            ChoiceModal(
                f"PROVIDER / {name}",
                [
                    ("edit", "Edit connection and Live settings"),
                    ("activate", "Use in this workspace"),
                    *([("global", "Set as global default")] if scope == "global" else []),
                    ("models", "Browse model catalog (sets workspace model)"),
                    ("delete", "Delete connection"),
                ],
            ),
            lambda action: self._provider_choice(name, action),
        )

    def _provider_choice(self, name: str, action: str | None) -> None:
        scope = "workspace" if name in self.harness.provider_store.load_scope("workspace").providers else "global"
        if action == "edit":
            registry = self.harness.provider_store.load()
            profile = registry.providers.get(name)
            if profile is not None:
                self.push_screen(ProviderEditor(name, profile), lambda result: self._save_provider(result, scope))
        elif action == "activate":
            self._launch(lambda: self._activate_provider(name, "workspace"), "Selecting provider...")
        elif action == "global":
            self._launch(lambda: self._activate_provider(name, "global"), "Selecting global provider...")
        elif action == "models":
            self._launch(lambda: self._provider_models(name), "Fetching models...")
        elif action == "delete":
            self._launch(lambda: self._delete_provider(name, scope), "Deleting provider...")

    def _save_provider(self, result: tuple[str, object] | None, scope: str = "workspace") -> None:
        from nagents.harness.providers.registry import ProviderProfile

        if not result:
            return
        name, profile = result
        if isinstance(profile, ProviderProfile):
            self._launch(lambda: self._persist_provider(name, profile, scope), "Saving provider...")

    async def _persist_provider(self, name: str, profile: ProviderProfile, scope: str = "workspace") -> None:
        registry = self.harness.provider_store.load_scope(scope)
        await self.harness.save_provider(name, profile, registry.revision, scope)
        await self._notice(
            f"Saved {name} in {self.harness.provider_store.store(scope).path}. "
            f"Credential: {profile.credential_source}. Endpoint: {profile.effective_endpoint}."
        )

    async def _activate_provider(self, name: str, scope: str = "workspace") -> None:
        registry = self.harness.provider_store.load_scope(scope)
        await self.harness.activate_provider(name, registry.revision, scope)
        await self._notice(f"Using provider connection {name} in {scope} settings.")

    async def _inherit_provider(self) -> None:
        registry = self.harness.provider_store.load_scope("workspace")
        await self.harness.inherit_provider(registry.revision)
        await self._notice("Following the global default in this workspace.")

    async def _delete_provider(self, name: str, scope: str = "workspace") -> None:
        registry = self.harness.provider_store.load_scope(scope)
        await self.harness.delete_provider(name, registry.revision, scope)
        await self._notice(f"Deleted provider connection {name}.")

    async def _provider_models(self, name: str) -> None:
        models = await self.harness.provider_models(name)
        if not models:
            await self._notice("The provider returned no models; use /model to enter an ID manually.")
            return
        self.push_screen(
            ChoiceModal(f"MODELS / {name}", [(model, model) for model in models]),
            lambda selected: self._choose_provider_model(name, selected),
        )

    def _choose_provider_model(self, name: str, model: str | None) -> None:
        if not model:
            return
        self.command(f"/model {model}")

    def _chosen_provider(self, token: str | None) -> None:
        if not token:
            return
        chosen = provider_login.method(token)

        def collected(result: tuple[str, str, str] | None) -> None:
            if result is None:
                return
            key, model, base_url = result
            self._launch(
                lambda: self._login_provider_api(chosen, key, model, base_url),
                f"Signing in to {chosen.provider}...",
            )

        self.push_screen(ProviderKeyModal(chosen), collected)

    async def _login_provider_api(self, chosen: LoginMethod, key: str, model: str, base_url: str) -> None:
        entry = chosen
        if self.harness.config.demo:
            await self._notice(
                "Restart without --demo to sign in. Offline demo never starts network login.", error=True
            )
            return
        login = ProviderLogin(
            provider=entry.provider,
            model=model or entry.default_model,
            base_url=base_url,
            api="auto",
            auth="api-key",
            api_key_env=entry.env,
            api_key=key,
        )
        try:
            await self.harness.login_api(login)
            note = await provider_login.verify_credentials(
                provider=entry.provider,
                model=self.harness.config.model,
                base_url=base_url,
                api="auto",
                api_key=key,
            )
        except Exception:
            # Provider and network errors may embed response bodies. Never render them.
            await self._notice(
                f"Signing in to {entry.provider} did not complete; check the details and try /login again.",
                error=True,
            )
            return
        self._usage = ""
        await self._notice(f"Signed in to {entry.provider}. Model: {self.harness.config.model}. {note}")

    async def _login_openrouter(self) -> None:
        if self.harness.config.demo:
            await self._notice(
                "Restart without --demo to sign in. Offline demo never starts network login.", error=True
            )
            return
        verifier = provider_login.code_verifier()
        modal = OpenRouterLoginModal(provider_login.authorization_url(verifier))
        pending: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

        def closed(result: str | None) -> None:
            if not pending.done():
                pending.set_result(result)

        try:
            await self.push_screen(modal, closed)
            async with asyncio.timeout(900):
                code = await pending
            if not code or self._shutting_down:
                return
            key = await provider_login.exchange_code(code, verifier)
            login = ProviderLogin(
                provider="openrouter",
                model=provider_login.OPENROUTER_DEFAULT_MODEL,
                auth="api-key",
                api_key_env="OPENROUTER_API_KEY",
                api_key=key,
            )
            await self.harness.login_api(login)
            note = await provider_login.verify_credentials(
                provider="openrouter",
                model=self.harness.config.model,
                base_url="",
                api="auto",
                api_key=key,
            )
        except TimeoutError:
            if not self._shutting_down and modal in self.screen_stack:
                await modal.dismiss()
            await self._notice("OpenRouter sign-in expired. Run /login for a new code.", error=True)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._notice(
                "OpenRouter sign-in did not complete; check the connection and try /login again.", error=True
            )
            return
        finally:
            if not self._shutting_down and modal in self.screen_stack:
                await modal.dismiss()
        self._usage = ""
        await self._notice(f"Signed in to OpenRouter. Model: {self.harness.config.model}. {note}")

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
        self._cancel_tool_previews()
        await self._flush_stream()
        self._answer = None
        self._answer_text = ""
        self._tools.clear()
        self._usage = ""
        conversation = self.query_one("#conversation", VerticalScroll)
        await conversation.remove_children(".turn, .tool-card, .notice, .channel-delivery")
        self._delivery_ids.clear()
        self._delivery_anchors.clear()
        self._unanchored_tools.clear()
        self._called_tools.clear()
        self._delivery_session = ""
        self.query_one("#welcome").display = True

    async def _change(self, name: str, argument: str) -> None:
        if not self._backend_ready:
            await self._initialize()
        if name == "/new":
            await self.harness.new_session()
            await self._clear_conversation()
        elif name == "/fork":
            if self._queued_prompts:
                raise ValueError("Finish or clear queued prompts before forking this conversation.")
            await self._fork_session(argument)
            await self._clear_conversation()
            await self._load_history()
            await self._notice("Forked conversation. New messages are independent of the original chat.")
        elif name == "/rename":
            title = await self.harness.rename_session(argument)
            await self._notice(f"Conversation renamed to {title}.")
        elif name == "/compact":
            await self._event(await self.harness.compact())
        elif name == "/agent":
            await self.harness.set_agent(argument)
            await self._notice(f"Profile changed to {self.harness.config.agent}.")
        elif name == "/model":
            await self.harness.set_model(argument)
            if hasattr(self.harness, "provider_store"):
                self.harness.provider_store.model_store("workspace").save(argument)
            await self._notice(f"Model changed to {self.harness.config.model}.")

    async def _fork_session(self, title: str) -> str:
        if not self._owns_wakeups:
            return await self.harness.fork_session(title)
        with self.harness.operation("fork session"):
            await self.harness.initialize()
            if any(not task.done() for task in self.harness.tasks.root._workers.values()):
                raise forks.SessionForkError("Finish descendant work before forking this conversation.")
            source = self.harness.session_id

            async def change() -> str:
                target = await forks.transaction(
                    self.harness.agent.session.db_path, lambda db: forks.fork_in(db, source, title)
                )
                self.harness.session_id = target
                self.harness._session_created = True
                self.harness.tools.read_hashes.clear()
                # Timers retain their originating root; the copy acquires none.
                return target

            return await join_owned(asyncio.create_task(change(), name="ngn-tui-fork"))

    async def _sessions(self) -> None:
        sessions = await self.harness.list_sessions()
        choices = [("__new__", "+ New session")]
        choices.extend(
            (session.id, f"{session.title or 'Untitled'}  /  {session.updated_at}  /  {session.id[:8]}")
            for session in sessions
        )
        # Fetch while the entrance runs, but never put a modal over its shell.
        await self._startup_complete.wait()
        if not self._shutting_down and not self._closing_requested:
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
