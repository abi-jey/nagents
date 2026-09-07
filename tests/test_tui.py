"""Headless terminal integration tests, with no provider calls or async plugin."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import pytest
from textual.color import Color
from textual.containers import VerticalScroll
from textual.events import Paste
from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import Markdown
from textual.widgets import Static

from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.events import Usage
from nagents.harness import Harness
from nagents.harness.commands import CommandRegistry
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.types import ApprovalRequest
from nagents.harness.types import HarnessEvent
from nagents.harness.types import Notice
from nagents.harness.types import SessionInfo
from nagents.harness.types import ToolOutput
from nagents.tui import NagentsApp
from nagents.tui.screens import ApprovalModal
from nagents.tui.screens import ChoiceModal
from nagents.tui.screens import DetailModal
from nagents.tui.screens import ModelModal
from nagents.tui.widgets import OUTPUT_LIMIT
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import Turn
from nagents.types import Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot

    from nagents.harness.auth import DeviceAuthorization


class FakeHarness:
    """Only a test double. Production UI always calls the supplied harness."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.config = HarnessConfig(workspace=workspace, demo=True)
        self.session_id = "test-session"
        self.approval_handler: Callable[[ApprovalRequest], Awaitable[bool]] = self.deny
        self.initialized = False
        self.closed = False
        self.cancelled = False
        self.running = False
        self.prompts: list[str] = []
        self.messages: list[Message] = []
        self.events: list[HarnessEvent] = [
            TextChunkEvent(chunk="Hello **world**."),
            TextDoneEvent(text="Hello **world**."),
            DoneEvent(),
        ]
        self.decisions: list[bool] = []
        self.resumed: list[str] = []
        self.new_count = 0
        self.describe_count = 0
        self.fail_model = False
        self._busy = ""
        self.tools = SimpleNamespace(skills={})
        self.tasks = SimpleNamespace(list=lambda: [])
        self.commands = CommandRegistry(cast("Harness", self))

    async def deny(self, request: ApprovalRequest) -> bool:
        return False

    async def initialize(self) -> None:
        self.initialized = True

    async def history(self) -> list[Message]:
        return self.messages

    async def task_history(self, task_id: str, limit: int = 100) -> list[Message]:
        return []

    async def run(self, prompt: str) -> AsyncIterator[HarnessEvent]:
        self.prompts.append(prompt)
        self.running = True
        try:
            if prompt == "wait":
                yield TextChunkEvent(chunk="A partial answer.")
                await asyncio.Event().wait()
            if prompt == "approval":
                allowed = await self.approval_handler(
                    ApprovalRequest(
                        "a1",
                        "write_file",
                        "Replace a file [red]literally[/red]",
                        {"path": "hello.py", "content": "print('hello')\n"},
                        "--- a/hello.py\n+++ b/hello.py\n-old\n+new\n",
                    )
                )
                self.decisions.append(allowed)
                yield ToolResultEvent(id="a1", name="write_file", result={"allowed": allowed, "preview_only": True})
            for event in self.events:
                yield event
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.running = False

    async def list_sessions(self) -> list[SessionInfo]:
        return [SessionInfo("saved-1", "A saved conversation", "2026-09-06")]

    async def resume(self, session_id: str) -> None:
        self.resumed.append(session_id)
        self.session_id = session_id
        self.messages = [
            Message(role="user", content="Remember this"),
            Message(role="assistant", content="Resumed answer"),
        ]

    async def new_session(self) -> str:
        self.new_count += 1
        self.session_id = f"new-{self.new_count}"
        self.messages = []
        return self.session_id

    async def compact(self) -> CompactionDoneEvent:
        return CompactionDoneEvent(original_message_count=10, new_message_count=3)

    async def set_agent(self, name: str) -> None:
        self.config.agent = name

    async def set_model(self, model: str) -> None:
        if self.fail_model:
            raise ValueError("Unsupported model [red]literal[/red]")
        self.config.model = model

    def describe(self) -> str:
        self.describe_count += 1
        return "Workspace context\nPlugins: none\n[red]not markup[/red]"

    def auth_status(self) -> str:
        return "OFFLINE DEMO" if self.config.demo else "API key from environment (checked on send)"

    async def login(self, show_code: Callable[[DeviceAuthorization], Awaitable[None]]) -> None:
        raise RuntimeError("Login is not configured in this test")

    async def logout(self) -> None:
        return None

    async def close(self) -> None:
        assert not self.running, "Backend closed before the run was awaited"
        self.closed = True


