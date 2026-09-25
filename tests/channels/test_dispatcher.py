"""Detached dispatch and owned tools through real Harness execution, without listeners."""

import asyncio
import json
from io import BytesIO
from pathlib import Path

import pytest

from nagents.channels import dispatcher as dispatch_module
from nagents.channels.dispatcher import ChannelDispatcher
from nagents.channels.runtime import ChannelRuntime
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelValue
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import ApprovalRequest
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.tools import ToolRegistry
from tests.channels.test_channels_runtime import MemoryChannel
from tests.channels.test_channels_runtime import OfflineProvider
from tests.channels.test_channels_runtime import make_agent


def test_detached_catalog_schema_and_identity_membership() -> None:
    async def drive() -> None:
        channel = MemoryChannel("source")
        dispatcher = ChannelDispatcher((channel,))
        expected = dispatcher.catalog()
        schema = dispatcher.action_schema("source", "edit")
        channel.actions[0].parameters["required"] = []
        channel.name = "renamed"
        channel.description = "changed"
        dispatcher.catalog()[0].clear()
        schema.clear()
        assert await dispatcher.channel_list() == expected
        assert dispatcher.action_schema("source", "edit")["required"] == ["id", "text"]
        assert dispatcher.contains_channel("source", channel)
        assert not dispatcher.contains_channel("renamed", channel)
        assert not dispatcher.contains_channel("source", MemoryChannel("source"))
        arguments: dict[str, ChannelValue] = {"id": "remote", "text": "edit"}
        await dispatcher.channel_action("source", "edit", arguments)
        assert arguments == {"id": "remote", "text": "edit"}
        with pytest.raises(ChannelError, match="Unknown channel action"):
            dispatcher.action_schema("source", "missing")
        assert channel.opened == channel.closed == 0
        assert not channel.listening.is_set()

    asyncio.run(drive())


def test_empty_catalog_and_duplicate_names() -> None:
    async def drive() -> None:
        dispatcher = ChannelDispatcher()
        assert dispatcher.catalog() == await dispatcher.channel_list() == []
        with pytest.raises(ChannelError) as caught:
            await dispatcher.channel_send("missing", "room", "text")
        assert json.loads(str(caught.value)) == {"error": "Unknown channel", "retry_after": 0, "outcome_unknown": False}
        with pytest.raises(ChannelError, match="unique"):
            ChannelDispatcher((MemoryChannel("same"), MemoryChannel("same")))

    asyncio.run(drive())


@pytest.mark.parametrize("operation", ["send", "action"])
def test_dispatch_failure_is_sanitized_and_attempted_once(operation: str) -> None:
    async def drive() -> None:
        channel = MemoryChannel("source")
        channel.failure_stage = operation
        dispatcher = ChannelDispatcher((channel,))
        with pytest.raises(ChannelError) as caught:
            if operation == "send":
                await dispatcher.channel_send("source", "room", "text")
            else:
                await dispatcher.channel_action("source", "edit", {"id": "remote", "text": "edit"})
        assert json.loads(str(caught.value)) == {
            "error": f"Channel {operation} failed",
            "retry_after": 0,
            "outcome_unknown": True,
        }
        assert len(channel.sent) + len(channel.operations) == 1

    asyncio.run(drive())


def test_registration_collision_is_atomic_and_cleanup_preserves_replacements() -> None:
    dispatcher = ChannelDispatcher()
    registry = ToolRegistry()
    previous = registry.register(dispatcher.channel_list, name="channel_action")
    with pytest.raises(ChannelError, match="collision"), dispatcher.register_tools(registry):
        pytest.fail("collision must fail before entering")
    assert registry.get_all() == [previous]
    registry.unregister("channel_action")
    replacement = previous
    with pytest.raises(RuntimeError, match="owner failed"), dispatcher.register_tools(registry) as tools:
        assert {tool.name for tool in tools} == {"channel_list", "channel_send", "channel_action"}
        replacement = registry.register(dispatcher.channel_list, name="channel_send")
        raise RuntimeError("owner failed")
    assert registry.get_all() == [replacement]


