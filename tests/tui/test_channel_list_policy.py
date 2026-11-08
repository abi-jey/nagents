"""Host discovery bypasses approval, not policy or definition identity checks."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.channels.dispatcher import ChannelDispatcher
from nagents.channels.local_delivery import discard_notification
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.tui.channel import TuiChannelHost
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.types import ApprovalRequest
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("case", ["original", "disabled", "replacement", "dispatcher", "mutated", "schema"])
def test_channel_list_host_identity_and_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="list", name="channel_list", arguments={})
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        host = TuiChannelHost(harness, discard_notification)
        host.open()
        approvals: list[str] = []
        invoked: list[str] = []

        async def deny(request: ApprovalRequest) -> bool:
            approvals.append(request.tool)
            return False

        async def spoof() -> str:
            invoked.append("spoof")
            return "spoof"

        harness.approval_handler = deny
        registry = harness.agent.tool_registry
        original = registry.get("channel_list")
        assert original is not None
        try:
            if case == "disabled":
                settings = tmp_path / ".ngn" / "tools.yaml"
                settings.parent.mkdir(exist_ok=True)
                settings.write_text("version: 1\nagents: {assistant: {channel_list: false}}\n")
            elif case in {"replacement", "dispatcher"}:
                registry.unregister("channel_list")
                function = ChannelDispatcher((host.channel,)).channel_list if case == "dispatcher" else spoof
                registry.register(function, name="channel_list")
            elif case == "mutated":
                original.func = spoof
            elif case == "schema":
                original.parameters = {"type": "object", "properties": {}, "additionalProperties": False}
            events = await collect(harness)
            result = next(event for event in events if isinstance(event, ToolResultEvent) and event.id == "list")
            if case == "original":
                assert not result.error
                assert "builtin.tui" in str(result.result)
                assert approvals == []
            elif case == "disabled":
                assert result.error and "disabled" in result.error
                assert approvals == []
            else:
                assert result.error
                assert approvals == ["channel_list"]
            assert invoked == []
        finally:
            host.close()
            assert harness._channel_list_definition is None
            assert harness._channel_list_binding is None
            await harness.close()

    asyncio.run(scenario())
