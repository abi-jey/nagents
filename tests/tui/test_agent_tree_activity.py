"""Agent row activity uses independent clocks without rebuilding the tree."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Literal

import pytest
from textual.app import App

from nagents.harness.agents.subagents import TaskInfo
from nagents.tui import tasks as tasks_module
from nagents.tui.tasks import AgentTree
from nagents.tui.themes import configure_theme

if TYPE_CHECKING:
    from textual.app import ComposeResult


class ActivityApp(App[None]):
    def __init__(self, *, animations: bool = True, compact: bool = False) -> None:
        super().__init__()
        self.compact = compact
        configure_theme(self, "ocean")
        self.harness = SimpleNamespace(config=SimpleNamespace(animations=animations))

    def compose(self) -> ComposeResult:
        yield AgentTree(compact=self.compact)


def test_activity_refresh_preserves_nodes_without_status_text_or_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"mono": 10.0}
    monkeypatch.setattr(tasks_module, "monotonic", lambda: clock["mono"])

    async def scenario() -> None:
        app = ActivityApp()
        async with app.run_test() as pilot:
            tree = app.query_one(AgentTree)
            parent = TaskInfo("parent", "Parent", started_at=935, phase="reasoning")
            child = TaskInfo("child", "Child", started_at=900, status="completed", completed_at=930)
            tree.update_tasks([parent, child], {"child": "parent"})
            await pilot.pause()
            node = tree.task_nodes["parent"]
            tree.move_cursor(node)
            node.collapse()
            await pilot.pause()
            phase = tree._fields["parent"].phase
            assert str(node.label)[7:] == " Parent [parent] (assistant)"
            assert str(tree.task_nodes["child"].label)[7:] == " Child [child] (assistant)"
            clock.update(mono=11)
            tree.refresh_activity()
            assert tree.task_nodes["parent"] is node
            assert tree.cursor_node is node
            assert not node.is_expanded
            assert tree._fields["parent"].phase > phase
            assert str(node.label)[7:] == " Parent [parent] (assistant)"
            assert str(tree.task_nodes["child"].label)[7:] == " Child [child] (assistant)"
            parent.phase = "tool_call"
            tree.update_tasks([parent, child], {"child": "parent"})
            assert tree.task_nodes["parent"] is node
            assert tree._fields["parent"].state == "tool"
            tree.update_tasks([parent], {})
            assert "child" not in tree._fields

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled"])
def test_compact_sidebar_keeps_motion_and_name_without_status_text_or_timer(
    status: Literal["running", "completed", "failed", "cancelled"],
) -> None:

    async def scenario() -> None:
        app = ActivityApp(compact=True)
        async with app.run_test(size=(31, 24)):
            tree = app.query_one(AgentTree)
            task = TaskInfo("agent", "A very long agent title that exceeds the sidebar", started_at=1000, phase="tool")
            tree.update_tasks([task], {})
            label = str(tree.task_nodes["agent"].label)
            assert label[3:] == " " + task.title
            assert tree._fields["agent"].state == "tool"
            task.status = status
            task.completed_at = 1065
            tree.refresh_activity()
            assert str(tree.task_nodes["agent"].label)[3:] == " " + task.title
            expected_state = {"running": "tool", "completed": "done", "failed": "error", "cancelled": "idle"}[status]
            assert tree._fields["agent"].state == expected_state

    asyncio.run(scenario())


@pytest.mark.parametrize("disabled_by", ["config", "level"])
def test_activity_respects_disabled_animation(monkeypatch: pytest.MonkeyPatch, disabled_by: str) -> None:
    clock = [10.0]
    monkeypatch.setattr(tasks_module, "monotonic", lambda: clock[0])

    async def scenario() -> None:
        app = ActivityApp(animations=disabled_by != "config")
        if disabled_by == "level":
            app.animation_level = "none"
        async with app.run_test():
            tree = app.query_one(AgentTree)
            task = TaskInfo("agent", "", phase="starting")
            tree.update_tasks([task], {})
            field = tree._fields["agent"]
            phase = field.phase
            clock[0] += 10
            task.status = "failed"
            tree.refresh_activity()
            assert field.phase == phase
            assert field.state == "error"
            assert all(spring.value == spring.target for spring in field.springs)
            assert str(tree.task_nodes["agent"].label)[7:] == " agent (assistant)"

    asyncio.run(scenario())
