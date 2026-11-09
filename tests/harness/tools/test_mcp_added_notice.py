"""MCP additions announce their names before the next model request, once."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.harness.types import ToolAddedNotice
from tests.harness.tools.test_resource_reload import server
from tests.support.providers import collect
from tests.support.providers import setup_harness

pytestmark = pytest.mark.usefixtures("workspace_discovery_only")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.asyncio
async def test_initial_mcp_notice_precedes_first_message_and_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert messages[-2].name == "ngn_tools_added"
        assert messages[-1].content == "Hello"
        yield DoneEvent()

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (server(tmp_path),)
    try:
        await harness.initialize()
        history = await harness.history()
        assert len(history) == 1
        assert history[0].name == "ngn_tools_added"
        assert history[0].content == "Tool added: mcp__fixture__echo"
        assert providers[0].requests == []
        events = await collect(harness, "Hello")
        assert not any(isinstance(event, ToolAddedNotice) for event in events)
        original = harness.session_id
        await harness.new_session()
        assert [message.name for message in await harness.history()] == ["ngn_tools_added"]
        await harness.resume(original)
        assert sum(message.name == "ngn_tools_added" for message in await harness.history()) == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_mcp_hot_addition_is_announced_before_generation_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield DoneEvent()

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        await collect(harness, "initial turn")
        harness.config.mcp_servers = (server(tmp_path),)
        events = await collect(harness, "after adding MCP")
        assert any(isinstance(event, ToolAddedNotice) and "mcp__fixture__echo" in event.text for event in events)
        request = providers[0].requests[-1]
        assert any(message.content == "Tool added: mcp__fixture__echo" for message in request)
        assert "mcp__fixture__echo" in providers[0].schemas[-1]
        events = await collect(harness, "unchanged MCP")
        assert not any(isinstance(event, ToolAddedNotice) for event in events)
        assert sum(message.content == "Tool added: mcp__fixture__echo" for message in providers[0].requests[-1]) == 1
    finally:
        await harness.close()