def make_app(backend: FakeHarness) -> NagentsApp:
    return NagentsApp(cast("Harness", backend))


async def idle(app: NagentsApp, pilot: Pilot[None]) -> None:
    for _ in range(150):
        await pilot.pause(0.01)
        if not app.busy and (not app._queued_prompts or app._queue_paused):
            return
    raise AssertionError("Backend task did not become idle")


async def send(app: NagentsApp, pilot: Pilot[None], text: str) -> None:
    composer = app.query_one(Composer)
    composer.load_text(text)
    composer.focus()
    await pilot.press("enter")


def test_composer_callback_after_shutdown_does_not_query_unmounted_widgets(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            app._shutting_down = True
            await app.query_one(Composer).remove()
            app.composer_changed()

    asyncio.run(scenario())


def test_startup_and_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [
            Message(role="user", content="[red]literal[/red]"),
            Message(role="assistant", content="A **saved** reply."),
        ]
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            assert backend.initialized
            assert app.focused is app.query_one(Composer)
            assert "OFFLINE DEMO" in str(app.query_one("#mode", Static).content)
            assert len(app.query(Turn)) == 2
            assert app.query_one(Composer).prompt_history == ["[red]literal[/red]"]
            assert not app.query_one("#welcome").display
        assert backend.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("size,rail", [((60, 20), False), ((80, 24), False), ((110, 30), False), ((132, 38), True)])
def test_responsive_layout(tmp_path: Path, size: tuple[int, int], rail: bool) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            assert app.query_one("#rail").display is rail
            assert composer.region.y > 3
            assert composer.region.bottom <= size[1] - 2
            assert composer.region.width >= size[0] - (36 if rail else 4)
            assert app.query_one("#welcome-title").region.bottom < composer.region.y
            await pilot.resize_terminal(60, 20)
            # Resize dispatch precedes Textual's deferred layout refresh.
            await pilot.pause()
            assert not app.query_one("#rail").display
            assert composer.region.bottom <= 18

    asyncio.run(scenario())


def test_stream_tools_compaction_and_safe_markup(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.events = [
            CompactionStartedEvent(),
            TextChunkEvent(chunk="PRIVATE COMPACTION"),
            TextDoneEvent(text="PRIVATE COMPACTION"),
            CompactionDoneEvent(original_message_count=8, new_message_count=2),
            TextChunkEvent(chunk="A **streamed** "),
            TextChunkEvent(chunk="answer [red]literal[/red]."),
            TextDoneEvent(text="A **streamed** answer [red]literal[/red]."),
            ToolCallEvent(id="t1", name="read[red]file", arguments={"path": "[blue]file"}),
            ToolOutput("t1", "read[red]file", "x" * (OUTPUT_LIMIT + 200)),
            ToolResultEvent(
                id="t1", name="read[red]file", result={"diff": "--- a/file\n+++ b/file\n-old\n+new"}, duration_ms=125
            ),
            ToolOutput("orphan", "shell", "[red]literal output[/red]"),
            ToolResultEvent(id="orphan", name="shell", result="Finished"),
            TextDoneEvent(text="A **streamed** answer [red]literal[/red]."),
            DoneEvent(usage=Usage(prompt_tokens=12, completion_tokens=7, total_tokens=19)),
        ]
        app = make_app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "[red]user text[/red]")
            await idle(app, pilot)
            markdown = list(app.query(Markdown))
            assert [item.source for item in markdown] == ["A **streamed** answer [red]literal[/red]."]
            cards = list(app.query(ToolCard))
            assert len(cards) == 2
            assert cards[0].diff.display
            assert len(cards[0].output_text) < OUTPUT_LIMIT + 100
            assert "truncated" in cards[0].output_text
            assert cards[1].output_text == "[red]literal output[/red]\nFinished"
            assert "read[red]file" in str(cards[0].query_one("CollapsibleTitle", Static).content)
            assert "12 in / 7 out tokens" in str(app.query_one("#status", Static).content)
            cards[0].collapsed = False
            await pilot.pause()
            assert cards[0].diff.region.height > 0
            user_body = app.query_one(".user", Turn).body
            assert isinstance(user_body, Static)
            assert "[red]user text[/red]" in str(user_body.content)
        assert backend.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["allow", "deny", "escape"])
