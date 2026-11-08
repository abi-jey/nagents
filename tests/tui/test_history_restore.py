"""Restore saved transcripts without per-row layout or live tool effects."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import Mock

from textual.containers import VerticalScroll

from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import Turn
from nagents.types import Message
from nagents.types import ToolCall
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_history_is_mounted_once_without_replaying_tool_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            backend.messages = [Message(role="user", content="Saved question")]
            for index in range(40):
                backend.messages.extend(
                    [
                        Message(
                            role="assistant",
                            content=f"Saved answer {index}",
                            tool_calls=[ToolCall(id=f"call-{index}", name="shell", arguments={"command": "pwd"})],
                        ),
                        Message(
                            role="tool",
                            tool_call_id=f"call-{index}",
                            name="shell",
                            content="Error: failed" if index == 0 else "saved output",
                        ),
                    ]
                )
            # A session may end after a call was saved but before its result.
            backend.messages.append(
                Message(role="assistant", tool_calls=[ToolCall(id="unfinished", name="read_file", arguments={})])
            )
            conversation = app.query_one("#conversation", VerticalScroll)
            with monkeypatch.context() as patch:
                mount = Mock(wraps=conversation.mount)
                patch.setattr(conversation, "mount", mount)
                await app._load_history()
                assert mount.call_count == 1
            await pilot.pause()
            children = list(conversation.children)
            assert isinstance(children[-1], ToolCard)
            assert len(app.query(Turn)) == 41
            assert len(app.query(ToolCard)) == 41
            assert app.query_one(Composer).prompt_history == ["Saved question"]
            for index in range(40):
                card = app._tools[f"call-{index}"]
                assert children.index(card) == children.index(app.query(Turn)[index + 1]) + 1
                assert card._outcome == ("failed" if index == 0 else "complete")
                assert card.output_text == ("Error: failed" if index == 0 else "saved output")
                assert card.collapsed
                assert card._elapsed_timer is None
                assert card._completion_timer is None
            unfinished = app._tools["unfinished"]
            assert unfinished._outcome == "interrupted"
            assert not unfinished.has_class("running")
            assert unfinished._elapsed_timer is None
            assert conversation.scroll_y == conversation.max_scroll_y
            unfinished.collapsed = False
            await pilot.pause()
            assert unfinished.output_text == ""
            assert unfinished._elapsed_timer is None

            # The historical flag must not suppress effects for future calls.
            live = ToolCard("live", "shell", {"command": "pwd"})
            await conversation.mount(live)
            assert live._outcome == "running"
            assert live._elapsed_timer is not None
            live.finish("new output", None, 1)
            assert live._elapsed_timer is None

    asyncio.run(scenario())
