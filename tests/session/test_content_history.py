"""Ordinary JSON replies must not masquerade as serialized rich content."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.session import SessionManager
from nagents.types import AudioContent
from nagents.types import DocumentContent
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.types import ContentPart


@pytest.mark.parametrize(
    "content",
    [
        '["mcp__github__get_file_contents", "mcp__github__list_branches"]',
        "[]",
        '[1, true, null, ["nested"]]',
        '[{"name": "example"}]',
        '[{"text": "missing discriminator"}]',
        '[{"type": "unknown", "text": "ordinary data"}]',
        '[{"type": "text", "text": 2}]',
        '[{"type": "text", "text": "valid"}, "ordinary data"]',
        '[{"type": "text", "text": "valid", "extra": "preserve me"}]',
        '[{"type": "image", "base64_data": 42}]',
        '[{"type": "document", "base64_data": "YQ==", "title": []}]',
        '[{"type": "audio"}]',
        '["unfinished',
        "[A Markdown link](https://example.test)",
        "[" * 1500 + "0" + "]" * 1500,
    ],
    ids=[
        "mcp-names",
        "empty-array",
        "json-values",
        "objects",
        "missing-type",
        "unknown-type",
        "invalid-text",
        "mixed-array",
        "extra-fields",
        "invalid-image",
        "invalid-title",
        "missing-audio",
        "malformed-json",
        "markdown",
        "deep-json",
    ],
)
def test_json_replies_survive_history_reload_and_compaction(tmp_path: Path, content: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "sessions.db"
        manager = SessionManager(path)
        await manager.get_or_create_session("root", "user")
        await manager.add_message("root", Message(role="assistant", content=content))
        restored = await SessionManager(path).get_history("root")
        assert restored[0].content == content
        assert isinstance(restored[0].content, str)

        await manager.replace_context("root", [Message(role="assistant", content=content)])
        compacted = await SessionManager(path).get_history("root")
        assert len(compacted) == 1 and compacted[0].content == content

    asyncio.run(scenario())


def test_serialized_rich_content_still_round_trips(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = SessionManager(tmp_path / "sessions.db")
        await manager.get_or_create_session("root", "user")
        parts: list[ContentPart] = [
            TextContent("Describe these attachments"),
            ImageContent("aW1hZ2U=", "image/png", detail="low"),
            AudioContent("YXVkaW8=", format="mp3"),
            DocumentContent("ZG9jdW1lbnQ=", title="Example"),
        ]
        await manager.add_message("root", Message(role="user", content=parts))
        assert (await SessionManager(manager.db_path).get_history("root"))[0].content == parts

        # Older records can omit fields that had defaults in the serializer.
        legacy = json.dumps(
            [
                {"type": "image", "base64_data": "aW1hZ2U="},
                {"type": "audio", "base64_data": "YXVkaW8="},
                {"type": "document", "base64_data": "ZG9jdW1lbnQ="},
            ]
        )
        await manager.add_message("root", Message(role="user", content=legacy))
        assert (await manager.get_history("root"))[-1].content == [
            ImageContent("aW1hZ2U=", "image/jpeg"),
            AudioContent("YXVkaW8="),
            DocumentContent("ZG9jdW1lbnQ="),
        ]

    asyncio.run(scenario())