def test_approval(tmp_path: Path, decision: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "approval")
            await pilot.pause()
            assert isinstance(app.screen, ApprovalModal)
            assert app.screen.focused is app.screen.query_one("#deny", Button)
            assert app.screen.request.arguments["content"] == "print('hello')\n"
            assert "+new" in app.screen.request.preview
            if decision == "escape":
                await pilot.press("escape")
            else:
                await pilot.click(f"#{decision}")
            await idle(app, pilot)
            assert backend.decisions == [decision == "allow"]
            assert not isinstance(app.screen, ApprovalModal)
            assert app.focused is app.query_one(Composer)

    asyncio.run(scenario())


def test_cancel_resend_and_duplicate_guard(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "wait")
            await pilot.pause()
            assert backend.running
            await send(app, pilot, "next draft")
            assert backend.prompts == ["wait"]
            assert app.query_one(Composer).text == ""
            assert list(app._queued_prompts) == ["next draft"]
            await pilot.press("escape")
            await idle(app, pilot)
            assert backend.cancelled
            assert "A partial answer." in [item.source for item in app.query(Markdown)]
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.prompts == ["wait", "next draft"]
            assert app.query_one(Composer).text == ""

    asyncio.run(scenario())


def test_shutdown_awaits_run_and_closes_backend(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "wait")
            assert backend.running
        assert backend.cancelled
        assert backend.closed

    asyncio.run(scenario())


def test_sessions_resume_new_and_escape(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+l")
            await idle(app, pilot)
            assert isinstance(app.screen, ChoiceModal)
            await pilot.press("escape")
            await idle(app, pilot)
            assert backend.new_count == 0
            await pilot.press("ctrl+l")
            await idle(app, pilot)
            app.screen.query_one(Input).value = "saved"
            await pilot.pause()
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.resumed == ["saved-1"]
            assert [item.source for item in app.query(Markdown)] == ["Resumed answer"]
            await pilot.press("ctrl+n")
            await idle(app, pilot)
            assert backend.new_count == 1
            assert not app.query(Turn)
            assert app.query_one("#welcome").display

    asyncio.run(scenario())


def test_commands_models_profiles_and_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "/help")
            assert isinstance(app.screen, DetailModal)
            assert "/compact" in app.screen.text
            await pilot.press("escape", "ctrl+p")
            assert isinstance(app.screen, ChoiceModal)
            app.screen.query_one(Input).value = "profile"
            await pilot.pause()
            await pilot.press("enter")
            assert app.query_one(Composer).text == "/agent "
            await pilot.press("enter")
            assert isinstance(app.screen, ChoiceModal)
            app.screen.query_one(Input).value = "reviewer"
            await pilot.pause()
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.config.agent == "reviewer"
            await send(app, pilot, "/model")
            assert isinstance(app.screen, ModelModal)
            app.screen.query_one(Input).value = "arbitrary/model-id"
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.config.model == "arbitrary/model-id"
            for command in ("/plugins", "/context"):
                await send(app, pilot, command)
                assert isinstance(app.screen, DetailModal)
                assert "[red]not markup[/red]" in app.screen.text
                await pilot.press("escape")
            assert backend.describe_count == 2
            backend.fail_model = True
            await send(app, pilot, "/model invalid")
            await idle(app, pilot)
            assert app.query_one("#status").has_class("error")
            assert "Unsupported model" in str(app.query_one("#status", Static).content)
            assert backend.config.model == "arbitrary/model-id"
            await send(app, pilot, "/compact")
            await idle(app, pilot)
            assert any("10 -> 3" in str(item.content) for item in app.query(".notice").results(Static))

    asyncio.run(scenario())


