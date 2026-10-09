"""Live delivery failures, quiet cues and provenance across real root turns."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.harness.tools import CodingTools
from nagents.live.delegation import ClientDelegationRequest
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.live_login import ChatGPTLiveConnection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.test_web_live_bridge import _config
from tests.test_web_live_login import config as login_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from aiohttp import ClientWebSocketResponse

    from nagents.events import Event
    from nagents.live.delegation import LiveAppendKind
    from nagents.types import Message
    from nagents.web.service import Run
    from nagents.web.service import WebState


@asynccontextmanager
async def live_state(tmp_path: Path, generate: Callable[..., AsyncIterator[Event]]) -> AsyncIterator[WebState]:
    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            yield app.state.web


@pytest.mark.parametrize("overflow", [False, True])
def test_failed_update_consumer_requires_reconnect_without_blackholing_new_requests(
    tmp_path: Path, overflow: bool
) -> None:
    async def scenario() -> None:
        entered, release_model, attempted = (asyncio.Event() for _ in range(3))
        reports: list[dict[str, object]] = []
        inputs: list[str] = []

        async def generate(
            provider: HarnessProvider, messages: list[Message], **kwargs: object
        ) -> AsyncIterator[Event]:
            inputs.append(str(messages[-1].content))
            entered.set()
            await release_model.wait()
            yield TextDoneEvent(text="The accepted work completed in chat.")

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            attempted.set()
            if overflow:
                await asyncio.Event().wait()
            raise RuntimeError("PRIVATE_SINK_FAILURE")

        async with live_state(tmp_path, generate) as state, asyncio.timeout(HANG_GUARD):
            bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
            bridge.attach(append)
            try:
                await bridge.handle_request(ClientDelegationRequest("first", "The accepted first request"))
                await entered.wait()
                await attempted.wait()
                run = state.active
                assert run is not None
                if overflow:
                    for index in range(129):
                        bridge.updates.append("thinking", f"Bounded pending progress {index}", "first")
                worker = bridge.updates._worker
                assert worker is not None
                await asyncio.gather(worker, return_exceptions=True)
                assert bridge.updates.closed
                failed = [record["live_append"] for record in reports if "live_append" in record]
                assert failed and all(isinstance(record, dict) and record.get("failed") for record in failed)

                result = await bridge.handle_request(ClientDelegationRequest("next", "Do not silently discard me"))
                assert result and "reconnect" in result.lower()
                later = [record for record in reports if record.get("delegation_id") == "next"]
                assert [record.get("status") for record in later] == ["queued", "failed"]
                assert len(inputs) == 1 and not await state.channels.store.has_pending()
                assert "PRIVATE_SINK_FAILURE" not in result + json.dumps(reports)

                release_model.set()
                await run.task
                assert run.outcome == "completed"  # A failed voice sink does not cancel accepted tools/work.
                await bridge.close()
                assert await bridge.handle_request(ClientDelegationRequest("detached", "No speech after End")) == ""
            finally:
                release_model.set()
                await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("quiet_at_send", [False, True])
def test_attention_cue_rechecks_quiet_state_and_never_speaks_native_instruction_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, quiet_at_send: bool
) -> None:
    # The child is a plain Harness, unlike the portable ControlledHarness root.
    # This delivery lifecycle needs neither child project instructions nor file IO.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    guarded = Mock(side_effect=OSError("Guarded workspace file tools currently require POSIX"))
    monkeypatch.setattr(CodingTools, "directory", guarded)

    async def scenario() -> None:
        child_started, release_child, interim, writing_result, release_sink, drained = (
            asyncio.Event() for _ in range(6)
        )
        quiet = True
        sent: list[tuple[str, str, str]] = []
        packets: list[dict[str, object]] = []
        reports: list[dict[str, object]] = []

        class NativeSocket:
            async def send_json(self, event: dict[str, object]) -> None:
                packets.append(event)

        connection = ChatGPTLiveConnection(login_config())
        connection._sockets.append(cast("ClientWebSocketResponse", NativeSocket()))
        connection._delegations.add("task")

        async def generate(
            provider: HarnessProvider, messages: list[Message], **kwargs: object
        ) -> AsyncIterator[Event]:
            last = str(messages[-1].content)
            if last == "edge child":
                child_started.set()
                await release_child.wait()
                yield TextDoneEvent(text="PRIVATE_CHILD_DETAIL")
            elif last.startswith("BACKGROUND TASK NOTIFICATION:"):
                yield TextDoneEvent(text="The verified background answer.")
            elif messages[-1].role == "tool":
                yield TextDoneEvent(text="Background work is running.")
            else:
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "edge child"})

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            if content == "The verified background answer.":
                writing_result.set()
                await release_sink.wait()
            wire = await connection.append(kind, content, identifier) if native else f"session.{kind}.append"
            if wire:
                sent.append((kind, content, identifier))
            if content == "Background work is running.":
                interim.set()
            if content == "Drain complete":
                drained.set()
            return wire

        async with live_state(tmp_path, generate) as state, asyncio.timeout(HANG_GUARD):
            bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
            bridge.attach(append)
            bridge.updates.needs_attention = lambda: quiet
            try:
                await bridge.handle_request(ClientDelegationRequest("task", "Start background work"))
                await child_started.wait()
                await interim.wait()
                run = state.active
                assert run is not None
                release_child.set()
                await writing_result.wait()
                await run.task  # Completion and its attention cue are queued behind the blocked sink.
                quiet = quiet_at_send
                bridge.updates.append("thinking", "Drain complete", "task")
                release_sink.set()
                await drained.wait()
                assert sent.count(("commentary", "The verified background answer.", "task")) == 1
                instructions = [text for kind, text, _ in sent if kind == "instructions"]
                assert len(instructions) == int(quiet_at_send and not native)
                assert all("PRIVATE_CHILD_DETAIL" not in text for _, text, _ in sent)
                if native:
                    assert all("Tell the caller" not in json.dumps(packet) for packet in packets)
                    assert all(packet["type"] == "delegation.context.append" for packet in packets)
                    assert any(packet["channel"] == "speakable" for packet in packets)
                recorded = [record["live_append"] for record in reports if "live_append" in record]
                assert sum(
                    isinstance(record, dict) and record.get("kind") == "instructions" and bool(record.get("wire_type"))
                    for record in recorded
                ) == int(quiet_at_send and not native)
            finally:
                release_child.set()
                release_sink.set()
                await bridge.close()
                connection._sockets.clear()

    asyncio.run(scenario())
    guarded.assert_not_called()


def test_new_call_cannot_reuse_old_delegation_identity_for_recovered_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        old_sent: list[tuple[str, str, str]] = []
        new_sent: list[tuple[str, str, str]] = []
        old_result, own_result = asyncio.Event(), asyncio.Event()

        async def generate(
            provider: HarnessProvider, messages: list[Message], **kwargs: object
        ) -> AsyncIterator[Event]:
            if "Original old-call work" in str(messages[-1].content):
                yield TextDoneEvent(text="Old-call answer.")
            else:
                yield TextDoneEvent(text="Current-call answer.")

        async def old_sink(kind: LiveAppendKind, content: str, identifier: str) -> str:
            old_sent.append((kind, content, identifier))
            return f"session.{kind}.append"

        async def new_sink(kind: LiveAppendKind, content: str, identifier: str) -> str:
            new_sent.append((kind, content, identifier))
            if content == "Old-call answer.":
                old_result.set()
            elif content == "Current-call answer.":
                own_result.set()
            return f"session.{kind}.append"

        async with live_state(tmp_path, generate) as state, asyncio.timeout(HANG_GUARD):
            state.mutating = True
            old = MainAgentBridge(state, state.selected_session_id)
            old.attach(old_sink)
            new = MainAgentBridge(state, state.selected_session_id)
            new.attach(new_sink)
            try:
                await old.handle_request(ClientDelegationRequest("same-provider-id", "Original old-call work"))
                await old.close()
                before = list(old_sent)
                request = ClientDelegationRequest("same-provider-id", "Current call's own work")
                new.observe(request)  # Same raw ID in a different voice namespace is not the old task.
                assert old.voice_session_id != new.voice_session_id
                state.mutating = False
                state.channels.changed.set()
                await old_result.wait()
                assert ("commentary", "Old-call answer.", "") in new_sent
                assert ("commentary", "Old-call answer.", "same-provider-id") not in new_sent
                await new.handle_request(request)
                await own_result.wait()
                assert ("commentary", "Current-call answer.", "same-provider-id") in new_sent
                assert old_sent == before
            finally:
                state.mutating = False
                await old.close()
                await new.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_model_request_capture_uses_producer_turn_while_consumer_is_behind(tmp_path: Path) -> None:
    async def scenario() -> None:
        first_started, release_response, consumer_waiting, release_consumer, second_started, spoken = (
            asyncio.Event() for _ in range(6)
        )
        reports: list[dict[str, object]] = []
        (tmp_path / "capture.txt").write_text("A verified fixture result")

        async def generate(
            provider: HarnessProvider, messages: list[Message], **kwargs: object
        ) -> AsyncIterator[Event]:
            if "Second capture request" in str(messages[-1].content):
                second_started.set()
                yield TextDoneEvent(text="Second captured answer.")
            else:
                first_started.set()
                await release_response.wait()
                yield ToolCallEvent(id="capture-read", name="read_file", arguments={"path": "capture.txt"})

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            if content == "Second captured answer.":
                spoken.set()
            return f"session.{kind}.append"

        async with live_state(tmp_path, generate) as state, asyncio.timeout(HANG_GUARD):
            original = state.send

            async def delayed(run: Run, record: dict[str, object]) -> None:
                await original(run, record)
                if record.get("event") == "tool_result" and record.get("id") == "capture-read":
                    consumer_waiting.set()
                    await release_consumer.wait()

            bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
            bridge.attach(append)
            try:
                with patch.object(state, "send", delayed):
                    await bridge.handle_request(ClientDelegationRequest("a", "First capture request"))
                    await first_started.wait()
                    await bridge.handle_request(ClientDelegationRequest("b", "Second capture request"))
                    run = state.active
                    assert run is not None
                    release_response.set()
                    await consumer_waiting.wait()
                    await second_started.wait()
                    assert bridge.updates.origin(run) == "a"
                    captures: list[dict[str, object]] = []
                    for record in reports:
                        capture = record.get("model_request")
                        if not isinstance(capture, dict) or capture.get("type") != "model_context":
                            continue
                        payload = capture.get("payload")
                        if isinstance(payload, dict) and "Second capture request" in str(payload.get("text", "")):
                            captures.append(record)
                    assert captures and all(record["delegation_id"] == "b" for record in captures)
                    release_consumer.set()
                    await spoken.wait()
                    await run.task
            finally:
                release_response.set()
                release_consumer.set()
                await bridge.close()

    asyncio.run(scenario())
