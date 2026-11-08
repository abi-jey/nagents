"""Keyboard/mouse task inspection without provider calls or persistent jobs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import ClassVar

import pytest
from textual import on
from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.harness.agents.subagents import TaskInfo
from nagents.session import SessionManager
from nagents.tui.tasks import AgentTree
from nagents.tui.tasks import TaskScreen
from nagents.tui.themes import configure_theme
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ConversationMarkdown
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import Turn
from nagents.types import Message
from nagents.types import ToolCall
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from textual.app import ComposeResult
    from textual.binding import BindingType


def history_text(history: Vertical) -> str:
    """Read shared turn bodies rather than the container's renderable."""
    return "\n".join(
        (turn.body.source or "") if isinstance(turn.body, ConversationMarkdown) else str(turn.body.render())
        for turn in history.query(Turn)
    )


class InspectorApp(App[None]):
    def __init__(self, tasks: Callable[[], list[TaskInfo]]) -> None:
        super().__init__()
        configure_theme(self, "ocean")
        self.tasks = tasks
        self.result: tuple[str, str] | None = None

    def on_mount(self) -> None:
        async def history(session_id: str) -> list[Message]:
            return [
                Message(role="user", content=f"Question for {session_id}"),
                Message(role="assistant", content="Saved child response"),
            ]

        self.push_screen(TaskScreen(self.tasks, history, parents=lambda: {}), self.finished)

    def finished(self, result: tuple[str, str] | None) -> None:
        self.result = result


class HistoryInspectorApp(App[None]):
    def __init__(self, tasks: Callable[[], list[TaskInfo]], history: Callable[[str], Awaitable[list[Message]]]) -> None:
        super().__init__()
        configure_theme(self, "ocean")
        self.tasks = tasks
        self.history = history

    def on_mount(self) -> None:
        self.push_screen(TaskScreen(self.tasks, self.history, parents=lambda: {}))


@pytest.mark.parametrize("replace_target", [False, True])
def test_pending_history_cannot_update_a_removed_or_replaced_view(tmp_path: Path, replace_target: bool) -> None:
    async def scenario() -> None:
        manager = SessionManager(tmp_path / "history.db")
        for session_id in ("s1", "s2"):
            await manager.get_or_create_session(session_id, "test")
            await manager.add_message(session_id, Message(role="assistant", content=f"Saved answer for {session_id}"))
        tasks = [TaskInfo("s1", "", status="completed"), TaskInfo("s2", "", status="completed")]
        started, release = asyncio.Event(), asyncio.Event()
        hold = False

        async def history(session_id: str) -> list[Message]:
            messages = await manager.get_history(session_id)
            if hold and session_id == "s1":
                started.set()
                await release.wait()
            return messages

        app = HistoryInspectorApp(lambda: tasks, history)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            assert screen.get_history is history
            # Selection can cancel an earlier exclusive history worker. Wait
            # for this view's rendered data, rather than every global worker.
            async with asyncio.timeout(HANG_GUARD):
                while "Saved answer for s1" not in history_text(screen.query_one("#task-history", Vertical)):
                    await pilot.pause()
            # Make this pending read differ from the already-rendered snapshot.
            await manager.add_message("s1", Message(role="user", content="New saved question"))
            hold = True
            worker = screen.load_history("s1")
            async with asyncio.timeout(HANG_GUARD):
                await started.wait()
            original = screen.query_one("#task-history", Vertical)
            scroll = screen.query_one("#task-history-scroll", VerticalScroll)
            await original.remove()
            assert screen.is_mounted and screen.is_attached
            sentinel = Static("Replacement view has no history yet", markup=False)
            replacement = Vertical(sentinel, id="task-history")
            if replace_target:
                await scroll.mount(replacement)
            release.set()
            await worker.wait()
            if replace_target:
                assert list(replacement.children) == [sentinel]
                assert not replacement.query(Turn)
                fresh = screen.load_history("s1")
                await fresh.wait()
                assert "Saved answer for s1" in history_text(replacement)
            await app.pop_screen()
            reopened = TaskScreen(lambda: tasks, manager.get_history, parents=lambda: {}, selected_session_id="s2")
            await app.push_screen(reopened)
            await pilot.pause()
            async with asyncio.timeout(HANG_GUARD):
                while "Saved answer for s2" not in history_text(reopened.query_one("#task-history", Vertical)):
                    await pilot.pause()
            text = history_text(reopened.query_one("#task-history", Vertical))
            assert "Saved answer for s2" in text and "Saved answer for s1" not in text

    asyncio.run(scenario())


