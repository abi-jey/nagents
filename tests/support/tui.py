"""Textual app construction and fake harness helpers shared by TUI suites."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

from nagents.events import CompactionDoneEvent
from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolResultEvent
from nagents.harness.commands import CommandRegistry
from nagents.harness.config import HarnessConfig
from nagents.harness.types import ApprovalRequest
from nagents.harness.types import HarnessEvent
from nagents.harness.types import SessionInfo
from nagents.tui import NagentsApp
from nagents.tui.widgets import Composer
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot

    from nagents.harness import Harness
    from nagents.harness.auth import DeviceAuthorization
    from nagents.skills import Skill


class FakeHarness:
    """Only a test double. Production UI always calls the supplied harness."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.config = HarnessConfig(workspace=workspace, demo=True)
        self.session_id = "test-session"
        self.approval_handler: Callable[[ApprovalRequest], Awaitable[bool]] = self.deny
        self.initialized = False
        self._initialized = False
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
        self.create_session_calls: list[bool] = []
        self.new_count = 0
        self.describe_count = 0
        self.fail_model = False
        self._busy = ""
        self.tools = SimpleNamespace(skills={})
        self.agent = SimpleNamespace(refresh_skills=self.refresh_skills, skills={})
        self.tasks = SimpleNamespace(list=lambda: [])
        self.commands = CommandRegistry(cast("Harness", self))

    async def deny(self, request: ApprovalRequest) -> bool:
        return False

    async def initialize(self, *, create_session: bool = True) -> None:
        self.initialized = True
        self._initialized = True
        self.create_session_calls.append(create_session)

    async def refresh_skills(self) -> dict[str, Skill]:
        return {}

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
    try:
        async with asyncio.timeout(HANG_GUARD):
            while True:
                await pilot.pause(0.01)
                if not app.busy and (not app._queued_prompts or app._queue_paused):
                    return
    except TimeoutError:
        raise AssertionError("Backend task did not become idle") from None


async def send(app: NagentsApp, pilot: Pilot[None], text: str) -> None:
    composer = app.query_one(Composer)
    composer.load_text(text)
    composer.focus()
    await pilot.press("enter")


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance for an 8-bit sRGB triplet."""
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in (value / 255 for value in rgb)
    ]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))


def contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two opaque sRGB colors."""
    one, two = relative_luminance(first), relative_luminance(second)
    return (max(one, two) + 0.05) / (min(one, two) + 0.05)
