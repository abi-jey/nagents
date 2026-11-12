"""Real tool exceptions must never draw outside Textual's compositor."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import TYPE_CHECKING

import pytest

from nagents.events import ToolCallEvent
from nagents.tools import ToolExecutor
from nagents.tools import ToolRegistry
from nagents.tui.diagnostics import RuntimeLog
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolCard
from nagents.types import ToolCall
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("size", [(48, 18), (80, 24), (132, 38)])
@pytest.mark.parametrize("synchronous", [False, True])
def test_tool_exception_keeps_terminal_draft_and_footer_intact(
    tmp_path: Path, size: tuple[int, int], synchronous: bool
) -> None:
    async def scenario() -> None:
        # A pre-existing StreamHandler holds the original stderr even after
        # Textual redirects sys.stderr. Geometry/SVG tests alone miss this leak.
        terminal = io.StringIO()
        console = logging.StreamHandler(terminal)
        console.setLevel(logging.ERROR)
        root = logging.getLogger()
        root.addHandler(console)
        registry = ToolRegistry()

        def fail(path: str) -> str:
            """Fail while reading a nonexistent file."""
            raise FileNotFoundError(path)

        async def fail_async(path: str) -> str:
            return fail(path)

        registry.register(fail if synchronous else fail_async, name="read_file")
        executor = ToolExecutor(registry)
        path = "PRIVATE-PATH/" + "a-very-long-directory/" * 12 + "missing-file.txt"
        app = make_app(FakeHarness(tmp_path))
        try:
            with RuntimeLog(tmp_path) as runtime:
                async with app.run_test(size=size) as pilot:
                    await idle(app, pilot)
                    composer = app.query_one(Composer)
                    composer.load_text("Keep this wrapped draft while the tool fails. " * 3)
                    composer.focus()
                    await pilot.pause()
                    draft = composer.text
                    position = composer.cursor_location
                    layout = composer.region, app.query_one("#footer").region
                    await app._event(ToolCallEvent(id="failure", name="read_file", arguments={"path": path}))
                    result = await executor.execute(ToolCall("failure", "read_file", {"path": path}))
                    await app._event(result)
                    await pilot.pause()
                    card = app.query_one(ToolCard)
                    assert result.error == path and card.has_class("failed")
                    assert card.collapsed and card.region.height == 1
                    assert card.region.bottom <= composer.region.y
                    assert path in card.output_text
                    assert terminal.getvalue() == ""
                    assert "Traceback (most recent call last)" not in card.output_text
                    for expanded in (True, False):
                        card.collapsed = not expanded
                        await pilot.pause()
                        assert composer.text == draft and composer.cursor_location == position
                        assert (composer.region, app.query_one("#footer").region) == layout
                        assert app.query_one("#conversation").region.bottom <= composer.region.y
                        assert app.query_one("#footer").region.y >= composer.region.bottom
                        assert app.focused is composer
                    await pilot.resize_terminal(60, 20)
                    await pilot.pause()
                    assert composer.text == draft and app.focused is composer
                    assert app.query_one("#conversation").region.bottom <= composer.region.y
                    assert app.query_one("#footer").region.bottom <= 20
                    assert terminal.getvalue() == ""
            records = [json.loads(line) for line in runtime.path.read_text(encoding="utf-8").splitlines()]
            assert any(record.get("exception_type") == "FileNotFoundError" for record in records)
            assert path not in runtime.path.read_text(encoding="utf-8")
        finally:
            root.removeHandler(console)
            console.close()

    asyncio.run(scenario())
