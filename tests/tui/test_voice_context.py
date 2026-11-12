"""Native startup context is bounded, read-only visible text, not model work."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.session import SessionManager
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.web.live.context import MAX_SEED_BYTES
from nagents.web.live.context import MAX_SOURCE_BYTES
from nagents.web.live.context import message_seed
from nagents.web.live.context import read_session_seed

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.live.context import ContextMode


@pytest.mark.parametrize("mode", ["none", "recent", "summary"])
def test_native_message_context_excludes_private_payloads(mode: ContextMode) -> None:
    messages = [
        Message(role="system", content="PRIVATE SYSTEM"),
        Message(role="compaction_summary", content="An existing summary"),
        Message(
            role="user",
            content=[
                TextContent(text="Visible request"),
                ImageContent(base64_data="PRIVATE IMAGE", media_type="image/png"),
            ],
        ),
        Message(role="tool", content="PRIVATE TOOL"),
        Message(role="assistant", content="Visible answer"),
    ]
    seed = message_seed(messages, "ngn-test", mode)
    wire = json.dumps(seed.history)
    assert "PRIVATE" not in wire and "PRIVATE" not in seed.summary_source
    if mode == "none":
        assert not seed.history and not seed.summary_source
    else:
        assert "Visible request" in wire and "Visible answer" in wire
        assert seed.summary_included and seed.omitted_content
        assert seed.mode == mode and seed.fingerprint


def test_native_context_bounds_unicode_source_and_preserves_omission_notice() -> None:
    messages = [Message(role="user", content="🎤" * 10000) for _ in range(256)]
    seed = message_seed(messages, "ngn-test", "summary")
    source = json.loads(seed.summary_source)
    assert source["partial"] and seed.omitted_content
    assert len(seed.summary_source.encode()) <= MAX_SOURCE_BYTES
    size = seed.report()["bytes"]
    assert isinstance(size, int) and size <= MAX_SEED_BYTES
    assert "�" not in json.dumps(seed.history, ensure_ascii=False)


def test_standard_session_context_reads_only_selected_text_without_web_tables(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = SessionManager(tmp_path / "history.db")
        root = await session.get_or_create_session("ngn-root", "harness")
        other = await session.get_or_create_session("ngn-other", "harness")
        await session.add_message(root, Message(role="user", content="PRIVATE OLD HISTORY"))
        summary_id = await session.add_message(root, Message(role="compaction_summary", content="Saved summary"))
        await session.set_compaction_boundary(root, summary_id)
        await session.add_message(
            root,
            Message(
                role="user",
                content=[
                    TextContent(text="Recent question"),
                    ImageContent(base64_data="PRIVATE IMAGE" * 10000, media_type="image/png"),
                ],
            ),
        )
        await session.add_message(root, Message(role="tool", content="PRIVATE TOOL"))
        await session.add_message(other, Message(role="user", content="PRIVATE OTHER SESSION"))
        count = await session.get_message_count(root)
        seed = await read_session_seed(session.db_path, root, "recent", task_state="The assistant is still working.")
        assert "Recent question" in str(seed.history) and "Saved summary" in str(seed.history)
        assert "still working" in str(seed.history)
        assert "PRIVATE" not in str(seed.history) and "PRIVATE" not in seed.summary_source
        assert seed.omitted_content and seed.summary_included
        assert await session.get_message_count(root) == count
        empty = await read_session_seed(session.db_path, root, "none")
        assert not empty.history
        with pytest.raises(ValueError, match="no longer available"):
            await read_session_seed(session.db_path, "ngn-missing", "recent")

    asyncio.run(scenario())
