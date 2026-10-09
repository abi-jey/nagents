"""First-turn policy and one backend input per delegated caller identity."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.provider import HarnessProvider
from nagents.live.runtime import ClientDelegations
from nagents.types import Message
from nagents.web.live import create_agent
from nagents.web.live import create_login_config
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.live_handoff import LoginDelegations
from nagents.web.live_login import normalize_event
from nagents.web.live_settings import LiveConnection
from nagents.web.live_settings import LiveValues
from nagents.web.subscriptions import Subscriber
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.test_web_live_bridge import _config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.live.delegation import ClientDelegationRequest
    from nagents.live.delegation import LiveAppendKind
    from nagents.provider.openai import CodexCredentials


@pytest.mark.parametrize("native", [False, True])
def test_effective_client_policy_delegates_first_greeting_once_without_history_replay(native: bool) -> None:
    async def scenario() -> None:
        async def handler(request: ClientDelegationRequest) -> str:
            raise AssertionError("Constructing voice settings must not run the backend")

        async def credentials() -> CodexCredentials:
            raise AssertionError("Constructing settings must not retrieve credentials")

        values = LiveValues.model_validate(
            {**LiveValues.defaults().model_dump(), "enabled": True, "instructions": "Speak concisely in English."}
        )
        connection = LiveConnection(
            values,
            "1" * 64,
            SecretStr("" if native else "synthetic-api-key"),
            credential_available=native,
            voice_auth="chatgpt" if native else "api-key",
            login_credentials=credentials if native else None,
        )
        if native:
            instructions = create_login_config(connection, "sol", handler).instructions
        else:
            agent = create_agent(connection, client_request_handler=handler)
            try:
                session = agent.live_configuration()
                assert session["delegation"] == {"type": "client"}
                assert isinstance(session["instructions"], str)
                instructions = session["instructions"]
            finally:
                await agent.close()
        assert "first utterance" in instructions and "exactly once" in instructions
        assert "Hello, Hey" in instructions and "brief greeting" in instructions
        assert "project instructions, user profile" in instructions
        assert "do not resume or repeat actions from saved chat history" in instructions
        assert "substantive request" in instructions and "same delegation" in instructions
        assert "Do not send a second context-only delegation" in instructions
        assert "Speak concisely in English." in instructions

    asyncio.run(scenario())


@pytest.mark.requires_posix
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("first_text", ["Hello", "Please explain the current project"])
def test_first_notice_loads_context_once_and_typed_messages_after_close_are_not_relayed(
    tmp_path: Path, native: bool, first_text: str
) -> None:
    async def scenario() -> None:
        (tmp_path / "AGENTS.md").write_text("GREETING_CONTEXT_MARKER: Never repeat completed historical actions.\n")
        entered, release, spoken = (asyncio.Event() for _ in range(3))
        model_inputs: list[str] = []
        voice_updates: list[tuple[str, str, str]] = []
        transport_results: list[dict[str, object]] = []
        answer = "The same legitimate answer."

        async def generate(
            provider: HarnessProvider, messages: list[Message], **kwargs: object
        ) -> AsyncIterator[Event]:
            model_inputs.append(str(messages[-1].content))
            if len(model_inputs) == 1:
                entered.set()
                await release.wait()
                assert (
                    json.loads(model_inputs[-1].split("Handoff data:\n", 1)[1].split("\nSpeech data:", 1)[0])["request"]
                    == first_text
                )
                yield ToolCallEvent(id="load-context", name="read_file", arguments={"path": "AGENTS.md"})
                return
            elif len(model_inputs) == 2:
                assert messages[-1].role == "tool" and "GREETING_CONTEXT_MARKER" in str(messages[-1].content)
            else:
                assert model_inputs[-1] == "Typed after voice"
            yield TextDoneEvent(text=answer)

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            voice_updates.append((kind, content, identifier))
            if kind == "commentary" and content == answer:
                spoken.set()
            return "delegation.context.append" if native else f"session.{kind}.append"

        async def send(event: dict[str, object]) -> None:
            transport_results.append(event)

        async def unused(context: str, identifier: str) -> str:
            raise AssertionError("The typed request handler owns the backend")

        with (
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with (
                client_app(tmp_path, config=_config(tmp_path), controlled=False) as (app, _, _, _),
                asyncio.timeout(HANG_GUARD),
            ):
                state = app.state.web
                assert "GREETING_CONTEXT_MARKER" in state.harness.instructions["AGENTS.md"]
                root = state.selected_session_id
                await state.history.add_message(
                    root, Message(role="user", content="Historical action already completed")
                )
                await state.history.add_message(
                    root, Message(role="assistant", content="That historical action is complete.")
                )
                subscribers = [Subscriber(session_id=root, ready=True), Subscriber(session_id=root, ready=True)]
                state.bus.subscribers.update(subscribers)
                frames: list[list[dict[str, object]]] = [[], []]

                def drain() -> None:
                    for subscriber, records in zip(subscribers, frames, strict=True):
                        while not subscriber.queue.empty():
                            frame, size = subscriber.queue.get_nowait()
                            subscriber.bytes -= size
                            records.append(frame)

                bridge = MainAgentBridge(state, root)
                bridge.attach(append)
                lane = (
                    LoginDelegations(bridge.handle_request, send, observer=bridge.observe_native)
                    if native
                    else ClientDelegations(unused, send, handler=bridge.handle_request, observer=bridge.observe)
                )
                speech: dict[str, object] = {
                    "type": "session.input_transcript.delta",
                    "delta": first_text,
                    "start_ms": 1,
                    "end_ms": 100,
                }
                native_notice: dict[str, object] = {
                    "type": "delegation.created",
                    "item": {
                        "type": "delegation",
                        "id": "first",
                        "target": "client",
                        "content": [{"type": "input_text", "text": first_text}],
                    },
                }
                notice = normalize_event(native_notice)
                if isinstance(lane, LoginDelegations):
                    lane.observe(speech, {})
                    lane.observe(notice, native_notice)
                    lane.observe(notice, native_notice)
                else:
                    lane.observe(speech)
                    lane.observe(notice)
                    lane.observe(notice)
                worker = asyncio.create_task(lane.run())
                try:
                    while not entered.is_set():
                        assert not worker.done(), await asyncio.gather(worker, return_exceptions=True)
                        await asyncio.sleep(0.001)
                    first = state.active
                    assert first is not None and len(model_inputs) == 1
                    release.set()
                    await first.task
                    assert first.outcome == "completed"
                    await spoken.wait()
                    drain()
                    assert model_inputs[0].count('"request":') == 1 and not transport_results
                    assert voice_updates.count(("commentary", answer, "first")) == 1
                    await bridge.close()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    sent_before_typing = list(voice_updates)
                    for index in (1, 2):
                        _, admitted = await state.queued_inputs.submit(root, f"typed-{index}", "Typed after voice")
                        assert admitted
                        _, duplicate = await state.queued_inputs.submit(root, f"typed-{index}", "Typed after voice")
                        assert not duplicate
                        while len(model_inputs) < index + 2 or state.active is not None:
                            await asyncio.sleep(0.001)
                        drain()
                    assert len(model_inputs) == 4 and voice_updates == sent_before_typing
                    history = await state.history.snapshot(root)
                    answers = [row for row in history if row["role"] == "assistant" and row["content"] == answer]
                    assert len(answers) == 3 and len({row["history_id"] for row in answers}) == 3
                    for records in frames:
                        done = []
                        for frame in records:
                            record = frame.get("record")
                            if (
                                frame.get("type") == "event"
                                and isinstance(record, dict)
                                and record.get("event") == "text_done"
                            ):
                                done.append(record)
                        assert len(done) == 3
                        assert len({record["run_id"] for record in done}) == 3
                finally:
                    release.set()
                    await bridge.close()
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    state.bus.subscribers.difference_update(subscribers)

    asyncio.run(scenario())
