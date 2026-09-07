"""Background results stay visible and resume the parent transcript correctly."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Markdown
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskStarted
from nagents.tui import NagentsApp
from nagents.tui.tasks import AgentTree
from nagents.tui.tasks import TaskScreen
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import Turn
from tests.test_tui import FakeHarness
from tests.test_tui import idle
from tests.test_tui import make_app
from tests.test_tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_late_subagent_result_does_not_overwrite_parent_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.events = [
            TaskStarted("task-1", "quiet maple", "Review the docs"),
            TextChunkEvent(chunk="The reviewer is working."),
            TextDoneEvent(text="The reviewer is working."),
            TaskCompleted("task-1", "quiet maple", "Documentation checked."),
            TextChunkEvent(chunk="The review is complete."),
            TextDoneEvent(text="The review is complete."),
            DoneEvent(),
        ]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "Review")
            await idle(app, pilot)
            assert [widget.source for widget in app.query(Markdown)] == [
                "The reviewer is working.",
                "The review is complete.",
            ]
            card = app.query_one(ToolCard)
            assert "quiet maple" in card.title
            assert "complete" in card.title
            assert "Documentation checked." in card.output_text

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_three_offline_jobs_render_in_tui_and_tasks_view(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "demo subagents")
            async with asyncio.timeout(10):
                while app.busy:
                    await pilot.pause(0.02)
            tasks = harness.tasks.list()
            assert len(tasks) == 3
            assert all(task.status == "completed" for task in tasks)
            cards = [card for card in app.query(ToolCard) if card.call_id.startswith("task:")]
            assert len(cards) == 3
            assert all("complete" in card.title for card in cards)
            app.command("/tasks")
            await pilot.pause()
            assert isinstance(app.screen, TaskScreen)
            assert set(app.screen.query_one(AgentTree).task_nodes) == {task.id for task in tasks}
            assert all(task.name in [info.name for info in app.screen.infos.values()] for task in tasks)

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_agent_tree_continues_saved_child_and_notifies_parent(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(132, 40)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "demo subagents")
            await idle(app, pilot)
            tree = app.default_screen.query_one("#agent-tree", AgentTree)
            assert tree.region.x < app.query_one("#conversation").region.x
            assert len(tree.task_nodes) == 3
            await pilot.press("ctrl+t", "down", "enter")
            await pilot.pause()
            assert isinstance(app.screen, TaskScreen)
            task_id = app.screen.selected_id
            child_session = next(task.child_session_id for task in harness.tasks.list() if task.id == task_id)
            app.screen.query_one("#task-followup", TextArea).load_text("Follow-up marker: inspect the result again.")
            await pilot.click("#task-send")
            await idle(app, pilot)
            info = next(task for task in harness.tasks.list() if task.id == task_id)
            assert info.followups == 1 and info.status == "completed"
            assert info.child_session_id == child_session
            history = await harness.task_history(task_id)
            assert any(
                message.role == "user" and message.content == "Follow-up marker: inspect the result again."
                for message in history
            )
            parent = await harness.history()
            assert any(
                isinstance(message.content, str)
                and '"human_messages"' in message.content
                and "Follow-up marker" in message.content
                for message in parent
            )
            assert any("Follow-up to" in str(turn.body.render()) for turn in app.query(Turn) if turn.role == "user")
            assert len([card for card in app.query(ToolCard) if card.call_id.startswith("task:")]) == 4
            assert task_id not in app._task_drafts

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_narrow_task_tree_keeps_unsent_draft_on_escape(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "demo subagents")
            await idle(app, pilot)
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert isinstance(app.screen, TaskScreen)
            task_id = app.screen.selected_id
            app.screen.query_one("#task-followup", TextArea).load_text("Keep my follow-up draft")
            await pilot.press("escape")
            assert app._task_drafts[task_id] == "Keep my follow-up draft"
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert isinstance(app.screen, TaskScreen)
            assert app.screen.query_one("#task-followup", TextArea).text == "Keep my follow-up draft"
            assert app.screen.query_one("#task-send").region.bottom <= app.size.height
            await pilot.press("escape")
            assert app.query_one(Composer).has_focus
            assert harness.tasks.list()[0].followups == 0
            assert "no microphone" not in str(app.query_one("#status", Static).render())

    asyncio.run(scenario())
