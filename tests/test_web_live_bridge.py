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
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.live_bridge import voice_prompt
from nagents.web.service import Run
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
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
            (provider.harness_config.provider, provider.model, len(messages), tuple(tool.name for tool in tools or ()))
        )
        response = f"Assistant answer {len(seen)}"
        yield TextChunkEvent(chunk=response)
        yield TextDoneEvent(text=response)

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, harnesses):
            state = app.state.web
            root = state.selected_session_id
            bridge = MainAgentBridge(state, root)
            assert await bridge.handle(speech("What is in this workspace?")) == "Assistant answer 1"
            assert await bridge.handle(speech("And a follow-up?")) == "Assistant answer 2"
            assert seen[0][:2] == ("anthropic", "chat-model")
            assert len(seen) == 2 and seen[1][2] > seen[0][2]
            assert "shell" in seen[0][3]  # This is the real Harness tool registry.
            before = (await client.get("/api/settings", headers=headers)).json()
            changed = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": before["revision"],
                    "values": {**before["values"], "provider": "gemini", "model": "next-model"},
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
            assert harnesses[0].config.provider == "anthropic"

    with (
        patch.object(ControlledHarness, "run", Harness.run),
        patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
        patch.object(HarnessProvider, "generate", generate),
    ):
        asyncio.run(scenario())


def test_ending_voice_cancels_and_joins_the_main_assistant_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        stopped = asyncio.Event()

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
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield TextDoneEvent(text="Never finished")

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, _, _, harnesses):
                state = app.state.web
                work = asyncio.create_task(
                    MainAgentBridge(state, state.selected_session_id).handle(speech("A long task"))
                )
                async with asyncio.timeout(5):
                    await entered.wait()
                run = state.active
                assert run is not None
                work.cancel()
                assert (await asyncio.gather(work, return_exceptions=True))[0].__class__ is asyncio.CancelledError
                assert stopped.is_set() and run.finished and run.outcome == "cancelled"
                assert state.active is None and harnesses[0]._busy == ""

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
        seen.append((provider.harness_config.provider, provider.model, len(messages)))
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
                async with asyncio.timeout(8):
                    await complete.wait()
                assert spoken == ["Reply 1 from the selected assistant", "Reply 2 from the selected assistant"]
                assert seen[0][:2] == seen[1][:2] == ("anthropic", "chat-model")
                assert seen[1][2] > seen[0][2]  # One persistent chat conversation.
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
        workspace=path, data_dir=path / "data", provider="anthropic", model="chat-model", auth="api-key"
    )
