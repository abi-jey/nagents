"""Voice follows real root turns, child completions and scheduled continuations."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.harness.tools import CodingTools
from nagents.live.delegation import ClientDelegationRequest
from nagents.web.live_bridge import MainAgentBridge
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.test_web_live_bridge import _config
from tests.test_web_live_bridge import speech

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.live.delegation import LiveAppendKind
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


@pytest.fixture(autouse=True)
def portable_lifecycle_bootstrap(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # ControlledHarness covers the root; delegated children are plain Harnesses.
    # These delivery/wakeup contracts must not depend on guarded workspace IO.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    guarded = Mock(side_effect=OSError("Guarded workspace file tools currently require POSIX"))
    monkeypatch.setattr(CodingTools, "directory", guarded)
    yield
    guarded.assert_not_called()


@pytest.mark.parametrize("detach_early", [False, True])
@pytest.mark.parametrize("schedule_followup", [False, True])
def test_overlapping_followup_answers_before_child_and_late_result_remains_correlated(
    tmp_path: Path, detach_early: bool, schedule_followup: bool
) -> None:
    async def scenario() -> None:
        child_started, release_child, interim, second, final = (asyncio.Event() for _ in range(5))
        sent: list[tuple[str, str, str]] = []
        wake_model, wake_spoken = asyncio.Event(), asyncio.Event()

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            text = str(messages[-1].content)
            if text == "hold child":
                child_started.set()
                await release_child.wait()
                yield TextDoneEvent(text="PRIVATE CHILD DETAILS")
            elif text.startswith("BACKGROUND TASK NOTIFICATION: scheduled self-wake"):
                yield TextDoneEvent(text="Second scheduled answer.")
                wake_model.set()
            elif text.startswith("BACKGROUND TASK NOTIFICATION:"):
                yield TextDoneEvent(text="First work complete.")
            elif messages[-1].role == "tool":
                yield TextDoneEvent(
                    text="Second answer."
                    if messages[-1].name == "schedule_wakeup"
                    else "I am waiting for the background task."
                )
            elif "second request" in text:
                if schedule_followup:
                    yield ToolCallEvent(
                        id="second-wake",
                        name="schedule_wakeup",
                        arguments={"seconds": 0.02, "reason": "Finish the second request"},
                    )
                else:
                    yield TextDoneEvent(text="Second answer.")
            else:
                yield ToolCallEvent(id="child", name="delegate", arguments={"prompt": "hold child"})

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            sent.append((kind, content, identifier))
            if content == "I am waiting for the background task.":
                interim.set()
            elif content == "Second answer.":
                second.set()
            elif content == "First work complete.":
                final.set()
            elif content == "Second scheduled answer.":
                wake_spoken.set()
            return f"session.{kind}.append"

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
            patch("nagents.web.live_bridge.VOICE_REPLY_SECONDS", 0.001),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
                state = app.state.web
                bridge = MainAgentBridge(state, state.selected_session_id)
                bridge.attach(append)
                try:
                    request = ClientDelegationRequest("first", transcript=speech("first request"))
                    bridge.observe(request)
                    assert await bridge.handle_request(request) == ""
                    async with asyncio.timeout(HANG_GUARD):
                        await child_started.wait()
                        await interim.wait()
                    run = state.active
                    assert run is not None
                    transcript = json.dumps(
                        [
                            {"speaker": "user", "text": "first request", "start_ms": 1, "end_ms": 100},
                            {"speaker": "user", "text": "second request", "start_ms": 101, "end_ms": 200},
                        ]
                    )
                    assert await bridge.handle_request(ClientDelegationRequest("second", transcript=transcript)) == ""
                    async with asyncio.timeout(HANG_GUARD):
                        await second.wait()
                    assert state.active is run and not release_child.is_set()
                    assert ("commentary", "Second answer.", "second") in sent
                    assert (
                        await bridge.handle_request(ClientDelegationRequest("duplicate", transcript=transcript)) == ""
                    )
                    before_close = list(sent)
                    if detach_early:
                        await bridge.close()
                    release_child.set()
                    async with asyncio.timeout(HANG_GUARD):
                        await run.task
                        if not detach_early:
                            await final.wait()
                        if schedule_followup:
                            await wake_model.wait()
                            if not detach_early:
                                await wake_spoken.wait()
                    if detach_early:
                        assert sent == before_close
                    else:
                        assert sent.count(("commentary", "First work complete.", "first")) == 1
                        assert sent.count(("commentary", "Second answer.", "second")) == 1
                        if schedule_followup:
                            assert sent.count(("commentary", "Second scheduled answer.", "second")) == 1
                        assert any(
                            kind == "thinking" and "finished" in text and origin == "first"
                            for kind, text, origin in sent
                        )
                    assert all("PRIVATE CHILD DETAILS" not in text for _, text, _ in sent)
                finally:
                    release_child.set()
                    await bridge.close()

    asyncio.run(scenario())


def test_real_scheduled_wakeup_uses_attached_voice_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        delivered = asyncio.Event()
        sent: list[tuple[str, str, str]] = []

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            if str(messages[-1].content).startswith("BACKGROUND TASK NOTIFICATION:"):
                yield TextDoneEvent(text="The scheduled result arrived.")
            elif messages[-1].role == "tool":
                yield TextDoneEvent(text="Scheduled the follow-up.")
            else:
                yield ToolCallEvent(
                    id="wake", name="schedule_wakeup", arguments={"seconds": 0.02, "reason": "Check the result"}
                )

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            sent.append((kind, content, identifier))
            if content == "The scheduled result arrived.":
                delivered.set()
            return f"session.{kind}.append"

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
                state = app.state.web
                bridge = MainAgentBridge(state, state.selected_session_id)
                bridge.attach(append)
                try:
                    await bridge.handle_request(ClientDelegationRequest("scheduled", transcript=speech("Check soon")))
                    async with asyncio.timeout(HANG_GUARD):
                        await delivered.wait()
                    assert sent.count(("commentary", "The scheduled result arrived.", "scheduled")) == 1
                finally:
                    await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_stopped_run_reports_spoken_outcome_on_open_voice(tmp_path: Path, cancel: bool) -> None:
    async def scenario() -> None:
        entered, reported = asyncio.Event(), asyncio.Event()
        sent: list[tuple[str, str, str]] = []

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            yield ErrorEvent(message="PRIVATE_PROVIDER_FAILURE", recoverable=False)

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            sent.append((kind, content, identifier))
            if kind == "commentary":
                reported.set()
            return f"session.{kind}.append"

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
                state = app.state.web
                bridge = MainAgentBridge(state, state.selected_session_id)
                bridge.attach(append)
                try:
                    await bridge.handle_request(ClientDelegationRequest("outcome", "Do the work"))
                    async with asyncio.timeout(HANG_GUARD):
                        await entered.wait()
                        if cancel:
                            assert state.active is not None
                            await state.stop(state.active)
                        await reported.wait()
                    spoken = [text for kind, text, origin in sent if kind == "commentary" and origin == "outcome"]
                    assert len(spoken) == 1 and ("stopped" if cancel else "could not complete") in spoken[0]
                    assert all("PRIVATE_PROVIDER_FAILURE" not in text for _, text, _ in sent)
                finally:
                    await bridge.close()

    asyncio.run(scenario())


def test_first_empty_public_request_asks_for_clarification_without_queuing_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        sent: list[str] = []
        delivered = asyncio.Event()

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            sent.append(content)
            delivered.set()
            return f"session.{kind}.append"

        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            bridge = MainAgentBridge(state, state.selected_session_id)
            bridge.attach(append)
            try:
                await bridge.handle_request(ClientDelegationRequest("empty"))
                async with asyncio.timeout(HANG_GUARD):
                    await delivered.wait()
                assert any("repeat" in text for text in sent)
                assert not any("already" in text for text in sent)
                assert not await state.channels.store.has_pending() and state.active is None
            finally:
                await bridge.close()

    asyncio.run(scenario())
