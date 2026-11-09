"""Integration coverage for the TUI's state field and activity footer."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING
from xml.etree import ElementTree

import pytest
from rich.console import Console
from textual.widgets import Static

from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import TaskCompleted
from nagents.harness import TaskStarted
from nagents.harness.agents.subagents import TaskInfo
from nagents.harness.types import ApprovalRequest
from nagents.harness.types import TaskActivity
from nagents.tui import NagentsApp
from nagents.tui.screens import ApprovalModal
from nagents.tui.tasks import AgentTree
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_main_state_tracks_events_approval_and_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.animations = False
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)

            def current_state() -> str:
                return app._motion.state

            await app._event(TextChunkEvent(chunk="A visible answer"))
            assert current_state() == "responding"
            await app._event(ReasoningChunkEvent(chunk="planning"))
            assert current_state() == "working"

            await app._event(ToolCallEvent(id="tool-1", name="shell"))
            assert current_state() == "tool"
            await app._event(ToolResultEvent(id="tool-1", name="shell", result="ok"))
            assert current_state() == "working"
            await app._event(ToolCallEvent(id="delegate-1", name="delegate"))
            assert current_state() == "delegating"
            await app._event(ToolResultEvent(id="delegate-1", name="delegate", result="child started"))

            await app._event(TaskStarted(session_id="child-1", title="Review", prompt="Inspect"))
            assert current_state() == "delegating"
            await app._event(TaskCompleted(session_id="child-1", title="Review", result="Done"))
            assert current_state() == "returned"
            await app._event(DoneEvent())

            approval = asyncio.create_task(
                app.request_approval(ApprovalRequest("approval-1", "write_file", "Review", {}))
            )
            try:
                # The motion field changes before push_screen has mounted and
                # laid out the modal. A fixed sleep can click stale geometry on
                # a loaded runner, leaving the decision future pending forever.
                async with asyncio.timeout(HANG_GUARD):
                    while not isinstance(app.screen, ApprovalModal) or not app.screen.is_mounted:
                        await pilot.pause(0)
                    await pilot.pause(0)
                    assert current_state() == "approval"
                    assert await pilot.click("#deny")
                    assert await approval is False
                assert current_state() == "working"
            finally:
                # A failed input/assertion must not leave an orphaned approval
                # task waiting for a screen that run_test is about to remove.
                approval.cancel()
                await asyncio.gather(approval, return_exceptions=True)

            await app._event(ErrorEvent(message="Provider unavailable"))
            assert current_state() == "error"

            await send(app, pilot, "wait")
            async with asyncio.timeout(HANG_GUARD):
                while current_state() != "responding":
                    await pilot.pause(0)
                assert backend.running
                await pilot.press("escape")
                await idle(app, pilot)
            assert current_state() == "idle"
            assert backend.cancelled and not backend.running

    asyncio.run(scenario())


def test_direct_child_activity_is_independent_and_nested_rows_stay_in_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nagents.tui.app.time", lambda: 1316.0)

    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        tasks = [
            TaskInfo("child-tool", "Tool pass", depth=1, phase="tool", started_at=1000),
            TaskInfo("child-response", "Answer pass", depth=1, phase="responding"),
            TaskInfo("nested-child", "Nested", depth=2, parent_session_id="child-tool", phase="tool"),
        ]
        backend.tasks.list = lambda: tasks
        app = make_app(backend)
        async with app.run_test(size=(132, 38)) as pilot:
            await idle(app, pilot)
            app._refresh_tasks()
            now = monotonic()
            await app._event(
                TaskActivity(
                    session_id="child-tool",
                    root_session_id=backend.session_id,
                    parent_session_id=backend.session_id,
                    depth=1,
                    activation=1,
                    sequence=1,
                    timestamp=now,
                    turn_id="turn-1",
                    payload={"type": "tool_call", "phase": "tool", "name": "shell"},
                )
            )
            await app._event(
                TaskActivity(
                    session_id="child-response",
                    root_session_id=backend.session_id,
                    parent_session_id=backend.session_id,
                    depth=1,
                    activation=1,
                    sequence=1,
                    timestamp=now,
                    turn_id="turn-1",
                    payload={"type": "text_chunk", "phase": "responding"},
                )
            )
            app._render_background_activity(now)

            content = str(app.query_one("#background-status", Static).content)
            assert "Tool pass" in content and "Answer pass" in content
            assert "05:16" in content
            assert "Nested" not in content
            assert app._visible_task_ids == {"child-tool", "child-response"}
            tree = app.query_one(AgentTree)
            assert {"child-tool", "child-response", "nested-child"} <= tree.task_nodes.keys()
            assert app._task_fields["child-tool"].state == "tool"
            assert app._task_fields["child-response"].state == "responding"

            tasks[0].status = "completed"
            tasks[0].completed_at = 1065
            tasks[1].status = "completed"
            app._recent_task_id = "child-tool"
            app._recent_task_until = now + 10
            app._render_background_activity(now)
            finished = str(app.query_one("#background-status", Static).content)
            assert "01:05" in finished and "05:16" not in finished
            assert str(app._task_fields["child-tool"].state) == "done"

    asyncio.run(scenario())


def test_provider_model_footer_tracks_selected_harness_model_without_relayout(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(132, 38)) as pilot:
            await idle(app, pilot)
            footer = app.query_one("#footer")
            model_widget = app.query_one("#provider-model", Static)
            footer_region = footer.region
            model_region = model_widget.region
            expected = f"{harness.config.provider_profile().kind} · {harness.agent.provider.model}"
            assert str(model_widget.content) == expected

            await harness.set_model("integration-selected-model")
            app._refresh_model()
            await pilot.pause()

            assert str(model_widget.content) == f"{harness.config.provider_profile().kind} · integration-selected-model"
            assert footer.region == footer_region
            assert model_widget.region == model_region

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (132, 38)])
def test_lifecycle_sweep_reveals_screen_capture_without_relayout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    async def scenario() -> None:
        now = [0.0]
        monkeypatch.setattr("nagents.tui.app.monotonic", lambda: now[0])
        backend = FakeHarness(tmp_path)
        backend.config.animations = True
        backend.messages = [
            Message(role="user", content="Render a preserved question."),
            Message(role="assistant", content="A **rendered** answer with enough text to sweep."),
        ]
        app = make_app(backend)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            app._animate_activity()
            regions = tuple(
                app.query_one(selector).region
                for selector in ("#conversation", "#composer", "#status", "#provider-model")
            )
            initial_svg = app.export_screenshot()
            initial_text = _rendered_screen_text(app)
            assert "openai · gpt-6-astra" not in initial_text

            now[0] = 0.5
            app._animate_activity()
            mid_sweep_svg = app.export_screenshot()
            mid_sweep_text = _rendered_screen_text(app)
            assert app._sweep.enabled and app._sweep.amount > 0
            assert "openai · gpt-6-astra" in mid_sweep_text

            now[0] = 2.0
            app._animate_activity()
            final_svg = app.export_screenshot()
            final_text = _rendered_screen_text(app)
            assert not app._sweep.enabled
            assert mid_sweep_svg != initial_svg and final_svg != mid_sweep_svg
            assert "openai · gpt-6-astra" in final_text
            assert "Render a preserved question." in final_text
            assert "A rendered answer with enough text to sweep." in final_text
            assert (
                tuple(
                    app.query_one(selector).region
                    for selector in ("#conversation", "#composer", "#status", "#provider-model")
                )
                == regions
            )

    asyncio.run(scenario())


def _rendered_screen_text(app: NagentsApp) -> str:
    renderable = app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)
    console = Console(width=app.size.width, height=app.size.height, force_terminal=False, color_system=None)
    with console.capture() as capture:
        console.print(renderable, end="")
    screenshot = app.export_screenshot()
    assert ElementTree.fromstring(screenshot).tag.endswith("svg")
    return capture.get()
