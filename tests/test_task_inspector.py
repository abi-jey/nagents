"""Keyboard/mouse task inspection without provider calls or persistent jobs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual import on
from textual.app import App
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.harness.subagents import TaskInfo
from nagents.tui.tasks import AgentTree
from nagents.tui.tasks import TaskScreen
from nagents.tui.themes import configure_theme
from nagents.types import Message

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult


class InspectorApp(App[None]):
    def __init__(self, tasks: Callable[[], list[TaskInfo]]) -> None:
        super().__init__()
        configure_theme(self, "ocean")
        self.tasks = tasks
        self.result: tuple[str, str] | None = None

    def on_mount(self) -> None:
        async def history(task_id: str) -> list[Message]:
            return [
                Message(role="user", content=f"Question for {task_id}"),
                Message(role="assistant", content="Saved child response"),
            ]

        self.push_screen(TaskScreen(self.tasks, history, parents=lambda: {}), self.finished)

    def finished(self, result: tuple[str, str] | None) -> None:
        self.result = result


def test_inspector_arrows_show_history_and_send_explicit_followup() -> None:
    async def scenario() -> None:
        tasks = [TaskInfo("one", "calm finch", status="completed"), TaskInfo("two", "bright maple", status="completed")]
        app = InspectorApp(lambda: tasks)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            assert screen.selected_id == "two"
            assert "Question for two" in str(screen.query_one("#task-history", Static).render())
            await pilot.press("enter")
            assert screen.query_one("#task-followup", TextArea).has_focus
            await pilot.press("m", "o", "r", "e")
            assert app.result is None
            await pilot.click("#task-send")
            assert app.result == ("two", "more")

    asyncio.run(scenario())


def test_inspector_running_task_preserves_draft_until_completed() -> None:
    async def scenario() -> None:
        tasks = [TaskInfo("one", "calm finch")]
        app = InspectorApp(lambda: tasks)
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            assert screen.query_one("#task-send", Button).disabled
            composer = screen.query_one("#task-followup", TextArea)
            composer.load_text("Inspect this too")
            tasks[0].status = "completed"
            screen.refresh_tasks()
            await pilot.pause()
            assert not screen.query_one("#task-send", Button).disabled
            assert composer.text == "Inspect this too"
            assert screen.query_one("#task-send", Button).region.bottom <= app.size.height
            await pilot.press("escape")
            assert app.result is None

    asyncio.run(scenario())


def test_inspector_drafts_are_per_agent() -> None:
    async def scenario() -> None:
        tasks = [TaskInfo("one", "calm finch", status="completed"), TaskInfo("two", "bright maple", status="completed")]
        app = InspectorApp(lambda: tasks)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            composer = screen.query_one("#task-followup", TextArea)
            composer.load_text("Follow up one")
            options = screen.query_one(AgentTree)
            options.focus()
            await pilot.press("down")
            assert composer.text == ""
            composer.load_text("Follow up two")
            options.focus()
            await pilot.press("up")
            assert composer.text == "Follow up one"

    asyncio.run(scenario())


class TreeApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        configure_theme(self, "ocean")
        self.opened = ""

    def compose(self) -> ComposeResult:
        yield AgentTree()

    @on(AgentTree.Opened)
    def open_agent(self, event: AgentTree.Opened) -> None:
        self.opened = event.task_id


def test_tree_has_ancestry_keyboard_open_and_preserves_collapse_and_selection() -> None:
    async def scenario() -> None:
        app = TreeApp()
        async with app.run_test(size=(40, 12)) as pilot:
            tree = app.query_one(AgentTree)
            tasks = [TaskInfo("one", "calm finch"), TaskInfo("two", "bright maple"), TaskInfo("three", "clear otter")]
            parents = {"two": "one"}
            tree.update_tasks(tasks, parents)
            await pilot.pause()
            assert tree.task_nodes["two"].parent is tree.task_nodes["one"]
            tree.move_cursor(tree.task_nodes["two"])
            tree.focus()
            await pilot.press("enter")
            assert app.opened == "two"
            await pilot.click(AgentTree, offset=(8, tree.task_nodes["three"].line))
            assert app.opened == "three"
            tree.task_nodes["one"].collapse()
            await pilot.pause()
            tree.move_cursor(tree.task_nodes["three"])
            tasks[0].status = "completed"
            tree.update_tasks(tasks, parents)
            await pilot.pause()
            assert not tree.task_nodes["one"].is_expanded
            assert tree.cursor_node is not None and tree.cursor_node.data == "three"

    asyncio.run(scenario())


def test_large_tree_scrolls_without_losing_completed_agents() -> None:
    async def scenario() -> None:
        app = TreeApp()
        async with app.run_test(size=(40, 10)) as pilot:
            tree = app.query_one(AgentTree)
            tasks = [TaskInfo(str(index), f"agent {index}", status="completed") for index in range(30)]
            tree.update_tasks(tasks, {})
            await pilot.pause()
            assert len(tree.task_nodes) == 30
            tree.focus()
            await pilot.press("end")
            await pilot.pause()
            assert tree.cursor_node is not None and tree.cursor_node.data == "29"
            assert tree.scroll_y > 0
            await pilot.press("enter")
            assert app.opened == "29"

    asyncio.run(scenario())
