"""Terminal image output never dumps encoded bytes."""

import asyncio

from rich.console import Console
from textual.app import App

from nagents.tools.output import ToolOutput
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import format_value
from nagents.types import ImageContent


def test_tool_image_placeholder() -> None:
    output = ToolOutput(
        text="Screenshot", images=(ImageContent(base64_data="do-not-render-base64", media_type="image/png"),)
    )
    text = format_value(output)
    assert text.startswith("Screenshot")
    assert "Image" in text
    assert "image/png" in text
    assert "do-not-render-base64" not in text


def test_image_link_emits_native_terminal_osc8() -> None:
    async def scenario() -> None:
        uri = "file:///tmp/ngn/image-test.png"
        app: App[None] = App()
        async with app.run_test() as pilot:
            card = ToolCard("capture", "screenshot")
            await app.mount(card)
            card.collapsed = False
            card.append_output(f"[Image: image/png] {uri}")
            await pilot.pause()
            console = Console(force_terminal=True, color_system="truecolor")
            terminal_output = "".join(
                strip.render(console) for strip in card.output.render_lines(card.output.region.reset_offset)
            )
            assert "\x1b]8;" in terminal_output
            assert f";{uri}\x1b\\" in terminal_output
            assert "\x1b]8;;\x1b\\" in terminal_output

    asyncio.run(scenario())
