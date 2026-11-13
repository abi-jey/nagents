"""Tool installation feedback is a compact activity row, not a prompt turn."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.color import Color
from textual.content import Content
from textual.widgets import Collapsible
from textual.widgets import Static

from nagents.events import DoneEvent
from nagents.harness.types import ToolAddedNotice
from nagents.tui.themes import THEME_NAMES
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolNotice
from nagents.tui.widgets import Turn
from nagents.types import Message
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("theme", THEME_NAMES)
@pytest.mark.parametrize("background", ["terminal", "theme"])
def test_saved_tool_added_message_is_one_unlabeled_row_and_not_composer_history(
    tmp_path: Path, theme: str, background: str
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.theme = theme
        backend.config.theme_background = background
        backend.messages = [
            Message(
                role="user",
                name="ngn_tools_added",
                content="Tool added: weather_forecast",
            ),
        ]
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            rows = list(app.query(ToolNotice))
            assert len(rows) == 1
            assert rows[0].title == "Tools added: 1"
            assert rows[0].collapsed
            assert isinstance(rows[0], ToolNotice)
            assert rows[0].region.height == 1
            assert rows[0].styles.color == Color.parse(app.get_css_variables()["ngn-tool"])
            assert rows[0].styles.background.a == 0
            assert rows[0].styles.border_left[0] in {"", "none"}
            assert rows[0].styles.margin.bottom == 0
            assert rows[0].tooltip == "Tool added: weather_forecast"
            assert not app.query(Turn)
            assert app.query_one(Composer).prompt_history == []

    asyncio.run(scenario())


def test_live_tool_added_notice_is_not_a_user_turn_or_sent_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.events = [
            ToolAddedNotice(text="Tool added: calendar_lookup", history_id="tool-added-9"),
            DoneEvent(),
        ]
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "continue")
            await idle(app, pilot)
            rows = list(app.query(ToolNotice))
            assert len(rows) == 1
            assert rows[0].title == "Tools added: 1"
            assert rows[0].collapsed
            assert backend.prompts == ["continue"]
            assert len(app.query(Turn)) == 1
            assert app.query_one(Composer).prompt_history == ["continue"]
            assert isinstance(rows[0], ToolNotice)

    asyncio.run(scenario())


def test_tool_notice_groups_expand_independently(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [
            Message(
                role="user",
                name="ngn_tools_added",
                content=(
                    "Tools added: weather_forecast, mcp__chrome-devtools__click, "
                    "mcp__chrome-devtools__fill, mcp__docs__search"
                ),
            ),
        ]
        app = make_app(backend)
        async with app.run_test(size=(80, 30)) as pilot:
            await idle(app, pilot)
            notice = app.query_one(ToolNotice)
            assert notice.title == "Tools added: 4"
            assert notice.region.height == 1
            await pilot.click(notice.query_one("CollapsibleTitle"))
            await pilot.pause()
            assert not notice.collapsed
            groups = list(notice.query(".tool-source").results(Collapsible))
            assert [group.title for group in groups] == [
                "MCP server chrome-devtools added: 2",
                "MCP server docs added: 1",
                "Custom tools: 1",
            ]
            assert all(group.collapsed for group in groups)
            await pilot.click(groups[0].query_one("CollapsibleTitle"))
            await pilot.pause()
            assert not groups[0].collapsed
            assert not notice.collapsed
            assert groups[1].collapsed
            assert str(groups[0].query_one("Contents > Static", Static).content) == "click\nfill"
            await pilot.click(notice.query_one("CollapsibleTitle"))
            await pilot.pause()
            assert notice.collapsed
            assert notice.region.height == 1

    asyncio.run(scenario())


def test_live_initial_notice_renders_before_first_user_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            await app._event(ToolAddedNotice(text="Tool added: initial_tool"))
            await pilot.pause()
            assert not app.query(Turn)
            assert app.query_one(Composer).prompt_history == []
            assert backend.prompts == []
            backend.events = [DoneEvent()]
            await send(app, pilot, "hello")
            await idle(app, pilot)
            rows = [child for child in app.query_one("#conversation").children if isinstance(child, (ToolNotice, Turn))]
            assert [type(row) for row in rows] == [ToolNotice, Turn]
            assert app.query_one(Composer).prompt_history == ["hello"]

    asyncio.run(scenario())


def test_restored_notices_keep_order_without_reclassifying_user_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        literal = "Tool added: [bold]literal_tool[/bold]"
        long_notice = "Tool added: " + "long_tool_name_" * 20
        backend.messages = [
            Message(role="user", name="ngn_tools_added", content=literal),
            Message(role="user", name="ngn_tools_added", content=long_notice),
            Message(role="user", content="Tool added: this is a human prompt"),
            Message(role="assistant", content="Ready."),
            Message(role="user", name="ngn_tools_added", content="Tool added: later_tool"),
        ]
        app = make_app(backend)
        async with app.run_test(size=(60, 24)) as pilot:
            await idle(app, pilot)
            children = list(app.query_one("#conversation").children)
            rows = [child for child in children if isinstance(child, (ToolNotice, Turn))]
            assert [type(row) for row in rows] == [ToolNotice, ToolNotice, Turn, Turn, ToolNotice]
            notices = list(app.query(ToolNotice))
            assert notices[0].title == "Tools added"
            body = notices[0].query_one("Contents > Static", Static)
            assert str(body.content) == literal
            rendered = body.render()
            assert isinstance(rendered, Content)
            assert rendered.plain == literal
            assert notices[1].region.height == 1
            assert notices[1].region.right <= 60
            assert notices[1].tooltip == long_notice
            assert app.query_one(Composer).prompt_history == ["Tool added: this is a human prompt"]
            assert backend.prompts == []

    asyncio.run(scenario())
