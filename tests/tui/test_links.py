"""Markdown links open only through an explicit, validated click."""

from __future__ import annotations

import ast
import asyncio
from typing import TYPE_CHECKING

from textual.content import Content
from textual.style import Style
from textual.widgets import Markdown
from textual.widgets import Static

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.tui.widgets import ConversationMarkdown
from nagents.types import Message
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from textual.pilot import Pilot


async def _click_rendered_link(markdown: ConversationMarkdown, pilot: Pilot[None], href: str) -> None:
    for block in markdown.query("MarkdownBlock").results(Static):
        content = block.content
        if not isinstance(content, Content):
            continue
        for span in content.spans:
            if not isinstance(span.style, Style):
                continue
            action = span.style.meta.get("@click")
            if not isinstance(action, str) or not action.startswith("link("):
                continue
            try:
                target = ast.literal_eval(action[len("link(") : -1])
            except (SyntaxError, ValueError):
                continue
            if target == href:
                x = span.start + max(0, (span.end - span.start) // 2)
                assert await pilot.click(block, offset=(x, 0))
                await pilot.pause()
                return
    raise AssertionError(f"No rendered link for {href!r}")


def test_link_clicks_open_http_and_bare_urls_but_streaming_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [
            Message(role="assistant", content="Restored [link](https://restored.example/path)."),
        ]
        encoded = "https://docs.example/search?q=a%26b%3Dc"
        streamed = f"[docs](https://docs.example/path) and https://bare.example/path and [search]({encoded})"
        backend.events = [TextChunkEvent(chunk=streamed), TextDoneEvent(text=streamed), DoneEvent()]
        app = make_app(backend)
        opened: list[str] = []
        monkeypatch.setattr(app, "open_url", opened.append)

        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            assert opened == []
            await send(app, pilot, "show references")
            await idle(app, pilot)
            assert opened == []
            markdowns = list(app.query(ConversationMarkdown))
            restored, response = markdowns[-2:]
            await _click_rendered_link(response, pilot, "https://docs.example/path")
            assert opened == ["https://docs.example/path"]
            await _click_rendered_link(response, pilot, "https://bare.example/path")
            assert opened == ["https://docs.example/path", "https://bare.example/path"]
            await _click_rendered_link(response, pilot, encoded)
            assert opened == ["https://docs.example/path", "https://bare.example/path", encoded]
            assert restored.source is not None

            for href in (
                "javascript:alert(1)",
                "file:///etc/passwd",
                "mailto:help@example.test",
                "https://safe.example/line\nfeed",
                "https://safe.example/\x7f",
            ):
                response.post_message(Markdown.LinkClicked(response, href))
                await pilot.pause()
            assert opened == ["https://docs.example/path", "https://bare.example/path", encoded]

    asyncio.run(scenario())