def test_paste_newlines_and_boundary_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.remember("old prompt")
            composer.post_message(Paste("first\nsecond\n"))
            await pilot.pause()
            assert backend.prompts == []
            assert composer.text == "first\nsecond\n"
            await pilot.press("ctrl+j")
            assert composer.text == "first\nsecond\n\n"
            composer.move_cursor((1, 3))
            await pilot.press("up")
            assert composer.text == "first\nsecond\n\n"
            composer.move_cursor((0, 0))
            await pilot.press("up")
            assert composer.text == "old prompt"
            composer.move_cursor((0, len(composer.text)))
            await pilot.press("down")
            assert composer.text == "first\nsecond\n\n"
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.prompts == ["first\nsecond\n\n"]

    asyncio.run(scenario())


def test_missing_credentials_are_visible_and_recoverable(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.demo = False
        backend.events = [ErrorEvent(message="Missing OPENAI_API_KEY. Set it in your environment."), DoneEvent()]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert "OFFLINE DEMO" not in str(app.query_one("#mode", Static).content)
            await send(app, pilot, "hello")
            await idle(app, pilot)
            assert app.query_one("#status").has_class("error")
            assert "OPENAI_API_KEY" in str(app.query_one("#status", Static).content)
            assert any("OPENAI_API_KEY" in str(item.content) for item in app.query(".notice").results(Static))
            assert "tokens" not in str(app.query_one("#status", Static).content)

    asyncio.run(scenario())


def test_scrollback_stays_put_and_markdown_is_incremental(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [
            Message(role="assistant", content="\n\n".join(f"Earlier paragraph {index}." for index in range(30)))
        ]
        backend.events = [TextChunkEvent(chunk=f"Chunk {index}.\n\n") for index in range(70)]
        backend.events.append(DoneEvent())
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            original = app.query_one(Markdown)
            conversation = app.query_one("#conversation", VerticalScroll)
            conversation.scroll_home(animate=False)
            await pilot.pause()
            assert conversation.max_scroll_y > 20
            await send(app, pilot, "stream a long answer")
            await pilot.press("d", "r", "a", "f", "t")
            assert app.query_one(Composer).text == "draft"
            await idle(app, pilot)
            assert conversation.scroll_y == 0
            assert app.query_one(Markdown) is original
            assert len(app.query(Markdown)) == 2
            assert list(app.query(Markdown))[-1].source.count("Chunk 69.") == 1
            conversation.scroll_end(animate=False)
            await pilot.pause()
            await send(app, pilot, "follow again")
            await idle(app, pilot)
            assert conversation.max_scroll_y - conversation.scroll_y <= 3

    asyncio.run(scenario())


def test_final_only_after_tools_and_error_status(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.events = [
            TextChunkEvent(chunk="Before the tool."),
            ToolOutput("orphan", "shell", "Output before call."),
            ToolCallEvent(id="orphan", name="shell", arguments={"command": "false"}),
            ToolResultEvent(id="orphan", name="shell", error="Command exited 1"),
            TextDoneEvent(text="After the tool."),
            DoneEvent(),
        ]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "hello")
            await idle(app, pilot)
            assert [item.source for item in app.query(Markdown)] == ["Before the tool.", "After the tool."]
            children = list(app.query_one("#conversation").children)
            assert children.index(app.query_one(ToolCard)) < children.index(list(app.query(".assistant"))[-1])
            assert app.query_one("#status").has_class("error")
            assert "exited 1" in str(app.query_one("#status", Static).content)

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["ctrl+c", "ctrl+q"])
def test_interrupt_during_approval(tmp_path: Path, key: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "approval")
            await pilot.pause()
            assert isinstance(app.screen, ApprovalModal)
            assert app.screen.query_one("#deny").region.bottom <= 20
            assert app.screen.query_one("#allow").region.right <= 60
            await pilot.press(key)
            if key == "ctrl+c":
                await idle(app, pilot)
                assert not isinstance(app.screen, ApprovalModal)
                await send(app, pilot, "resend")
                await idle(app, pilot)
                assert backend.prompts == ["approval", "resend"]
        assert backend.cancelled
        assert backend.closed
        assert not backend.decisions

    asyncio.run(scenario())


def test_idle_ctrl_c_requires_confirmation(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("ctrl+c")
            assert not backend.closed
            assert "again to exit" in str(app.query_one("#status", Static).content)
            await pilot.press("ctrl+c")
        assert backend.closed

    asyncio.run(scenario())


def test_palette_arrow_keys_and_new_session_option(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "/")
            assert isinstance(app.screen, ChoiceModal)
            await pilot.press("down", "enter")
            assert app.query_one(Composer).text == "/new "
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.new_count == 1
            await pilot.press("ctrl+l")
            await idle(app, pilot)
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.new_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("allow", [True, False])
@pytest.mark.requires_posix
def test_real_harness_demo_approval_and_resume(tmp_path: Path, allow: bool) -> None:
    async def scenario() -> None:
        backend = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        app = NagentsApp(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "demo approval")
            for _ in range(150):
                await pilot.pause(0.02)
                if isinstance(app.screen, ApprovalModal):
                    break
            assert isinstance(app.screen, ApprovalModal)
            assert app.screen.request.tool == "demo_preview"
            await pilot.click("#allow" if allow else "#deny")
            await idle(app, pilot)
            answers = [item.source for item in app.query(Markdown)]
            assert len(answers) == 1
            assert "approved" in answers[0] if allow else "declined" in answers[0]
            assert answers[0].count("## OFFLINE DEMO") == 1
            assert not (tmp_path / "example.py").exists()
            assert "tokens" not in str(app.query_one("#status", Static).content)
            saved = backend.session_id
            await send(app, pilot, "/new")
            await idle(app, pilot)
            assert backend.session_id != saved
            app._chosen_session(saved)
            await idle(app, pilot)
            assert backend.session_id == saved
            assert [item.source for item in app.query(Markdown)] == answers

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_real_harness_boot_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NGN_TUI_TEST_MISSING_KEY", raising=False)

    async def scenario() -> None:
        backend = Harness(
            HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", api_key_env="NGN_TUI_TEST_MISSING_KEY")
        )
        app = NagentsApp(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert app.query_one("#welcome").display
            await send(app, pilot, "hello")
            await idle(app, pilot)
            assert app.query_one("#status").has_class("error")
            assert "NGN_TUI_TEST_MISSING_KEY" in str(app.query_one("#status", Static).content)

    asyncio.run(scenario())


def test_composer_alt_enter_wrap_and_demo_suggestion(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            await pilot.click("#prompt-review")
            composer = app.query_one(Composer)
            assert composer.text == "demo approval"
            assert not backend.prompts
            composer.load_text("    indented code")
            composer.move_cursor((0, len(composer.text)))
            await pilot.press("alt+enter", "x")
            assert composer.text == "    indented code\nx"
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.prompts == ["    indented code\nx"]
            composer.load_text("A long line that wraps. " * 10)
            await pilot.pause()
            assert composer.region.height == 5
            assert composer.region.bottom <= 18

    asyncio.run(scenario())


def test_long_approval_is_scrollable_without_truncating(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            preview = "\n".join(f"+line {index}" for index in range(100))
            approval = asyncio.create_task(
                app.request_approval(
                    ApprovalRequest("long", "edit", "Exact proposal", {"content": "x" * 20_000}, preview)
                )
            )
            await pilot.pause()
            assert isinstance(app.screen, ApprovalModal)
            scroll = app.screen.query_one(".approval-content", VerticalScroll)
            assert scroll.max_scroll_y > 100
            assert app.screen.request.preview == preview
            assert app.screen.focused is app.screen.query_one("#deny")
            scroll.scroll_end(animate=False)
            await pilot.pause()
            assert scroll.scroll_y > 100
            await pilot.press("escape")
            assert not await approval

    asyncio.run(scenario())


def test_help_and_quit_remain_available_while_busy(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "wait")
            await send(app, pilot, "/help")
            assert isinstance(app.screen, DetailModal)
            assert backend.running
            await pilot.press("escape")
            await send(app, pilot, "/quit")
        assert backend.cancelled
        assert backend.closed

    asyncio.run(scenario())


def test_tool_ids_are_scoped_to_a_run_and_saved_errors_stay_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [Message(role="tool", tool_call_id="saved", name="shell", content="Error: Command failed")]
        backend.events = [
            ToolCallEvent(id="reused", name="read_file"),
            ToolResultEvent(id="reused", name="read_file", result="text"),
        ]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert app.query_one(ToolCard).has_class("failed")
            await send(app, pilot, "first")
            await idle(app, pilot)
            first = list(app.query(ToolCard))[-1]
            await send(app, pilot, "second")
            await idle(app, pilot)
            assert len(app.query(ToolCard)) == 3
            assert list(app.query(ToolCard))[-1] is not first

    asyncio.run(scenario())


@pytest.mark.parametrize("continue_session", [False, True])
def test_initial_resume_precedes_history_once(tmp_path: Path, continue_session: bool) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = NagentsApp(
            cast("Harness", backend),
            resume_session="" if continue_session else "selected-id",
            continue_session=continue_session,
        )
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert backend.resumed == ["saved-1" if continue_session else "selected-id"]
            assert [item.source for item in app.query(Markdown)] == ["Resumed answer"]
            await app._initialize()
            await send(app, pilot, "another prompt")
            await idle(app, pilot)
            assert len(backend.resumed) == 1

    asyncio.run(scenario())


def test_initial_resume_options_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either"):
        NagentsApp(cast("Harness", FakeHarness(tmp_path)), resume_session="session", continue_session=True)


@pytest.mark.parametrize("selected", ["agent", "audit"])
def test_agent_picker_includes_named_profiles(tmp_path: Path, selected: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.profiles["audit"] = AgentProfile(mode="reviewer", instructions="Do not show profile internals")
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "/agent")
            assert isinstance(app.screen, ChoiceModal)
            assert app.screen.choices[:3] == [
                ("build", "build  /  build"),
                ("agent", "agent  /  build"),
                ("reviewer", "reviewer  /  reviewer"),
            ]
            assert ("audit", "audit  /  reviewer") in app.screen.choices
            app.screen.query_one(Input).value = selected
            await pilot.pause()
            await pilot.press("enter")
            await idle(app, pilot)
            assert backend.config.agent == selected

    asyncio.run(scenario())


@pytest.mark.parametrize("background", ["auto", "terminal", "theme"])
def test_background_config_and_visible_tool_states(tmp_path: Path, background: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.theme = "ember"
        backend.config.theme_background = background
        app = make_app(backend)
        async with app.run_test(size=(132, 38)) as pilot:
            await idle(app, pilot)
            assert app.native_ansi_color is (background == "terminal")
            variables = app.get_css_variables()
            for state, token in (
                ("running", "tool"),
                ("complete", "success"),
                ("failed", "error"),
                ("cancelled", "warning"),
            ):
                card = ToolCard(state, "subagent / audit")
                await app._add(card)
                if state == "complete":
                    card.finish("Reviewed", None, 100)
                elif state == "failed":
                    card.finish(None, "Unavailable", 100)
                elif state == "cancelled":
                    card.cancel()
                await pilot.pause()
                assert card.has_class(state)
                assert card.query_one("CollapsibleTitle").styles.color == Color.parse(variables[f"ngn-{token}"])
            await app._event(Notice("Review before applying."))
            await app._event(Notice("Retry limit reached.", level="warning"))
            await pilot.pause()
            warning = app.query_one(".notice.warning", Static)
            assert str(warning.content) == "Warning: Retry limit reached."
            assert warning.styles.color == Color.parse(variables["ngn-warning"])
            app._status("Running", state="working")
            await pilot.pause()
            assert app.query_one("#status").styles.color == Color.parse(variables["ngn-tool"])
            app._status("Failure", error=True)
            await pilot.pause()
            assert app.query_one("#rail-activity").styles.color == Color.parse(variables["ngn-error"])

    asyncio.run(scenario())
