"""Native tool images reach the next model call, not terminal output."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents import Agent
from nagents import SessionManager
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.media import MediaCapabilities
from nagents.tools.output import ToolOutput
from nagents.types import ImageContent
from tests.agent.test_agent_reliability import ScriptedProvider

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("vision", [True, False])
def test_tool_images_round_trip(tmp_path: Path, streaming: bool, vision: bool) -> None:
    async def drive() -> None:
        image = ImageContent(base64_data="c2VjcmV0LWltYWdlLWJ5dGVz", media_type="image/png")

        def capture() -> ToolOutput:
            """Return an image."""
            return ToolOutput(text="Captured", images=(image,))

        class ImageProvider(ScriptedProvider):
            @property
            def supported_media_formats(self) -> MediaCapabilities:
                return MediaCapabilities(image_formats=frozenset({"png"}) if vision else frozenset())

        provider = ImageProvider(
            [
                [ToolCallEvent(id="one", name="capture"), ToolCallEvent(id="two", name="capture")],
                [TextDoneEvent(text="done")],
            ]
        )
        agent = Agent(
            provider,
            SessionManager(tmp_path / "images.db"),
            tools=[capture],
            streaming=streaming,
            compaction_strategy=None,
        )
        try:
            events = [event async for event in agent.run("inspect", session_id="images")]
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert len(results) == 2
            for result in results:
                assert isinstance(result.result, str)
                assert "Image" in result.result
                assert image.base64_data not in result.result
                assert "file:///" in result.result
                assert len(list((tmp_path / "tool-images").glob("*.png"))) == 2
                if not vision:
                    assert "unsupported" in result.result
            request = provider.requests[1]
            tools = [index for index, message in enumerate(request) if message.role == "tool"]
            assert len(tools) == 2
            image_messages = [
                message
                for message in request
                if isinstance(message.content, list) and any(isinstance(part, ImageContent) for part in message.content)
            ]
            assert len(image_messages) == int(vision)
            if vision:
                assert request.index(image_messages[0]) > max(tools)
                assert image_messages[0].role == "user"
                assert isinstance(image_messages[0].content, list)
                assert [part for part in image_messages[0].content if isinstance(part, ImageContent)] == [image, image]
            stored = await agent.session.get_history("images")
            assert stored == [*request, stored[-1]]
        finally:
            await agent.close()

    asyncio.run(drive())
