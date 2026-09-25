"""Delivery authority through real Agent persistence, approvals and workers."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.extensions import AgentPlugin
from nagents.harness.execution import ExecutionBridge
from nagents.harness.execution import bind_channel_send
from nagents.harness.execution import delivery_origin
from nagents.harness.execution import host_run
from nagents.harness.execution import revalidate_origin
from nagents.session import SessionManager
from nagents.types import Message
from nagents.types import ToolCall
from nagents.web.history import WebHistory
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.channels.delivery_types import DeliveryOrigin
    from nagents.events import Event
    from nagents.extensions import RunContext
    from nagents.harness.types import ApprovalRequest
    from tests.support.providers import FakeProvider


# Every scenario exercises real Harness skill/workspace guards, even when the
# provider requests only a custom channel tool.
pytestmark = pytest.mark.requires_posix


@pytest.mark.parametrize("web", [False, True])
def test_exact_committed_blocks_and_live_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, web: bool) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) % 3:
                for name in ("first", "second"):
                    yield ToolCallEvent(
                        id=name,
                        name="channel_send",
                        arguments={"channel": "web", "destination": harness.session_id, "text": name},
                    )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        if web:
            harness.agent.session = WebHistory(harness.agent.session.db_path, lambda run, record: None)
        origins: list[DeliveryOrigin] = []

        class Writes(AgentPlugin):
            async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
                with pytest.raises(PermissionError):
                    delivery_origin(harness, "web", harness.session_id)
                await harness.agent.session.add_message(context.session_id, Message(role="assistant", content="hook"))
                return call

            async def after_tool(self, context: RunContext, result: ToolResultEvent) -> ToolResultEvent:
                with pytest.raises(PermissionError):
                    delivery_origin(harness, "web", harness.session_id)
                return result

        harness.agent.plugins.append(Writes())

        async def approve(request: ApprovalRequest) -> bool:
            with pytest.raises(PermissionError):
                delivery_origin(harness, "web", harness.session_id)
            return True

        async def wrong() -> str:
            with pytest.raises(PermissionError):
                delivery_origin(harness, "web", harness.session_id)
            return "no authority"

        async def channel_send(channel: str, destination: str, text: str) -> str:
            origin = delivery_origin(harness, channel, destination)
            if origins:
                with pytest.raises(PermissionError):
                    revalidate_origin(harness, channel, destination, origins[-1])
            origins.append(origin)
            with pytest.raises(PermissionError):
                delivery_origin(harness, "tui", destination)
            with pytest.raises(PermissionError):
                delivery_origin(harness, channel, "another-session")

            async def spawned() -> None:
                with pytest.raises(PermissionError):
                    delivery_origin(harness, channel, destination)

            await asyncio.create_task(spawned())
            nested = await harness.agent.tool_executor.execute(ToolCall("nested", "wrong", {}))
            assert nested.error is None
            revalidate_origin(harness, channel, destination, origin)
            return text

        harness.approval_handler = approve
        harness.agent.register_tool(wrong)
        definition = harness.agent.register_tool(channel_send)
        try:
            with bind_channel_send(harness, definition), host_run(harness, "host-run"):
                for _ in range(2):
                    events = await collect(harness)
                    assert not [event for event in events if isinstance(event, ToolResultEvent) and event.error]
                    with pytest.raises(PermissionError):
                        revalidate_origin(harness, "web", harness.session_id, origins[-1])
            assert len(origins) == 8
            assert {origin.host_run_id for origin in origins} == {"host-run"}
            assert len({origin.turn_id for origin in origins}) == 2
            assert len({origin.invocation_id for origin in origins}) == 8
            assert [origin.call_position for origin in origins] == [0, 1] * 4
            assert len({origin.anchor_message_id for origin in origins}) == 4
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                for origin in origins:
                    cursor = await db.execute(
                        "SELECT role, tool_calls FROM v2_messages WHERE id = ?", (origin.anchor_message_id,)
                    )
                    row = await cursor.fetchone()
                    assert row is not None and row[0] == "assistant"
                    calls = json.loads(row[1])
                    assert calls[origin.call_position]["id"] == origin.call_id
            assert harness.agent._execution_bridge is None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_shared_web_history_is_task_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                owner = first if provider is first.agent.provider else second
                yield ToolCallEvent(
                    id="same", name="channel_send", arguments={"channel": "web", "destination": owner.session_id}
                )
            else:
                yield TextDoneEvent(text="done")

        first, _ = setup_harness(tmp_path, monkeypatch, script)
        second, _ = setup_harness(tmp_path, monkeypatch, script)
        history = WebHistory(first.agent.session.db_path, lambda run, record: None)
        first.agent.session = second.agent.session = history
        ready = asyncio.Event()
        origins: list[DeliveryOrigin] = []

        async def approve(request: ApprovalRequest) -> bool:
            return True

        async def run(index: int) -> None:
            harness = (first, second)[index]
            harness.approval_handler = approve

            async def channel_send(channel: str, destination: str) -> str:
                origin = delivery_origin(harness, channel, destination)
                origins.append(origin)
                if len(origins) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), HANG_GUARD)
                revalidate_origin(harness, channel, destination, origin)
                with pytest.raises(PermissionError):
                    delivery_origin((second, first)[index], channel, destination)
                return "ok"

            definition = harness.agent.register_tool(channel_send)
            with bind_channel_send(harness, definition), host_run(harness, f"host-{index}"):
                events = await collect(harness)
            assert not [event for event in events if isinstance(event, ToolResultEvent) and event.error]

        try:
            # Serialize schema creation; run producers concurrently with one adapter.
            await first.initialize()
            await second.initialize()
            await asyncio.wait_for(asyncio.gather(run(0), run(1)), HANG_GUARD)
            assert {origin.actor_session_id for origin in origins} == {first.session_id, second.session_id}
            assert {origin.host_run_id for origin in origins} == {"host-0", "host-1"}
            assert len({origin.anchor_message_id for origin in origins}) == 2
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())


def test_cancel_invalidates_copied_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield ToolCallEvent(
                id="call", name="channel_send", arguments={"channel": "web", "destination": harness.session_id}
            )

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        started, release = asyncio.Event(), asyncio.Event()
        delayed: list[asyncio.Task[None]] = []

        async def approve(request: ApprovalRequest) -> bool:
            return True

        async def channel_send(channel: str, destination: str) -> str:
            origin = delivery_origin(harness, channel, destination)

            async def later() -> None:
                await asyncio.wait_for(release.wait(), HANG_GUARD)
                with pytest.raises(PermissionError):
                    revalidate_origin(harness, channel, destination, origin)

            delayed.append(asyncio.create_task(later()))
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        harness.approval_handler = approve
        definition = harness.agent.register_tool(channel_send)
        try:
            with bind_channel_send(harness, definition):
                task = asyncio.create_task(collect(harness))
                await asyncio.wait_for(started.wait(), HANG_GUARD)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, HANG_GUARD)
                assert harness.agent._execution_bridge is None
                await harness.new_session()
                release.set()
                await asyncio.wait_for(asyncio.gather(*delayed), HANG_GUARD)
        finally:
            release.set()
            await harness.close()

    asyncio.run(scenario())


def test_host_run_survives_parent_synthesis_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        origins: list[DeliveryOrigin] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                yield TextDoneEvent(text="child complete")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate", name="delegate", arguments={"prompt": "inspect"})
                yield ToolCallEvent(
                    id="same", name="channel_send", arguments={"channel": "web", "destination": harness.session_id}
                )
            elif notifications(messages) and len(origins) == 1:
                yield ToolCallEvent(
                    id="same", name="channel_send", arguments={"channel": "web", "destination": harness.session_id}
                )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)

        async def approve(request: ApprovalRequest) -> bool:
            return True

        async def channel_send(channel: str, destination: str) -> str:
            origins.append(delivery_origin(harness, channel, destination))
            return "ok"

        harness.approval_handler = approve
        definition = harness.agent.register_tool(channel_send)
        try:
            with bind_channel_send(harness, definition), host_run(harness, "one-host-run"):
                events = await collect(harness)
            assert not [event for event in events if isinstance(event, ToolResultEvent) and event.error]
            assert len(origins) == 2
            assert origins[0].turn_id != origins[1].turn_id
            assert {origin.host_run_id for origin in origins} == {"one-host-run"}
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode",
    [
        "denied",
        "replacement",
        "approval_function",
        "unbound",
        "custom",
        "child",
        "child_custom",
        "wrong",
        "mutated",
        "schema",
        "profile",
        "destination",
    ],
)
def test_unauthorized_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="call",
                    name="wrong" if mode == "wrong" else "channel_send",
                    arguments={
                        "channel": 42 if mode == "schema" else "web",
                        "destination": "other" if mode == "destination" else harness.session_id,
                    },
                )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        reached: list[str] = []
        root = harness
        if mode in {"child", "child_custom"}:
            root.tasks.begin(root.session_id, reset_budget=True)
            harness = root.tasks._create_child("assistant")
            harness.supports_child_custom_tools = mode == "child_custom"

        async def channel_send(channel: str, destination: str) -> str:
            with pytest.raises(PermissionError):
                delivery_origin(harness, channel, destination)
            reached.append(channel)
            return "external fallback"

        async def wrong(channel: str, destination: str) -> str:
            return await channel_send(channel, destination)

        definition = harness.agent.register_tool(channel_send)
        harness.agent.register_tool(wrong)

        async def approve(request: ApprovalRequest) -> bool:
            if mode == "replacement":
                harness.agent.register_tool(wrong, name="channel_send")
            if mode == "approval_function":
                definition.func = wrong
            if mode == "profile":
                harness.config.agent = "reviewer"
            return mode != "denied"

        harness.approval_handler = approve
        root.approval_handler = approve
        if mode == "custom":
            harness.agent.session = SessionManager(harness.agent.session.db_path)
        try:
            with bind_channel_send(harness, definition):
                if mode == "unbound":
                    harness._delivery_definition = None
                if mode == "mutated":
                    definition.func = wrong
                events = await collect(harness)
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert len(results) == 1
            if mode in {"denied", "replacement", "approval_function", "child", "schema", "profile"}:
                assert results[0].error
                assert not reached
            else:
                assert results[0].error is None
                assert reached == ["web"]
        finally:
            await harness.close()
            if root is not harness:
                await root.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["channel", "destination", "function"])
def test_live_accessor_rechecks_executed_call_and_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="call",
                    name="channel_send",
                    arguments={"channel": "web", "destination": harness.session_id},
                )
            else:
                yield TextDoneEvent(text="done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        reached: list[str] = []

        async def approve(request: ApprovalRequest) -> bool:
            return True

        async def replacement(channel: str, destination: str) -> str:
            raise AssertionError("Replacement must never execute")

        async def channel_send(channel: str, destination: str) -> str:
            origin = delivery_origin(harness, channel, destination)
            bridge = harness.agent._execution_bridge
            assert isinstance(bridge, ExecutionBridge) and bridge.approved_call is not None
            if mutation == "function":
                definition.func = replacement
            else:
                # The executor's actual call is distinct from the Agent's call
                # and persisted block; both identities must remain consistent.
                assert bridge.approved_call is not bridge.call
                bridge.approved_call.arguments[mutation] = "other"
            with pytest.raises(PermissionError):
                delivery_origin(harness, channel, destination)
            with pytest.raises(PermissionError):
                revalidate_origin(harness, channel, destination, origin)
            reached.append(mutation)
            return "rejected"

        harness.approval_handler = approve
        definition = harness.agent.register_tool(channel_send)
        try:
            with bind_channel_send(harness, definition):
                events = await collect(harness)
            assert reached == [mutation]
            assert not [event for event in events if isinstance(event, ToolResultEvent) and event.error]
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "outcome", ["success", "tool_error", "tool_cancel", "approval_denied", "approval_error", "approval_cancel"]
)
def test_foreign_executor_masks_ambient_authority_and_restores_outer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="outer",
                    name="channel_send",
                    arguments={"channel": "web", "destination": first.session_id},
                )
            else:
                yield TextDoneEvent(text="done")

        first, _ = setup_harness(tmp_path, monkeypatch, script)
        second, foreign_providers = setup_harness(tmp_path, monkeypatch, script)
        stages: list[str] = []

        def reject_ambient(stage: str) -> None:
            assert asyncio.current_task() is first._worker
            assert second.agent._execution_bridge is None
            assert second.tools.call_id.get() == "foreign"
            with pytest.raises(PermissionError):
                delivery_origin(first, "web", first.session_id)
            with pytest.raises(PermissionError):
                delivery_origin(second, "web", second.session_id)
            stages.append(stage)

        async def approve_outer(request: ApprovalRequest) -> bool:
            return True

        async def approve_foreign(request: ApprovalRequest) -> bool:
            assert request.tool == "other_custom_tool"
            reject_ambient("approval")
            try:
                await asyncio.sleep(0)
                if outcome == "approval_cancel":
                    raise asyncio.CancelledError("approval cancelled")
                if outcome == "approval_error":
                    raise ValueError("approval failed")
                return outcome != "approval_denied"
            finally:
                reject_ambient("approval_cleanup")

        async def other_custom_tool() -> str:
            reject_ambient("tool")
            try:
                await asyncio.sleep(0)
                if outcome == "tool_cancel":
                    raise asyncio.CancelledError("tool cancelled")
                if outcome == "tool_error":
                    raise ValueError("tool failed")
                return "foreign execution succeeded"
            finally:
                reject_ambient("tool_cleanup")

        async def channel_send(channel: str, destination: str) -> str:
            origin = delivery_origin(first, channel, destination)
            call = ToolCall("foreign", "other_custom_tool", {})
            if outcome.endswith("cancel"):
                with pytest.raises(asyncio.CancelledError):
                    await second.agent.tool_executor.execute(call)
            else:
                result = await second.agent.tool_executor.execute(call)
                if outcome == "success":
                    assert result.error is None and result.result == "foreign execution succeeded"
                else:
                    assert result.error
            revalidate_origin(first, channel, destination, origin)
            assert delivery_origin(first, channel, destination) is origin
            assert first.tools.call_id.get() == "outer"
            assert second.tools.call_id.get() == ""
            stages.append("restored")
            return "outer execution succeeded"

        first.approval_handler = approve_outer
        second.approval_handler = approve_foreign
        second.agent.register_tool(other_custom_tool)
        definition = first.agent.register_tool(channel_send)
        try:
            await second.initialize()
            with bind_channel_send(first, definition):
                events = await collect(first)
            results = [event for event in events if isinstance(event, ToolResultEvent)]
            assert len(results) == 1 and results[0].error is None
            assert results[0].result == "outer execution succeeded"
            expected = ["approval", "approval_cleanup"]
            if not outcome.startswith("approval_"):
                expected.extend(["tool", "tool_cleanup"])
            assert stages == [*expected, "restored"]
            assert not foreign_providers[0].requests
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())
