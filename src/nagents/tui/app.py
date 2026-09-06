"""A quiet, streaming terminal client for the production harness."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
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
from textual.widgets import Button
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
from nagents.harness.types import Notice
from nagents.harness.types import ToolOutput
from nagents.types import TextContent

from .screens import ApprovalModal
from .screens import ChoiceModal
from .screens import DetailModal
from .screens import ModelModal
from .widgets import Composer
from .widgets import ToolCard
from .widgets import Turn

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.events import Resize
    from textual.widget import Widget

    from nagents.harness import Harness
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import HarnessEvent

COMMANDS = [
    ("/help", "/help       Keyboard shortcuts and commands"),
    ("/new", "/new        Start a new session"),
    ("/sessions", "/sessions   Resume a saved session"),
    ("/compact", "/compact    Compact the current context"),
    ("/agent", "/agent      Choose agent profile: build or reviewer"),
    ("/model", "/model      Change the model ID"),
    ("/plugins", "/plugins    Inspect configured plugins"),
    ("/context", "/context    Inspect workspace and context"),
    ("/quit", "/quit       Close ngn"),
]
HELP = """COMMANDS
/help                 This reference
/new                  Start a new session
/sessions             Browse and resume sessions
/compact              Compact context through the harness
/agent [name]         Choose build or reviewer
/model [id]           Change model (provider stays configured)
/plugins              Inspect plugins via harness.describe()
/context              Inspect active workspace and context
/quit                 Cancel work, close the backend, exit

KEYBOARD
Enter                 Send (never sends a multiline paste)
Ctrl+J / Alt+Enter     Insert a newline
Up / Down             Prompt history at start / end of text
Esc                   Cancel work; deny an approval
Ctrl+P                Commands
Ctrl+N / Ctrl+L       New session / saved sessions
Ctrl+C                Cancel if busy; press twice to exit if idle
Ctrl+Q                Quit