def test_partial_registration_failure_removes_only_owned_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ToolRegistry()
    dispatcher = ChannelDispatcher()

    def broken() -> None:
        raise RuntimeError("schema extraction failed")

    monkeypatch.setattr(dispatcher, "channel_send", broken)
    monkeypatch.setattr(registry, "_extract_parameters", lambda func: broken() if func is broken else {})
    with pytest.raises(RuntimeError, match="schema extraction"), dispatcher.register_tools(registry):
        pytest.fail("registration must fail before entering")
    assert registry.get_all() == []


def test_runtime_guards_and_exact_tool_schemas(tmp_path: Path) -> None:
    async def drive() -> None:
        agent = make_agent(tmp_path / "runtime.db", OfflineProvider())
        channel = MemoryChannel("source")
        runtime = ChannelRuntime(agent, (channel,), "identity")
        registry = ToolRegistry()
        try:
            with runtime.dispatcher.register_tools(registry):
                for name in ("channel_list", "channel_send", "channel_action"):
                    definition = registry.get(name)
                    assert definition is not None
                    if name != "channel_action":
                        other = ToolRegistry().register(getattr(runtime, "_" + name), name=name)
                        assert definition.parameters == other.parameters
                        assert definition.description == other.description
                for call in (
                    runtime._channel_list(),
                    runtime._channel_send("source", "room", "text"),
                    runtime._channel_action("source", "edit", {"id": "remote", "text": "edit"}),
                ):
                    with pytest.raises(ChannelError, match="only active inside listen"):
                        await call
            assert channel.sent == []
            assert channel.operations == []
        finally:
            await agent.close()

    asyncio.run(drive())


@pytest.mark.requires_posix
def test_owned_registration_runs_real_harness_tools_without_listener(tmp_path: Path) -> None:
    async def drive() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", auth="api-key"))
        channel = MemoryChannel("source")
        dispatcher = ChannelDispatcher((channel,), workspace=tmp_path)
        approvals: list[str] = []

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request.tool)
            return True

        harness.approval_handler = approve
        harness.agent.provider = OfflineProvider(
            (
                (ToolCallEvent(id="list", name="channel_list", arguments={}),),
                (
                    ToolCallEvent(
                        id="send",
                        name="channel_send",
                        arguments={"channel": "source", "destination": "room", "text": "hello"},
                    ),
                ),
                (
                    ToolCallEvent(
                        id="action",
                        name="channel_action",
                        arguments={
                            "channel": "source",
                            "action": "edit",
                            "arguments": {"id": "remote-1", "text": "edited"},
                        },
                    ),
                ),
            )
        )
        registry = harness.agent.tool_registry
        try:
            with dispatcher.register_tools(registry):
                events = [event async for event in harness.run("Use the configured channel tools")]
                results = [event for event in events if isinstance(event, ToolResultEvent)]
                assert len(results) == 3
                assert all(event.error is None for event in results)
                assert approvals == ["channel_list", "channel_send", "channel_action"]
                assert channel.sent == [ChannelSend("room", "hello")]
                assert channel.operations == [("edit", {"id": "remote-1", "text": "edited"})]
            assert all(registry.get(name) is None for name in approvals)
            assert channel.opened == channel.closed == 0
            assert not channel.listening.is_set()
        finally:
            await harness.close()

    asyncio.run(drive())


@pytest.mark.parametrize("per_file,total,expected_reads", [(4, 10, [5]), (6, 8, [7, 3])])
def test_growing_files_are_bounded_by_actual_bytes_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, per_file: int, total: int, expected_reads: list[int]
) -> None:
    for name in ("first.txt", "second.txt"):
        (tmp_path / name).write_bytes(b"x")
    reads: list[int] = []

    class GrowingFile(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None
            reads.append(size)
            return super().read(size)

    def opened(path: Path, mode: str) -> GrowingFile:
        assert mode == "rb"
        return GrowingFile(b"grown!")

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(dispatch_module, "MAX_OUTBOUND_FILE_BYTES", per_file)
    monkeypatch.setattr(dispatch_module, "MAX_OUTBOUND_TOTAL_BYTES", total)

    async def drive() -> None:
        channel = MemoryChannel("source")
        dispatcher = ChannelDispatcher((channel,), workspace=tmp_path)
        with pytest.raises(ChannelError) as caught:
            await dispatcher.channel_send("source", "room", "text", attachments=["first.txt", "second.txt"])
        assert not caught.value.outcome_unknown and caught.value.retry_after == 0
        assert reads == expected_reads
        assert channel.sent == []

    asyncio.run(drive())
