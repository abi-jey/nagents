"""Tool activity stays compact and user-controlled throughout its lifecycle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button
from textual.widgets import Static

from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.types import ApprovalRequest
from nagents.harness.types import ToolOutput
from nagents.tui.screens import ApprovalModal
from nagents.tui.widgets import OUTPUT_LIMIT
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolCard
from nagents.types import Message
from nagents.types import ToolCall
from tests.support.hang_guard import HANG_GUARD
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.types import ToolArguments


@pytest.mark.parametrize("outcome", ["complete", "failed", "cancelled"])
@pytest.mark.parametrize("expanded", [False, True])
def test_tool_lifecycle_preserves_user_choice(tmp_path: Path, outcome: str, expanded: bool) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            # Output-before-call must get the same compact card and later hint.
            await app._event(ToolOutput("tool", "shell", "first output\n"))
            await pilot.pause()
            card = app.query_one(ToolCard)
            assert card.collapsed
            assert card.region.height == 1
            title = card.query_one("CollapsibleTitle", Static)
            await pilot.click(title)
            await pilot.pause()
            assert not card.collapsed
            assert app.focused is title
            if not expanded:
                await pilot.press("enter")
                await pilot.pause()
                assert card.collapsed
            await app._event(ToolCallEvent(id="tool", name="shell", arguments={"command": "printf '[red]literal'"}))
            await app._event(ToolOutput("tool", "shell", "large output\n" * OUTPUT_LIMIT))
            await pilot.pause()
            assert card.collapsed is not expanded
            assert "printf '[red]literal'" in str(title.content)
            assert "truncated" in card.output_text
            assert len(card.output_text) < OUTPUT_LIMIT + 100
            if outcome == "cancelled":
                card.cancel()
            else:
                await app._event(
                    ToolResultEvent(
                        id="tool",
                        name="shell",
                        result={"diff": "--- a\n+++ b\n-old\n+new"},
                        error="PRIVATE failure [red]details[/red]" if outcome == "failed" else None,
                        duration_ms=1250,
                    )
                )
            await pilot.pause()
            assert card.collapsed is not expanded
            assert card.has_class(outcome)
            assert outcome in str(title.content)
            assert "PRIVATE" not in str(title.content)
            assert "PRIVATE" not in str(app.query_one("#status", Static).content)
            assert app.focused is title
            if outcome != "cancelled":
                assert "1.25s" in str(title.content)
            if not expanded:
                assert card.region.height == 1
                assert card.output.region.height == 0
                await pilot.press("enter")
                await pilot.pause()
            assert not card.collapsed
            assert card.output.region.height > 0
            if not expanded:
                assert title.region.y >= app.query_one("#conversation").content_region.y
                assert await pilot.click(title)
                await pilot.pause()
                assert card.collapsed
                await pilot.press("space")
                await pilot.pause()
            if outcome == "failed":
                assert "PRIVATE failure [red]details[/red]" in str(card.output.content)
            await pilot.press("enter")
            await pilot.pause()
            assert card.collapsed
            assert card.region.height == 1

    asyncio.run(scenario())


def test_restored_tool_history_is_compact_and_inspectable(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.tab_action = "focus"
        backend.messages = [
            Message(
                role="assistant", tool_calls=[ToolCall(id="ok", name="read_file", arguments={"path": "src/main.py"})]
            ),
            Message(role="tool", tool_call_id="ok", name="read_file", content="saved output"),
            Message(role="tool", tool_call_id="bad", name="shell", content="Error: saved failure"),
        ]
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            cards = list(app.query(ToolCard))
            assert len(cards) == 2
            assert all(card.collapsed and card.region.height == 1 for card in cards)
            assert cards[1].has_class("failed")
            assert "src/main.py" in str(cards[0].query_one("CollapsibleTitle", Static).content)
            assert app.focused is app.query_one(Composer)
            # Reach a header through ordinary keyboard traversal, then inspect.
            for _ in range(8):
                await pilot.press("tab")
                if app.focused is cards[1].query_one("CollapsibleTitle"):
                    break
            assert app.focused is cards[1].query_one("CollapsibleTitle")
            await pilot.press("enter")
            await pilot.pause()
            assert not cards[1].collapsed
            assert "Error: saved failure" in str(cards[1].output.content)
            assert cards[0].collapsed

    asyncio.run(scenario())


def test_long_unicode_and_markup_summary_keeps_outcome_on_screen(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            tool = "[red]工具[/red]\n" * 40
            command = "[blue]界[/blue]\t" * 1000
            await app._event(ToolCallEvent(id="wide", name=tool, arguments={"command": command}))
            await app._event(ToolResultEvent(id="wide", name=tool, error="hidden error", duration_ms=1250))
            card = app.query_one(ToolCard)
            for width in (80, 60, 100, 80):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                title = card.query_one("CollapsibleTitle", Static)
                rendered = title.render_line(0).text
                assert "failed  1.25s" in rendered
                assert "…" in rendered
                assert "[red]" in rendered
                assert card.region.height == title.region.height == 1
                assert card.region.right <= width
                assert app.query_one(Composer).region.bottom <= 24
            await pilot.click(title)
            await pilot.pause()
            assert not card.collapsed
            assert str(card.query_one(".tool-name", Static).content) == tool

    asyncio.run(scenario())


def test_tool_output_and_failure_preserve_scrollback_and_composer_focus(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [Message(role="assistant", content="Earlier paragraph.\n\n" * 40)]
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            conversation = app.query_one("#conversation", VerticalScroll)
            conversation.scroll_home(animate=False)
            await pilot.pause()
            await app._event(ToolCallEvent(id="offscreen", name="shell", arguments={"command": "false"}))
            await app._event(ToolOutput("offscreen", "shell", "output\n" * OUTPUT_LIMIT))
            await app._event(ToolResultEvent(id="offscreen", name="shell", error="failure"))
            await pilot.pause()
            assert conversation.scroll_y == 0
            assert app.focused is app.query_one(Composer)
            assert app.query_one(ToolCard).collapsed
            assert app.query_one(ToolCard).region.height == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("expanded", [False, True])
def test_escape_cancellation_preserves_tool_expansion(tmp_path: Path, expanded: bool) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "wait")
            assert backend.running
            await app._event(ToolCallEvent(id="cancel", name="shell", arguments={"command": "sleep 60"}))
            await pilot.pause()
            card = app.query_one(ToolCard)
            if expanded:
                await pilot.click(card.query_one("CollapsibleTitle"))
                await pilot.pause()
            assert card.collapsed is not expanded
            await pilot.press("escape")
            await idle(app, pilot)
            assert backend.cancelled
            assert card.has_class("cancelled")
            assert card.collapsed is not expanded
            assert "cancelled" in str(card.query_one("CollapsibleTitle", Static).content)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
@pytest.mark.parametrize("tab_action", ["agent", "complete", "focus"])
def test_pane_focus_and_tool_keyboard_navigation(tmp_path: Path, size: tuple[int, int], tab_action: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.tab_action = tab_action
        app = make_app(backend)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            for index in range(30):
                await app._event(ToolCallEvent(id=str(index), name="read_file", arguments={"path": f"file{index}"}))
            await pilot.pause()
            composer = app.query_one(Composer)
            composer.load_text("unsent draft")
            composer.focus()
            conversation = app.query_one("#conversation", VerticalScroll)
            conversation.scroll_end(animate=False)
            await pilot.pause()
            before = conversation.scroll_y
            assert before > 0
            assert "F6" in str(conversation.border_title)
            await pilot.press("f6")
            assert app.focused is conversation
            assert conversation.scroll_y == before
            await pilot.press("pageup")
            await pilot.pause()
            assert conversation.scroll_y < before
            await pilot.press("tab")
            first, second = list(app.query(ToolCard))[:2]
            assert app.focused is first.query_one("CollapsibleTitle")
            await pilot.press("space")
            await pilot.pause()
            assert not first.collapsed
            await pilot.press("enter")
            await pilot.pause()
            assert first.collapsed
            await pilot.press("tab")
            assert app.focused is second.query_one("CollapsibleTitle")
            await pilot.press("shift+tab")
            assert app.focused is first.query_one("CollapsibleTitle")
            await pilot.press("f6")
            assert app.focused is composer
            assert composer.text == "unsent draft"
            assert not backend.prompts
            assert backend.config.tab_action == tab_action

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
def test_approval_paging_from_decision_buttons_keeps_exact_proposal(tmp_path: Path, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("keep draft")
            preview = "\n".join(f"+exact proposal line {index}" for index in range(120))
            arguments: ToolArguments = {"path": "fixture", "content": "x" * 20_000}
            approval = asyncio.create_task(
                app.request_approval(ApprovalRequest("page", "edit_file", "Exact", arguments, preview))
            )
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ApprovalModal)
            scroll = modal.query_one(".approval-content", VerticalScroll)
            deny = modal.query_one("#deny", Button)
            allow = modal.query_one("#allow", Button)
            async with asyncio.timeout(HANG_GUARD):
                while app.focused is not deny:
                    await pilot.pause()
            assert "PgUp/PgDn" in str(modal.query_one(".dialog-hint", Static).content)
            assert modal.request.arguments == arguments
            assert modal.request.preview == preview
            assert any("+exact proposal line 119" in str(item.content) for item in scroll.query(Static))
            assert deny.region.bottom <= size[1]
            assert allow.region.right <= size[0]
            await pilot.press("f6")
            assert app.focused is deny
            for button in (deny, allow):
                assert app.focused is button
                before = scroll.scroll_y
                await pilot.press("pagedown")
                await pilot.pause()
                assert scroll.scroll_y > before
                assert app.focused is button
                await pilot.press("pageup")
                await pilot.pause()
                assert scroll.scroll_y == before
                assert app.focused is button
                assert not approval.done()
                if button is deny:
                    await pilot.press("tab")
            await pilot.press("escape")
            assert not await approval
            await pilot.pause()
            assert app.screen.focused is composer
            assert composer.text == "keep draft"

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
def test_opening_large_tool_keeps_header_clickable_after_animations(tmp_path: Path, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            for index in range(3):
                await app._event(ToolCallEvent(id=str(index), name="shell", arguments={"command": "check"}))
            card = list(app.query(ToolCard))[-1]
            card.append_output("\n".join(f"line {index}" for index in range(100)))
            card.finish(None, "Failed", 100)
            await pilot.pause()
            title = card.query_one("CollapsibleTitle")
            assert await pilot.click(title)
            await pilot.pause()
            async with asyncio.timeout(5):
                await app.animator.wait_until_complete()
            await pilot.pause()
            assert not card.collapsed
            assert card.region.height > size[1]
            assert title.region.y >= app.query_one("#conversation").content_region.y
            assert await pilot.click(title)
            await pilot.pause()
            assert card.collapsed

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
def test_focused_visible_header_survives_growth_without_chasing_scrollback(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [Message(role="assistant", content="Earlier context.\n\n" * 40)]
        app = make_app(backend)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            conversation = app.query_one("#conversation", VerticalScroll)
            await app._event(ToolCallEvent(id="growing", name="shell", arguments={"command": "check"}))
            await pilot.pause()
            card = app.query_one(ToolCard)
            title = card.query_one("CollapsibleTitle")
            await pilot.click(title)
            await pilot.pause()
            conversation.anchor()
            await pilot.pause()
            assert app.focused is title
            assert title.region.y >= conversation.content_region.y
            await app._event(ToolOutput("growing", "shell", "progress\n" * 100))
            await pilot.pause()
            async with asyncio.timeout(5):
                await app.animator.wait_until_complete()
            assert title.region.y >= conversation.content_region.y
            assert title.region.bottom <= conversation.content_region.bottom
            assert app.focused is title
            assert not card.collapsed

            # Manually scroll into the output, keeping its header focused but
            # deliberately offscreen. More output must not pull it back.
            conversation.scroll_relative(y=30, animate=False)
            await pilot.pause()
            assert title.region.bottom < conversation.content_region.y
            before = conversation.scroll_y
            await app._event(ToolOutput("growing", "shell", "more progress\n" * 100))
            await pilot.pause()
            assert conversation.scroll_y == before
            assert app.focused is title

            # Reading older scrollback, also without changing keyboard focus.
            conversation.scroll_home(animate=False)
            await pilot.pause()
            assert title.region.y > conversation.content_region.bottom
            await app._event(
                ToolResultEvent(id="growing", name="shell", error="failure", result={"diff": "+new\n" * 100})
            )
            await pilot.pause()
            assert conversation.scroll_y == 0
            assert app.focused is title
            assert not card.collapsed

            # Returning to the bottom resumes following without needing a new
            # card or assistant turn to reset the anchor.
            conversation.scroll_end(animate=False)
            await pilot.pause()
            assert title.region.bottom < conversation.content_region.y
            await app._event(ToolOutput("growing", "shell", "late progress\n" * 100))
            await pilot.pause()
            assert conversation.scroll_y == conversation.max_scroll_y

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
@pytest.mark.parametrize(
    "origin",
    [
        "composer",
        "conversation",
        "header",
        "removed",
        "direct-display",
        "ancestor-display",
        "direct-hidden",
        "ancestor-hidden",
    ],
)
def test_approval_restores_prior_focus_without_scrolling(tmp_path: Path, size: tuple[int, int], origin: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [Message(role="assistant", content="Earlier context.\n\n" * 40)]
        app = make_app(backend)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            await app._event(ToolCallEvent(id="inspect", name="read_file", arguments={"path": "fixture"}))
            await pilot.pause()
            composer = app.query_one(Composer)
            composer.load_text("exact unsent draft\nsecond line")
            conversation = app.query_one("#conversation", VerticalScroll)
            card = app.query_one(ToolCard)
            target = (
                composer
                if origin == "composer"
                else conversation
                if origin == "conversation"
                else card.query_one("CollapsibleTitle")
            )
            target.focus()
            await pilot.pause()
            # Even a deliberately offscreen focus target must not jump the
            # transcript on return from an approval.
            conversation.scroll_home(animate=False)
            await pilot.pause()
            approval = asyncio.create_task(
                app.request_approval(ApprovalRequest("focus", "edit_file", "Exact", {"path": "fixture"}, "+new"))
            )
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ApprovalModal)
            deny = modal.query_one("#deny")
            async with asyncio.timeout(HANG_GUARD):
                while app.focused is not deny:
                    await pilot.pause()
            if origin == "removed":
                await card.remove()
            elif origin == "direct-display":
                target.display = False
            elif origin == "ancestor-display":
                card.display = False
            elif origin == "direct-hidden":
                target.visible = False
            elif origin == "ancestor-hidden":
                card.visible = False
            await pilot.press("escape")
            assert not await approval
            await pilot.pause()
            expected = target if origin in {"composer", "conversation", "header"} else composer
            assert app.screen.focused is expected
            assert conversation.scroll_y == 0
            assert composer.text == "exact unsent draft\nsecond line"

    asyncio.run(scenario())