Tool cards expand with Enter or a click. Scroll up to pause
following the response; return to the bottom to follow again.
"""


class NagentsApp(App[None]):
    """Textual front end; the harness owns tools, credentials and persistence."""

    TITLE = "ngn"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+n", "new_session", "New", priority=True),
        Binding("ctrl+l", "sessions", "Sessions", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "interrupt", "Cancel / exit", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness
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
            with VerticalScroll(id="rail"):
                yield Static("WORKSPACE", classes="section-label", markup=False)
                yield Static(id="rail-workspace", markup=False)
                yield Static("SESSION", classes="section-label", markup=False)
                yield Static(id="rail-session", markup=False)
                yield Static("CONFIGURATION", classes="section-label", markup=False)
                yield Static(id="rail-config", markup=False)
                yield Static("ACTIVITY", classes="section-label", markup=False)
                yield Static(id="rail-activity", markup=False)
                yield Static("Local context.\nExplicit permissions.", id="rail-note", markup=False)
        with Vertical(id="compose-area"):
            yield Static("MESSAGE", id="compose-label", markup=False)
            yield Composer()
            yield Static(id="shortcuts", markup=False)
            yield Static("Starting...", id="status", markup=False)

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.harness.approval_handler = self.request_approval
        self._refresh_context()
        self._resize_layout(self.size.width, self.size.height)
        self.query_one(Composer).focus()
        self.set_interval(0.075, self._flush_stream)
        self._launch(self._initialize, "Opening workspace...")

    def on_resize(self, event: Resize) -> None:
        if self.query("#composer"):
            self._resize_layout(event.size.width, event.size.height)

    def _resize_layout(self, width: int, height: int) -> None:
        self.query_one("#rail").display = width > 110
        self.default_screen.set_class(width < 72 or height < 23, "small")
        hints = (
            "Enter send  ^J newline  ^P commands  Esc cancel"
            if width < 72
            else "Enter send   Ctrl+J newline   Ctrl+P commands   Ctrl+L sessions   Esc cancel"
        )
        self.query_one("#shortcuts", Static).update(hints)
        self._size_composer()

    @on(TextArea.Changed)
    def composer_changed(self) -> None:
        self._interrupt_at = 0
        self._size_composer()

    def _size_composer(self) -> None:
        composer = self.query_one(Composer)
        composer.styles.height = min(5 if self.size.height < 25 else 7, max(3, composer.wrapped_document.height + 2))

    def _status(self, text: str, *, error: bool = False) -> None:
        if self._shutting_down:
            return
        if error:
            self._last_error = text
        status = self.query_one("#status", Static)
        status.update(text)
        status.set_class(error, "error")
        self.query_one("#rail-activity", Static).update(text)

    def _refresh_context(self) -> None:
        config = self.harness.config
        self.query_one("#workspace", Static).update(self.harness.workspace.name or str(self.harness.workspace))
        self.query_one("#model", Static).update(config.model)
        self.query_one("#profile", Static).update(config.agent)
        mode = (
            "OFFLINE DEMO  /  no provider calls; no workspace writes"
            if config.demo
            else f"{config.provider}  /  credentials checked on send  /  /context for configuration"
        )
        self.query_one("#mode", Static).update(mode)
        self.query_one("#mode").set_class(config.demo, "demo")
        self.query_one("#rail-workspace", Static).update(str(self.harness.workspace))
        self.query_one("#rail-session", Static).update(self.harness.session_id or "New session")
        self.query_one("#rail-config", Static).update(
            f"{config.provider}\n{config.model}\nProfile: {config.agent}\n"
            f"Plugins: {', '.join(config.plugins) or 'none'}\n"
            f"Project trust: {'enabled' if config.trust_project else 'not granted'}"
        )

    def _launch(self, operation: Callable[[], Awaitable[None]], status: str) -> None:
        if self._shutting_down:
            return
        if self.busy:
            self._status("Already working. Esc cancels; your draft is kept.")
            return
        self._last_error = ""
        self._status(status)
        self._active = asyncio.create_task(self._work(operation), name="ngn-backend")

    async def _work(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
            await self._flush_stream()
            if self._last_error:
                self._status(self._last_error, error=True)
            else:
                self._status("Ready" + (f"  /  {self._usage}" if self._usage else ""))
        except asyncio.CancelledError:
            if not self._shutting_down:
                await self._flush_stream()
                for call_id in self._running_tools:
                    self._tools[call_id].cancel()
                await self._notice("Cancelled. You can send another message.")
                self._status("Cancelled  /  ready for your next message")
        except Exception as exc:
            if not self._shutting_down:
                await self._flush_stream()
                await self._notice(str(exc) or type(exc).__name__, error=True)
        finally:
            self._compacting = False
            self._running_tools.clear()
            if not self._shutting_down:
                self._refresh_context()
            self._active = None

    async def _initialize(self) -> None:
        await self.harness.initialize()
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

    async def _notice(self, text: str, *, error: bool = False) -> None:
        await self._add(
            Static(("Error: " if error else "") + text, markup=False, classes="notice error" if error else "notice")
        )
        if error:
            self._status(text, error=True)

    @on(Composer.Submitted)
    def submit(self, event: Composer.Submitted) -> None:
        command = event.text.strip()
        if self._shutting_down:
            return
        if self.busy and command not in {"/", "/help", "/context", "/plugins", "/quit"}:
            self._status("Already working. Esc cancels; your draft is kept.")
            return
        composer = self.query_one(Composer)
        composer.remember(event.text)
        composer.clear()
        if command.startswith("/"):
            self.command(command)
        else:
            self._launch(lambda: self._send(event.text), "Working  /  Esc to cancel")

    async def _send(self, text: str) -> None:
        if not self._backend_ready:
            await self._initialize()
        self._answer = None
        self._answer_text = ""
        self._run_text = ""
        self._pending.clear()
        self._tools.clear()
        self._usage = ""
        await self._add(Turn("user", text))
        # Close the producer even if cancellation happens while rendering an event.
        async with aclosing(self.harness.run(text)) as events:
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
        if isinstance(event, CompactionStartedEvent):
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
            await self._notice(event.text, error=event.level == "error")
        elif isinstance(event, ErrorEvent):
            await self._notice(event.message, error=True)
        elif isinstance(event, RateLimitEvent):
            self._status(
                event.message or f"Rate limited; retry {event.attempt}/{event.max_retries} in {event.retry_after:g}s"
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
        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        modal = ApprovalModal(request)

        def decide(allowed: bool | None) -> None:
            if not result.done():
                result.set_result(allowed is True)

        self._status(f"Approval needed: {request.tool}")
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
        if isinstance(self.screen, ApprovalModal):
            self.screen.action_deny()
        elif isinstance(self.screen, ModalScreen):
            self.screen.dismiss()
        elif self.busy and self._active is not None and not self._active.cancelling():
            self._status("Cancelling...")
            self._active.cancel()

    def action_interrupt(self) -> None:
        if self.busy:
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
        try:
            if self._active is not None:
                if not self._active.cancelling():
                    self._active.cancel()
                await asyncio.gather(self._active, return_exceptions=True)
            await self.harness.close()
        finally:
            await super()._shutdown()

    def action_commands(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.push_screen(ChoiceModal("COMMANDS", COMMANDS), self._chosen_command)

    def _chosen_command(self, command: str | None) -> None:
        if command:
            self.command(command)

    def action_new_session(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.command("/new")

    def action_sessions(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.command("/sessions")

    def command(self, text: str) -> None:
        name, _, argument = text.partition(" ")
        argument = argument.strip()
        if name == "/":
            self.action_commands()
        elif name == "/quit":
            self.exit()
        elif name == "/help":
            self.push_screen(DetailModal("NGN / HELP", HELP))
        elif name in ("/context", "/plugins"):
            try:
                self.push_screen(DetailModal(name[1:].upper(), self.harness.describe()))
            except Exception as exc:
                self._status(str(exc), error=True)
        elif self.busy:
            self._status("Already working. Esc cancels before changing the session or configuration.")
        elif name == "/sessions":
            self._launch(self._sessions, "Loading sessions...")
        elif name == "/agent" and not argument:
            self.push_screen(
                ChoiceModal(
                    "AGENT PROFILE",
                    [("build", "build      Implement and iterate"), ("reviewer", "reviewer   Review and explain")],
                ),
                lambda value: self.command(f"/agent {value}") if value else None,
            )
        elif name == "/model" and not argument:
            self.push_screen(
                ModelModal(self.harness.config.model), lambda value: self.command(f"/model {value}") if value else None
            )
        elif name in ("/new", "/compact", "/agent", "/model"):
            self._launch(lambda: self._change(name, argument), f"{name[1:].capitalize()}...")
        else:
            self._status(f"Unknown command: {name}. Use /help.", error=True)

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
