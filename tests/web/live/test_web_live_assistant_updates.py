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
from nagents.harness.providers.factory import HarnessProvider
from nagents.harness.runtime.core import Harness
from nagents.harness.tools.coding import CodingTools
from nagents.live.delegation import ClientDelegationRequest
from nagents.web.live.bridge import MainAgentBridge
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.web.live.test_web_live_bridge import _config
from tests.web.live.test_web_live_bridge import speech

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.live.delegation import LiveAppendKind
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition
    from nagents.web.http.routing import Work


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
        reports: list[dict[str, object]] = []
        child_activity: list[dict[str, object]] = []
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
            elif text.startswith("BACKGROUND AGENT WAKEUP:"):
                yield TextDoneEvent(text="Second scheduled answer.")
                wake_model.set()
            elif text.startswith("BACKGROUND AGENT UPDATES:"):
                yield TextDoneEvent(text="First work complete.")
            elif messages[-1].role == "tool":
                yield TextDoneEvent(
                    text="Second answer."
                    if messages[-1].name == "schedule_wakeup"
                    else "I am waiting for the background task."
                )
            elif "second request" in text:
                assert text == "second request"
                if schedule_followup:
                    yield ToolCallEvent(
                        id="second-wake",
                        name="schedule_wakeup",
                        arguments={"seconds": 0.02, "reason": "Finish the second request"},
                    )
                else:
                    yield TextDoneEvent(text="Second answer.")
            else:
                assert text == "first request"
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "hold child"})

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
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
                state = app.state.web
                state.run_observers.add(
                    lambda run, record: (
                        child_activity.append(dict(record)) if record.get("event") == "task_activity" else None
                    )
                )
                bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
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
                            {"speaker": "assistant", "text": "I am working on it.", "start_ms": 100, "end_ms": 101},
                            {"speaker": "user", "text": "second", "start_ms": 101, "end_ms": 150},
                            {"speaker": "user", "text": " request", "start_ms": 150, "end_ms": 200},
                        ]
                    )
                    assert await bridge.handle_request(ClientDelegationRequest("second", transcript=transcript)) == ""
                    async with asyncio.timeout(HANG_GUARD):
                        await second.wait()
                    assert state.active is run and not release_child.is_set()
                    assert ("commentary", "Second answer.", "second") in sent
                    admitted = [report for report in reports if "request_input" in report]
                    assert [report["request_input"] for report in admitted] == ["first request", "second request"]
                    assert all(report["text"] == "Processing…" for report in admitted)
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
                    assert any("PRIVATE CHILD DETAILS" in json.dumps(record) for record in child_activity)
                    assert all(record.get("session_id") for record in child_activity)
                finally:
                    release_child.set()
                    await bridge.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("hold_claim_cleanup", [False, True])
def test_real_scheduled_wakeup_uses_attached_voice_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold_claim_cleanup: bool
) -> None:
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
            if str(messages[-1].content).startswith("BACKGROUND AGENT WAKEUP:"):
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
                root = state.selected_session_id
                held, release, parked, notified = (asyncio.Event() for _ in range(4))
                clock = [0.0]
                if hold_claim_cleanup:
                    # The root can finish before the durable inbox claim is
                    # released. Force the timer worker to observe that interval,
                    # independently of platform clock and SQLite speed.
                    monkeypatch.setattr(state.wakeups, "clock", lambda: clock[0])
                    original_finish = state.channels.store.finish_work
                    original_tick = state.wakeups.tick
                    original_changed = state.wakeups.changed.set

                    async def finish_work(work: Work, status: str) -> None:
                        held.set()
                        await release.wait()
                        await original_finish(work, status)

                    async def tick(*, dispatch: bool = False) -> None:
                        await original_tick(dispatch=dispatch)
                        if (
                            clock[0] == 1.0
                            and held.is_set()
                            and state.run_for(root) is None
                            and root in state.channels.work_tasks
                        ):
                            parked.set()

                    def changed() -> None:
                        if parked.is_set() and root not in state.channels.work_tasks:
                            notified.set()
                        original_changed()

                    monkeypatch.setattr(state.channels.store, "finish_work", finish_work)
                    monkeypatch.setattr(state.wakeups, "tick", tick)
                    monkeypatch.setattr(state.wakeups.changed, "set", changed)
                bridge = MainAgentBridge(state, state.selected_session_id)
                bridge.attach(append)
                try:
                    await bridge.handle_request(ClientDelegationRequest("scheduled", transcript=speech("Check soon")))
                    async with asyncio.timeout(HANG_GUARD):
                        if hold_claim_cleanup:
                            await held.wait()
                            assert state.run_for(root) is None and state.wakeups.pending
                            claim = state.channels.work_tasks[root]
                            clock[0] = 1.0
                            state.wakeups.changed.set()
                            await parked.wait()
                            assert not delivered.is_set()
                            release.set()
                            await claim
                            assert notified.is_set(), "Releasing the inbox lane must notify the timer worker"
                        await delivered.wait()
                    assert sent.count(("commentary", "The scheduled result arrived.", "scheduled")) == 1
                finally:
                    release.set()
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
