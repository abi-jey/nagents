"""Submitting a new prompt returns from inspected scrollback to live output."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll

from nagents.types import Message
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_new_submission_resumes_bottom_follow(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [Message(role="assistant", content="Earlier screenshot description.\n\n" * 70)]
        app = make_app(backend)
        async with app.run_test(size=(90, 28)) as pilot:
            await idle(app, pilot)
            conversation = app.query_one("#conversation", VerticalScroll)
            conversation.release_anchor()
            conversation.scroll_home(animate=False)
            await pilot.pause()
            assert conversation.scroll_y == 0
            assert conversation.max_scroll_y > 10
            await send(app, pilot, "Inspect the next screenshot")
            await idle(app, pilot)
            assert conversation.max_scroll_y - conversation.scroll_y <= 1

    asyncio.run(scenario())