def test_polling_does_not_cancel_pending_history() -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def history(session_id: str) -> list[Message]:
            started.set()
            await release.wait()
            return [Message(role="assistant", content="Slow saved history")]

        tasks = [TaskInfo("slow", "", status="completed")]
        app = HistoryInspectorApp(lambda: tasks, history)
        async with app.run_test() as pilot:
            await started.wait()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            workers = tuple(screen.workers)
            for _ in range(3):
                screen.refresh_tasks()
            assert tuple(screen.workers) == workers
            release.set()
            async with asyncio.timeout(HANG_GUARD):
                while "Slow saved history" not in history_text(screen.query_one("#task-history", Vertical)):
                    await pilot.pause()

    asyncio.run(scenario())


def test_inspector_arrows_show_history_and_send_explicit_followup() -> None:
    async def scenario() -> None:
        tasks = [TaskInfo("s1", "", status="completed"), TaskInfo("s2", "", status="completed")]
        app = InspectorApp(lambda: tasks)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            assert screen.selected_session_id == "s2"
            assert "Question for s2" in history_text(screen.query_one("#task-history", Vertical))
            assert isinstance(screen.query_one("#task-followup", TextArea), Composer)
            await pilot.press("enter")
            assert screen.query_one("#task-followup", TextArea).has_focus
            await pilot.press("m", "o", "r", "e")
            assert app.result is None
            await pilot.click("#task-send")
            assert app.result == ("s2", "more")

    asyncio.run(scenario())


def test_inspector_running_task_preserves_draft_until_completed() -> None:
    async def scenario() -> None:
        tasks = [TaskInfo("s1", "")]
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
        tasks = [TaskInfo("s1", "", status="completed"), TaskInfo("s2", "", status="completed")]
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


def test_inspector_uses_shared_markdown_turns_and_tool_cards() -> None:
    async def scenario() -> None:
        markdown = "## Answer\n\n**Bold** and `code`\n\n```python\nprint('ok')\n```"
        messages = [
            Message(role="user", content="Literal [brackets]"),
            Message(
                role="assistant",
                content=markdown,
                tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})],
            ),
            Message(role="tool", content="File contents", tool_call_id="call-1", name="read_file"),
        ]

        async def history(session_id: str) -> list[Message]:
            assert session_id == "s1"
            return messages

        tasks = [TaskInfo("s1", "", status="completed")]
        app = HistoryInspectorApp(lambda: tasks, history)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TaskScreen)
            history_view = screen.query_one("#task-history", Vertical)
            async with asyncio.timeout(HANG_GUARD):
                while not any(card.output_text == "File contents" for card in history_view.query(ToolCard)):
                    await pilot.pause()
            turns = list(history_view.query(Turn))
            assert [turn.role for turn in turns] == ["user", "assistant"]
            assert isinstance(turns[0].body, Static)
            assert str(turns[0].body.render()) == "Literal [brackets]"
            assert isinstance(turns[1].body, ConversationMarkdown)
            assert turns[1].body.source == markdown
            cards = list(history_view.query(ToolCard))
            assert len(cards) == 1
            card = cards[0]
            assert card.call_id == "call-1" and card.tool == "read_file"
            assert card.output_text == "File contents"
            assert card.collapsed
            card.collapsed = False
            messages.append(Message(role="assistant", content="Updated response"))
            await screen.load_history("s1").wait()
            refreshed = history_view.query_one(ToolCard)
            assert refreshed is not card
            assert not refreshed.collapsed
            assert refreshed.output_text == "File contents"

    asyncio.run(scenario())


