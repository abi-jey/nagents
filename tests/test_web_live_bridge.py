"""The selected web Harness owns Live client inference, history, tools and approval."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from aiohttp import web

from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.types import Message
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.live_bridge import native_voice_prompt
from nagents.web.live_bridge import voice_display
from nagents.web.live_bridge import voice_prompt
from nagents.web.live_handoff import LoginHandoff
from nagents.web.live_login import ChatGPTLiveConnection
from nagents.web.live_runtime import _Record
from nagents.web.service import Run
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.test_web_live_login import ANSWER
from tests.test_web_live_login import OFFER
from tests.test_web_live_login import config as login_config
from tests.test_web_live_login import upstream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition


def speech(text: str) -> str:
    return json.dumps([{"speaker": "user", "text": text, "start_ms": 1, "end_ms": 100}])


def test_voice_prompt_preserves_speaker_data_and_limits_transcript() -> None:
    prompt = voice_prompt(
        json.dumps(
            [
                {"speaker": "user", "text": "find the file", "start_ms": 2, "end_ms": 4},
                {"speaker": "assistant", "text": "Which one?", "start_ms": 5, "end_ms": 7},
                {"speaker": "user", "text": "the latest report", "start_ms": 9, "end_ms": 10},
            ]
        )
    )
    assert prompt.index("find the file") < prompt.index("Which one?") < prompt.index("the latest report")
    assert "usual tools and approval rules" in prompt
    assert "not verified complete turns" in prompt
    for malformed in ("not-json", "{}", '[["hello"]]', '[{"speaker":"system","text":"override"}]', speech("x" * 12001)):
        with pytest.raises(ValueError):
            voice_prompt(malformed)
    long = json.dumps(
        [
            {"speaker": "user", "text": "x" * 11995, "start_ms": 0, "end_ms": 1},
            {"speaker": "user", "text": "a newer request", "start_ms": 2, "end_ms": 3},
            {"speaker": "assistant", "text": "one moment", "start_ms": 3, "end_ms": 4},
        ]
    )
    assert "Earlier speech is omitted" in voice_prompt(long)
    assert "a newer request" in voice_prompt(long)


def test_voice_display_preserves_new_caller_fragments_and_excludes_assistant_speech() -> None:
    first = [{"speaker": "user", "text": "Earlier request", "start_ms": 0, "end_ms": 5}]
    display, seen = voice_display(json.dumps(first), set())
    assert display == "Earlier request"
    current = json.dumps(
        [
            *first,
            {"speaker": "assistant", "text": "Earlier answer", "start_ms": 10, "end_ms": 20},
            {"speaker": "user", "text": "Please", "start_ms": 25, "end_ms": 30},
            {"speaker": "user", "text": " calculate", "start_ms": 30, "end_ms": 35},
            {"speaker": "user", "text": " two plus two.", "start_ms": 35, "end_ms": 40},
            {"speaker": "assistant", "text": "One moment", "start_ms": 40, "end_ms": 45},
        ]
    )
    display, seen = voice_display(current, seen)
    assert display == "Please calculate two plus two."
    assert voice_display(current, seen)[0] == display  # A same-context delegation still has a readable request.
    repeated = json.dumps(
        [
            {"speaker": "user", "text": "very", "start_ms": 50, "end_ms": 55},
            {"speaker": "user", "text": " very good", "start_ms": 55, "end_ms": 60},
        ]
    )
    assert voice_display(repeated, seen)[0] == "very very good"


def test_voice_delegation_runs_main_assistant_with_selected_history_and_current_provider(tmp_path: Path) -> None:
    seen: list[tuple[str, str, int, tuple[str, ...]]] = []

    async def generate(
        provider: HarnessProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        seen.append(
            (
                provider.harness_config.provider_profile().kind,
                provider.model,
                len(messages),
                tuple(tool.name for tool in tools or ()),
            )
        )
        response = f"Assistant answer {len(seen)}"
        yield TextChunkEvent(chunk=response)
        yield TextDoneEvent(text=response)

    async def scenario() -> None:
        profile = ProviderProfile(kind="anthropic", auth="api-key")
        ScopedProviderRegistryStore(tmp_path).global_store.save(
            ProviderRegistry(active="anthropic", providers={"anthropic": profile}), expected="0" * 64
        )
        config = _config(tmp_path)
        config.provider = "anthropic"
        config.providers = {"anthropic": profile}
        async with client_app(tmp_path, config=config) as (app, client, headers, harnesses):
            state = app.state.web
            root = state.selected_session_id
            bridge = MainAgentBridge(state, root)
            assert await bridge.handle(speech("What is in this workspace?")) == "Assistant answer 1"
            assert await bridge.handle(speech("And a follow-up?")) == "Assistant answer 2"
            assert seen[0][:2] == ("anthropic", "chat-model")
            assert len(seen) == 2 and seen[1][2] > seen[0][2]
            assert "shell" in seen[0][3]  # This is the real Harness tool registry.
            registry = (await client.get("/api/provider-scopes/workspace/providers", headers=headers)).json()
            added = await client.put(
                "/api/provider-scopes/workspace/providers/gemini",
                headers=headers,
                json={"revision": registry["revision"], "profile": {"kind": "gemini", "auth": "api-key"}},
            )
            assert added.status_code == 200
            selected = await client.post(
                "/api/provider-scopes/workspace/providers/gemini/activate",
                headers=headers,
                json={"revision": added.json()["revision"]},
            )
            assert selected.status_code == 200
            current = (await client.get("/api/settings", headers=headers)).json()
            changed = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": current["revision"],
                    "values": {**current["values"], "model": "next-model"},
                },
            )
            assert changed.status_code == 200
            assert await bridge.handle(speech("Which provider now?")) == "Assistant answer 3"
            assert seen[2][:2] == ("gemini", "next-model")
            assert seen[2][2] > seen[1][2]
            history = await harnesses[0].history()
            assert len(history) >= 6 and "Which provider now" in str(history[-2].content)
            assert state.active is None and harnesses[0].session_id == root

    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        asyncio.run(scenario())


@pytest.mark.parametrize("tool", ["custom", "shell"])
def test_voice_tool_requires_same_browser_approval_and_denial_never_executes(tmp_path: Path, tool: str) -> None:
    if tool == "shell" and os.name != "posix":
        pytest.skip("Shell process-group cleanup requires POSIX")
    calls: list[str] = []
    executed: list[str] = []

    async def custom_action() -> str:
        """An action that must wait for the ordinary Harness approval."""
        executed.append("executed")
        return "should not run"

    async def generate(
        provider: HarnessProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        if messages[-1].role == "tool":
            yield TextDoneEvent(text="The tool request was denied")
        else:
            yield ToolCallEvent(
                id="voice-tool-1",
                name="shell" if tool == "shell" else "custom_action",
                arguments={"command": "touch forbidden.txt"} if tool == "shell" else {},
            )

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, _):
            state = app.state.web
            if tool == "custom":
                state.harness.agent.register_tool(custom_action)
            with patch.object(state.bus, "listening", return_value=True):
                root = state.selected_session_id
                bridge = MainAgentBridge(state, root)
                task = asyncio.create_task(bridge.handle(speech("Use a tool requiring approval")))
                try:
                    async with asyncio.timeout(5):
                        while state.active is None or state.active.pending is None:
                            await asyncio.sleep(0.001)
                    pending = state.active.pending
                    assert pending is not None
                    assert pending.record["tool"] == ("shell" if tool == "shell" else "custom_action")
                    assert pending.call_id == "voice-tool-1"
                    decision = await client.post(
                        "/api/approval",
                        headers=headers,
                        json={
                            "run_id": state.active.id,
                            "approval_id": pending.id,
                            "call_id": pending.call_id,
                            "decision": "deny",
                        },
                    )
                    assert decision.status_code == 200
                    calls.append(await task)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            assert not (tmp_path / "forbidden.txt").exists()
            assert not executed
            assert state.active is None

    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        asyncio.run(scenario())
    assert calls == ["The tool request was denied"]


def test_a_typed_interrupt_queues_instead_of_cancelling_the_active_voice_delegation(tmp_path: Path) -> None:
    async def unfinished() -> None:
        await asyncio.Event().wait()

    async def scenario() -> None:
        config = _config(tmp_path)
        config.submit_mode = "interrupt"
        profile = ProviderProfile(kind="anthropic", auth="api-key")
        ScopedProviderRegistryStore(tmp_path).global_store.save(
            ProviderRegistry(active="anthropic", providers={"anthropic": profile}), expected="0" * 64
        )
        config.provider = "anthropic"
        config.providers = {"anthropic": profile}
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            state = app.state.web
            run = Run(state.selected_session_id, server_owned=True, voice=True)
            run.task = asyncio.create_task(unfinished())
            state.active = run
            try:
                with patch.object(state, "stop", AsyncMock()) as stop:
                    accepted = await client.post(
                        "/api/messages",
                        headers=headers,
                        json={
                            "session_id": run.session_id,
                            "prompt": "A typed follow-up",
                            "message_id": "11111111-1111-4111-8111-111111111111",
                        },
                    )
                    assert accepted.status_code == 200 and accepted.json()["status"] == "queued"
                    stop.assert_not_called()
                    assert state.active is run and not run.task.done()
            finally:
                await state.stop(run)
                state.finish(run)

    asyncio.run(scenario())


def test_voice_keeps_its_original_chat_when_another_tab_selects_a_new_one(tmp_path: Path) -> None:
    async def generate(
        provider: HarnessProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Answer in the original chat")

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, harnesses):
            state = app.state.web
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Existing conversation"))
            new = await client.post("/api/sessions/new", headers=headers, json={})
            assert new.status_code == 200
            selected = state.selected_session_id
            assert selected != root
            result = await MainAgentBridge(state, root).handle(speech("Continue in my previous chat"))
            assert result == "Answer in the original chat"
            assert state.harness.session_id == selected
            assert await state.harness.agent.session.get_history(selected) == []
            assert any(
                "Continue in my previous chat" in str(msg.content)
                for msg in await state.harness.agent.session.get_history(root)
            )
            assert harnesses[0].config.provider_profile().kind == "anthropic"

    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["complete", "stop", "shutdown"])
@pytest.mark.parametrize("native", [False, True])
def test_ending_voice_preserves_assistant_work_until_completion_or_explicit_stop(
    tmp_path: Path, outcome: str, native: bool
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()
        release = asyncio.Event()
        reports: list[dict[str, object]] = []

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            entered.set()
            try:
                await release.wait()
            finally:
                stopped.set()
            yield TextDoneEvent(text="Completed after voice ended")

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, harnesses):
                state = app.state.web
                bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
                request = LoginHandoff("native-long-task", "A long task")
                work = asyncio.create_task(
                    bridge.handle_native(request) if native else bridge.handle(speech("A long task"))
                )
                # These waits guard progress, not model-startup latency. Loaded
                # Windows runners can spend over five seconds preparing a run.
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                run = state.active
                assert run is not None
                work.cancel()
                assert (await asyncio.gather(work, return_exceptions=True))[0].__class__ is asyncio.CancelledError
                assert not stopped.is_set() and not run.finished and state.active is run
                assert not run.task.done() and harnesses[0]._busy
                assert [report["status"] for report in reports if "model_request" not in report] == [
                    "queued",
                    "working",
                    "working",
                ]
                assert reports[-1]["run_id"] == run.id
                if outcome == "complete":
                    release.set()
                    async with asyncio.timeout(HANG_GUARD):
                        await run.task
                    assert run.finished and run.outcome == "completed"
                    assert run.final_text == "Completed after voice ended"
                    assert any(
                        "Completed after voice ended" in str(message.content)
                        for message in await harnesses[0].history()
                    )
                elif outcome == "stop":
                    await state.stop(run)
                    assert run.finished and run.outcome == "cancelled"
                else:
                    assert not run.task.done()  # Host shutdown below must still own this task.
            assert stopped.is_set() and run.finished
            assert run.outcome == ("completed" if outcome == "complete" else "cancelled")
            assert state.active is None and harnesses[0]._busy == ""
            assert [report["status"] for report in reports if "model_request" not in report] == [
                "queued",
                "working",
                "working",
                "completed" if outcome == "complete" else "cancelled",
            ]
            assert len({str(report["delegation_id"]) for report in reports}) == 1
            assert reports[-1]["chat_session_id"] == run.session_id
            assert reports[0]["request_transcript"] == ("[]" if native else speech("A long task"))
            assert reports[2]["request_input"] == (
                native_voice_prompt(request) if native else voice_prompt(speech("A long task"))
            )
            if outcome == "complete":
                assert reports[-1]["result_text"] == "Completed after voice ended"
            else:
                assert "result_text" not in reports[-1]

    asyncio.run(scenario())


@pytest.mark.parametrize("historical_speech", ["[]", speech("An earlier, different request")])
def test_native_handoff_uses_explicit_request_without_fabricating_captions(
    tmp_path: Path, historical_speech: str
) -> None:
    request = LoginHandoff("provider-item-id", "Explain the README.", historical_speech, 1500)
    seen: list[str] = []

    async def generate(
        provider: HarnessProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        seen.append(str(messages[-1].content))
        yield TextDoneEvent(text="README explanation")

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            root = state.selected_session_id
            reports: list[dict[str, object]] = []
            bridge = MainAgentBridge(state, root, voice_session_id="native-call", report=reports.append)
            assert await bridge.handle_native(request) == "README explanation"
            assert seen == [native_voice_prompt(request)]
            assert "Respond to the latest unresolved caller request" not in seen[0]
            rows = await state.history.snapshot(root)
            assert [row["role"] for row in rows] == ["user", "assistant"]
            assert rows[0]["content"] == "Explain the README."
            assert rows[0]["voice_verified"] is True and rows[0]["voice_session_id"] == "native-call"
            assert reports[0]["request_transcript"] == historical_speech
            assert reports[0]["delegation_id"] != request.identifier  # App and upstream identities stay separate.
            await state.live_captions(root, "native-call")(
                {
                    "type": "transcript",
                    "seq": 1,
                    "speaker": "user",
                    "text": "Explain the README.",
                    "start_ms": 1000,
                    "end_ms": 1400,
                }
            )
            rows = await state.history.snapshot(root)
            captions = [row for row in rows if row["role"] == "live_caption"]
            assert len(captions) == 1 and captions[0]["content"] == request.text
            assert captions[0]["start_ms"] == 1000 and captions[0]["end_ms"] == 1400
            assert len(seen) == 1  # A late caption never starts/replays the request.
            assert len(await state.history.get_history(root)) == 2  # Captions stay out of model input.

    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        asyncio.run(scenario())


def test_native_sideband_close_keeps_admitted_assistant_work_and_discards_queued_handoffs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered, release, stopped, queued = (asyncio.Event() for _ in range(4))
        reports: list[dict[str, object]] = []
        inputs: list[str] = []
        commands: list[str] = []

        async def generate(
            provider: HarnessProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            inputs.append(str(messages[-1].content))
            entered.set()
            try:
                await release.wait()
            finally:
                stopped.set()
            yield TextDoneEvent(text="Finished after native voice ended")

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "POST":
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_native_bridge"})
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            for identifier, text in (("first", "Run the first task"), ("queued", "Do not start this second task")):
                await socket.send_json(
                    {
                        "type": "delegation.created",
                        "item": {
                            "id": identifier,
                            "type": "delegation",
                            "target": "client",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    }
                )
            async for message in socket:
                command = message.json()["type"]
                commands.append(command)
                if command == "session.close":
                    await socket.send_json({"type": "session.closed"})
                    break
            return socket

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _), upstream(monkeypatch, handle):
                state = app.state.web
                root = state.selected_session_id
                bridge = MainAgentBridge(state, root, voice_session_id="native-close-call", report=reports.append)
                connection = ChatGPTLiveConnection(login_config(bridge.handle_native))
                await connection.provision(OFFER)

                async def read() -> None:
                    async for event in connection.events():
                        if event.get("delegation") == {"id": "queued", "target": "client"}:
                            queued.set()

                reader = asyncio.create_task(read())
                try:
                    async with asyncio.timeout(HANG_GUARD):
                        await entered.wait()
                        await queued.wait()
                        run = state.active
                        assert run is not None
                        assert [record["status"] for record in reports if "model_request" not in record] == [
                            "queued",
                            "working",
                            "working",
                        ]
                        await connection.close()
                        await reader
                        assert connection.finalized and commands == ["session.close"]
                        assert state.active is run and not run.task.done() and not stopped.is_set()
                        assert len(inputs) == 1
                        assert inputs[0] == native_voice_prompt(LoginHandoff("first", "Run the first task"))
                        assert not any(row["role"] == "live_caption" for row in await state.history.snapshot(root))
                        release.set()
                        await run.task
                    assert stopped.is_set() and run.outcome == "completed" and run.finished
                    assert [record["status"] for record in reports if "model_request" not in record] == [
                        "queued",
                        "working",
                        "working",
                        "completed",
                    ]
                    assert reports[-1]["result_text"] == "Finished after native voice ended"
                    assert len(inputs) == 1 and commands == ["session.close"]
                    rows = await state.history.snapshot(root)
                    assert [row["role"] for row in rows] == ["user", "assistant"]
                    assert rows[0]["content"] == "Run the first task"
                    assert rows[0]["voice_session_id"] == "native-close-call" and rows[0]["voice_verified"]
                finally:
                    release.set()
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                    await connection.aclose()

    asyncio.run(scenario())


def test_queued_native_request_is_inspectable_before_the_main_assistant_slot_is_available(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            queued = asyncio.Event()
            record = _Record()

            def report(update: dict[str, object]) -> None:
                record.delegation(update)
                queued.set()

            request = LoginHandoff("native-pending", "The new requested action", speech("An older request"))
            bridge = MainAgentBridge(
                state, state.selected_session_id, voice_session_id=record.identifier, report=report
            )
            with state.idle():
                task = asyncio.create_task(bridge.handle_native(request))
                try:
                    async with asyncio.timeout(HANG_GUARD):
                        await queued.wait()
                    assert state.active is None and await state.harness.history() == []
                    item = next(iter(record.delegations.values()))
                    assert item["status"] == "queued" and item["run_id"] == ""
                    details = next(iter(record.delegation_details.values())).request
                    assert details["input"]["text"] == native_voice_prompt(request)
                    assert details["transcript"]["text"] == speech("An older request")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert next(iter(record.delegations.values()))["status"] == "cancelled"
            assert state.active is None and await state.harness.history() == []

    asyncio.run(scenario())


def test_voice_request_cancelled_before_admission_has_no_assistant_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        reports: list[dict[str, object]] = []
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            with state.idle():
                waiting = asyncio.create_task(
                    MainAgentBridge(state, state.selected_session_id, report=reports.append).handle(speech("Wait"))
                )
                await asyncio.sleep(0.02)
                assert [report["status"] for report in reports if "model_request" not in report] == ["queued"]
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            assert [report["status"] for report in reports if "model_request" not in report] == ["queued", "cancelled"]
            assert all(report["run_id"] == "" for report in reports)
            assert all("request_input" not in report for report in reports)
            assert state.active is None and await state.harness.history() == []

    asyncio.run(scenario())


def test_voice_reply_timeout_keeps_working_status_until_actual_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nagents.web.live_bridge.VOICE_REPLY_SECONDS", 0.02)

    async def scenario() -> None:
        release = asyncio.Event()
        entered = asyncio.Event()
        reports: list[dict[str, object]] = []

        async def produce(run: Run, prompt: str) -> None:
            entered.set()
            await release.wait()
            run.final_text = "Completed after the voice wait timed out"

        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            with patch.object(state, "produce_session", produce):
                bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
                result = await bridge.handle(speech("Long request"))
                assert "still working" in result and "chat" in result
                async with asyncio.timeout(5):
                    await entered.wait()
                run = state.active
                assert run is not None and not run.task.done()
                assert [report["status"] for report in reports if "model_request" not in report] == [
                    "queued",
                    "working",
                    "working",
                ]
                release.set()
                await run.task
                assert state.active is None and run.outcome == "completed"
                assert [report["status"] for report in reports if "model_request" not in report] == [
                    "queued",
                    "working",
                    "working",
                    "completed",
                ]
                assert reports[-1]["run_id"] == run.id

    asyncio.run(scenario())


def test_delegation_target_uses_resolved_agent_profile_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        configuration = _config(tmp_path)
        configuration.profiles = {"auditor": AgentProfile(mode="reviewer", model="profile-model")}
        reports: list[dict[str, object]] = []

        async def produce(run: Run, prompt: str) -> None:
            run.final_text = "Profile answer"

        async with client_app(tmp_path, config=configuration) as (app, _, _, _):
            state = app.state.web
            await state.harness.set_agent("auditor")
            assert state.settings.values.model != state.harness.agent.provider.model
            with patch.object(state, "produce_session", produce):
                assert (
                    await MainAgentBridge(state, state.selected_session_id, report=reports.append).handle(
                        speech("Review it")
                    )
                    == "Profile answer"
                )
            assert all(
                report["agent"] == "auditor"
                and report["model"] == "profile-model"
                and report["provider"] == "anthropic"
                for report in reports
            )

    asyncio.run(scenario())


def test_voice_bridge_waits_for_host_reservation_instead_of_losing_speech(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web

            async def answer(run: Run, prompt: str) -> None:
                run.final_text = "Answered after the reservation"

            with patch.object(state, "produce_session", answer):
                with state.idle():
                    pending = asyncio.create_task(
                        MainAgentBridge(state, state.selected_session_id).handle(speech("Wait"))
                    )
                    await asyncio.sleep(0.02)
                    assert not pending.done() and state.active is None
                assert await pending == "Answered after the reservation"
                assert state.active is None

    asyncio.run(scenario())


def test_stopping_the_assistant_does_not_cancel_its_voice_waiter(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def produce(run: Run, prompt: str) -> None:
            if "first request" in prompt:
                entered.set()
                await asyncio.Event().wait()
            run.final_text = "The next voice request still works"

        async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, _):
            state = app.state.web
            bridge = MainAgentBridge(state, state.selected_session_id)
            with patch.object(state, "produce_session", produce):
                waiting = asyncio.create_task(bridge.handle(speech("first request")))
                async with asyncio.timeout(5):
                    await entered.wait()
                    run = state.active
                    assert run is not None
                    await state.stop(run)
                    assert await waiting == (
                        "The assistant request was stopped. Check the chat for any actions already completed."
                    )
                assert not waiting.cancelled() and run.finished and state.active is None
                assert await bridge.handle(speech("second request")) == "The next voice request still works"

    asyncio.run(scenario())


def test_server_websocket_delegates_two_requests_to_selected_chat_without_restarting_live(tmp_path: Path) -> None:
    """Only the upstream Live media service and chat model are fakes; the host/runtime are real."""
    seen: list[tuple[str, str, int]] = []
    commands: list[str] = []
    spoken: list[str] = []
    complete = asyncio.Event()

    async def generate(
        provider: HarnessProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        seen.append((provider.harness_config.provider_profile().kind, provider.model, len(messages)))
        yield TextDoneEvent(text=f"Reply {len(seen)} from the selected assistant")

    async def connect(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        data = await socket.receive_json()
        assert data["type"] == "session.start"
        assert data["session"]["delegation"] == {"type": "client"}
        assert data["session"]["store"] is False
        assert data["session"]["input"] == []
        await socket.send_json({"type": "session.started", "session": {"id": "native-voice"}})
        await socket.send_json(
            {"type": "session.input_transcript.delta", "delta": "First question", "start_ms": 1, "end_ms": 10}
        )
        await socket.send_json({"type": "session.delegation.created", "delegation": {"id": "d1", "target": "client"}})
        async for message in socket:
            data = message.json()
            commands.append(str(data["type"]))
            if data["type"] == "session.commentary.append":
                spoken.append(str(data["content"]))
                assert data["delegation_id"] == f"d{len(spoken)}"
                if len(spoken) == 1:
                    await socket.send_json(
                        {"type": "session.input_transcript.delta", "delta": "Follow-up", "start_ms": 11, "end_ms": 20}
                    )
                    await socket.send_json(
                        {"type": "session.delegation.created", "delegation": {"id": "d2", "target": "client"}}
                    )
                else:
                    complete.set()
            elif data["type"] == "session.close":
                await socket.send_json({"type": "session.closed", "reason": "client_close"})
                break
        return socket

    async def scenario() -> None:
        upstream = web.Application()
        upstream.router.add_get("/v1/live/sessions", connect)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}/v1"
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, harnesses):
                state = app.state.web
                root = state.selected_session_id
                before = (await client.get("/api/live/settings", headers=headers)).json()
                saved = await client.post(
                    "/api/live/settings",
                    headers=headers,
                    json={
                        "revision": before["revision"],
                        "values": {**before["values"], "enabled": True, "base_url": base},
                        "api_key": "fixture-live-key",
                    },
                )
                assert saved.status_code == 200
                created = await client.post(
                    "/api/live/sessions",
                    headers=headers,
                    json={
                        "revision": saved.json()["revision"],
                        "session_id": root,
                    },
                )
                assert created.status_code == 201
                async with asyncio.timeout(HANG_GUARD):
                    await complete.wait()
                assert spoken == ["Reply 1 from the selected assistant", "Reply 2 from the selected assistant"]
                assert seen[0][:2] == seen[1][:2] == ("anthropic", "chat-model")
                assert seen[1][2] > seen[0][2]  # One persistent chat conversation.
                snapshot = (
                    await client.get(f"/api/live/sessions/{created.json()['session_id']}", headers=headers)
                ).json()
                assert [
                    event["status"]
                    for event in snapshot["events"]
                    if event["type"] == "delegation" and not event.get("detail_type")
                ] == [
                    "queued",
                    "working",
                    "working",
                    "completed",
                    "queued",
                    "working",
                    "working",
                    "completed",
                ]
                assert len(snapshot["delegations"]) == 2
                assert len({item["delegation_id"] for item in snapshot["delegations"]}) == 2
                assert len({item["run_id"] for item in snapshot["delegations"]}) == 2
                assert all(
                    item["chat_session_id"] == root and item["status"] == "completed"
                    for item in snapshot["delegations"]
                )
                history = await harnesses[0].history()
                assert any("First question" in str(message.content) for message in history)
                assert any("Follow-up" in str(message.content) for message in history)
                closed = await client.post(
                    f"/api/live/sessions/{created.json()['session_id']}/close", headers=headers, json={}
                )
                assert closed.status_code == 200 and closed.json()["status"] == "closed"
                assert state.active is None and harnesses[0].session_id == root
            assert commands.count("session.start") == 0 and commands.count("session.input_audio.append") == 0
            assert commands.count("session.close") == 1
        finally:
            await runner.cleanup()

    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        asyncio.run(scenario())


def _config(path: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace=path, data_dir=path / "data", providers=connection("anthropic", auth="api-key"), model="chat-model"
    )
