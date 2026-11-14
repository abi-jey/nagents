"""Native web hyperlinks preserve exact targets without browser side effects."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from rich.console import Console
from textual.content import Content
from textual.style import Style
from textual.visual import Visual
from textual.widgets import Markdown
from textual.widgets import Static

from nagents.events import DoneEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.tui.widgets import ConversationMarkdown
from nagents.tui.widgets import _terminal_link
from nagents.types import Message
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from pathlib import Path


def test_native_links_survive_streaming_history_and_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.messages = [Message(role="assistant", content="Restored [link](https://restored.example/path).")]
        encoded = "https://docs.example/search?q=a%26b%3Dc"
        streamed = (
            f"[**docs**](https://docs.example/path) and https://bare.example/path and [search]({encoded})\n\n"
            "| Reference |\n| --- |\n| [table link](https://table.example/path) |\n\n"
            "- [list link](https://list.example/path)\n\n"
            "# [Heading](https://heading.example/path)\n\n"
            "![Image](https://image.example/path)\n\n"
            "[local](file:///etc/passwd) [email](mailto:help@example.test)\n\n"
            "`https://code.example/path`"
        )
        backend.events = [TextChunkEvent(chunk=streamed), TextDoneEvent(text=streamed), DoneEvent()]
        app = make_app(backend)
        opened: list[str] = []
        monkeypatch.setattr(app, "open_url", opened.append)
        async with app.run_test(size=(120, 70)) as pilot:
            await idle(app, pilot)
            await send(app, pilot, "show references")
            await idle(app, pilot)
            await pilot.pause()
            targets: set[str] = set()
            output = ""
            for widget in app.query(Static):
                content = widget.content
                if isinstance(content, Content):
                    for span in content.spans:
                        if isinstance(span.style, Style) and span.style.link:
                            targets.add(span.style.link)
                            assert "@click" not in span.style.meta
                            assert span.style.underline
                            assert span.style.foreground is not None
                    for strip in Visual.to_strips(widget, content, 200, None, Style()):
                        output += strip.render(Console(force_terminal=True))
            expected = {
                "https://restored.example/path",
                "https://docs.example/path",
                "https://bare.example/path",
                encoded,
                "https://table.example/path",
                "https://list.example/path",
                "https://heading.example/path",
                "https://image.example/path",
            }
            assert targets == expected
            for href in expected:
                assert f";{href}\x1b\\" in output, f"No actual OSC 8 output for {href}"
            assert "\x1b]8;" in output
            for markdown in app.query(ConversationMarkdown):
                markdown.post_message(Markdown.LinkClicked(markdown, encoded))
            await pilot.pause()
            assert opened == [], "Streaming, restored content and app clicks must not launch a browser"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "mailto:help@example.test",
        "https:///missing-host",
        "https://safe.example/\n",
        "https://safe.example/%1B]8;;bad",
        "https://safe.example/%7f",
        "https://safe.example/\x9d",
        "https://safe.example/%C2%9C",
        "https://safe.example/has space",
    ],
)
def test_native_link_rejects_unsafe_targets(href: str) -> None:
    assert _terminal_link(href) == ""


def test_native_link_preserves_encoded_spaces_and_query_delimiters() -> None:
    href = "https://example.com/a%20b?q=a%26b%3Dc"
    assert _terminal_link(href) == href


def test_screen_compositor_emits_native_links_with_inline_formatting() -> None:
    from textual._compositor import CompositorUpdate
    from textual.app import App
    from textual.app import ComposeResult

    href = "https://example.com/a%20b?q=x%26y"

    class LinkApp(App[None]):
        def compose(self) -> ComposeResult:
            yield ConversationMarkdown(f"[**bold** and *italic* and `code`]({href})", open_links=False)

    async def scenario() -> None:
        app = LinkApp()
        async with app.run_test(size=(100, 15)) as pilot:
            await pilot.pause()
            update = app.screen._compositor.render_update(full=True)
            assert isinstance(update, CompositorUpdate)
            output = update.render_segments(Console(force_terminal=True))
            assert f";{href}\x1b\\" in output
            paragraph = app.query_one("MarkdownParagraph", Static)
            strips = paragraph.render_lines(paragraph.size.region)
            styles = [segment.style for strip in strips for segment in strip if segment.style and segment.style.link]
            assert any(style.bold for style in styles)
            assert any(style.italic for style in styles)
            assert all(style.underline for style in styles)

    asyncio.run(scenario())