class ParentDraftApp(App[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("f6", "parent_pane", show=False, priority=True)]

    def __init__(self) -> None:
        super().__init__()
        configure_theme(self, "ocean")
        self.parent_pane_calls = 0
        self.drafts: dict[str, str] = {}
        self.result: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield Composer(lambda _: False)

    def action_parent_pane(self) -> None:
        # Mirror NagentsApp's priority binding delegation to its active view.
        if isinstance(self.screen, TaskScreen):
            self.screen.action_focus_pane()
            return
        self.parent_pane_calls += 1

    def finished(self, result: tuple[str, str] | None) -> None:
        self.result = result

    def open_inspector(self) -> TaskScreen:
        async def history(session_id: str) -> list[Message]:
            return [Message(role="assistant", content=f"Answer for {session_id}")]

        screen = TaskScreen(
            lambda: [TaskInfo("s1", "", status="completed")],
            history,
            parents=lambda: {},
            selected_session_id="s1",
            drafts=self.drafts,
        )
        self.push_screen(screen, self.finished)
        return screen


@pytest.mark.parametrize("back_key", ["escape", "alt+left", "button"])
def test_fullscreen_back_retains_parent_and_agent_drafts(back_key: str) -> None:
    async def scenario() -> None:
        app = ParentDraftApp()
        async with app.run_test(size=(100, 30)) as pilot:
            parent_screen = app.screen
            parent = app.query_one("#composer", Composer)
            parent.load_text("Unsent parent draft")
            screen = app.open_inspector()
            await pilot.pause()
            assert screen.region == parent_screen.region
            assert screen.query_one("#task-dialog", Vertical).region == screen.region
            screen.query_one("#task-followup", Composer).load_text("Unsent child draft")
            if back_key == "button":
                await pilot.click("#task-close")
            else:
                await pilot.press(back_key)
            await pilot.pause()
            assert app.screen is parent_screen
            assert parent.text == "Unsent parent draft"
            assert app.result is None
            assert app.drafts == {"s1": "Unsent child draft"}
            reopened = app.open_inspector()
            await pilot.pause()
            assert reopened.query_one("#task-followup", Composer).text == "Unsent child draft"
            assert parent.text == "Unsent parent draft"

    asyncio.run(scenario())


def test_f6_focus_is_scoped_to_task_screen() -> None:
    async def scenario() -> None:
        app = ParentDraftApp()
        async with app.run_test(size=(100, 30)) as pilot:
            parent = app.query_one("#composer", Composer)
            parent.load_text("Parent draft")
            parent.focus()
            await pilot.press("f6")
            assert app.parent_pane_calls == 1
            screen = app.open_inspector()
            await pilot.pause()
            composer = screen.query_one("#task-followup", Composer)
            scroll = screen.query_one("#task-history-scroll", VerticalScroll)
            assert screen.query_one(AgentTree).has_focus
            await pilot.press("f6")
            assert composer.has_focus
            await pilot.press("f6")
            assert scroll.has_focus
            await pilot.press("f6")
            assert composer.has_focus
            assert app.parent_pane_calls == 1
            assert parent.text == "Parent draft"
            await pilot.press("escape")
            parent.focus()
            await pilot.press("f6")
            assert app.parent_pane_calls == 2

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
        self.opened = event.session_id


def test_tree_has_ancestry_keyboard_open_and_preserves_collapse_and_selection() -> None:
    async def scenario() -> None:
        app = TreeApp()
        async with app.run_test(size=(40, 12)) as pilot:
            tree = app.query_one(AgentTree)
            tasks = [TaskInfo("s1", ""), TaskInfo("s2", ""), TaskInfo("s3", "")]
            parents = {"s2": "s1"}
            tree.update_tasks(tasks, parents)
            await pilot.pause()
            assert tree.task_nodes["s2"].parent is tree.task_nodes["s1"]
            tree.move_cursor(tree.task_nodes["s2"])
            tree.focus()
            await pilot.press("enter")
            assert app.opened == "s2"
            await pilot.click(AgentTree, offset=(8, tree.task_nodes["s3"].line))
            assert app.opened == "s3"
            tree.task_nodes["s1"].collapse()
            await pilot.pause()
            tree.move_cursor(tree.task_nodes["s3"])
            tasks[0].status = "completed"
            tree.update_tasks(tasks, parents)
            await pilot.pause()
            assert not tree.task_nodes["s1"].is_expanded
            assert tree.cursor_node is not None and tree.cursor_node.data == "s3"

    asyncio.run(scenario())


def test_large_tree_scrolls_without_losing_completed_agents() -> None:
    async def scenario() -> None:
        app = TreeApp()
        async with app.run_test(size=(40, 10)) as pilot:
            tree = app.query_one(AgentTree)
            tasks = [TaskInfo(f"s{index + 1:x}", "", status="completed") for index in range(30)]
            tree.update_tasks(tasks, {})
            await pilot.pause()
            assert len(tree.task_nodes) == 30
            tree.focus()
            await pilot.press("end")
            # End animates scrolling; CPU idle does not guarantee completion.
            await pilot.wait_for_scheduled_animations()
            assert tree.cursor_node is not None and tree.cursor_node.data == "s1e"
            assert tree.scroll_y > 0
            await pilot.press("enter")
            assert app.opened == "s1e"

    asyncio.run(scenario())
