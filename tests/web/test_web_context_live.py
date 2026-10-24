"""Read-only live context accounting during root and child streaming activity."""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import Event
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import Usage
from nagents.harness.config import HarnessConfig
from nagents.web.service import Run
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.provider import Provider
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


def _components(data: dict[str, object]) -> dict[str, int]:
    components = cast("list[dict[str, object]]", data["components"])
    return {str(item["key"]): cast("int", item["tokens"]) for item in components}


def _assert_sums(data: dict[str, object]) -> None:
    components = cast("list[dict[str, object]]", data["components"])
    total = cast("int", data["total_tokens"])
    assert total == sum(cast("int", item["tokens"]) for item in components)
    window = data["context_window"]
    if isinstance(window, int):
        context_tokens = cast("int", data["context_tokens"])
        assert data["remaining_tokens"] == window - context_tokens


def test_run_context_reply_tracks_root_chunks_and_clears_on_boundaries() -> None:
    run = Run("ngn-root")
    assert run.context_reply == ""

    run.remember({"event": "text_chunk", "chunk": "first "})
    run.remember({"event": "text_chunk", "chunk": "second"})
    assert run.context_reply == "first second"

    # A child-scoped chunk must never leak into the root streaming reply.
    run.remember({"event": "text_chunk", "session_id": "s1", "activation": 1, "chunk": "child draft"})
    assert run.context_reply == "first second"

    # Each boundary clears the live flag, so the streaming reply is empty even
    # though the accumulated draft chunk may still be retained for replay.
    boundaries: tuple[dict[str, object], ...] = (
        {"event": "text_done", "text": "first second"},
        {"event": "tool_call", "id": "call", "name": "read_file"},
        {"event": "tool_result", "id": "call"},
        {"event": "done"},
        {"event": "compaction_started"},
    )
    for boundary in boundaries:
        fresh = Run("ngn-root")
        fresh.remember({"event": "text_chunk", "chunk": "live"})
        assert fresh.context_reply == "live"
        fresh.remember(boundary)
        assert fresh.context_reply == "", boundary["event"]


def test_run_context_reply_is_bounded_and_ignores_child_only_streams() -> None:
    run = Run("ngn-root")
    run.remember({"event": "text_chunk", "session_id": "s1", "activation": 1, "chunk": "child only"})
    assert run.context_reply == ""

    run.remember({"event": "text_chunk", "chunk": "x" * 300000})
    assert len(run.context_reply) == 262144


@pytest.mark.requires_posix
def test_active_run_context_reports_streaming_reply_before_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
        async with client_app(tmp_path, controlled=False, config=config) as (app, client, headers, harnesses):
            harness = harnesses[0]
            session_id = harness.session_id
            provider = harness.agent.provider
            release_measured_output = asyncio.Event()
            release_completion = asyncio.Event()

            async def generate(
                _provider: Provider,
                messages: list[Message],
                tools: list[ToolDefinition] | None = None,
                config: GenerationConfig | None = None,
                stream: bool = True,
                verify_model: bool = False,
            ) -> AsyncIterator[Event]:
                assert messages and stream
                del tools, config, verify_model
                # First report only the prompt count while text is still an
                # unmeasured draft. Then send cumulative response usage whose
                # reasoning count is already included in completion tokens.
                yield TextChunkEvent(
                    chunk="partial ", usage=Usage(prompt_tokens=400, prompt_reported=True, reported=True)
                )
                await release_measured_output.wait()
                yield TextChunkEvent(
                    chunk="answer",
                    usage=Usage(
                        prompt_tokens=400,
                        completion_tokens=120,
                        total_tokens=520,
                        reasoning_tokens=80,
                        reasoning_reported=True,
                        reasoning_in_output="included",
                        reported=True,
                        prompt_reported=True,
                        completion_reported=True,
                        total_reported=True,
                    ),
                )
                await release_completion.wait()
                yield TextDoneEvent(
                    text="partial answer",
                    usage=Usage(
                        prompt_tokens=400,
                        completion_tokens=120,
                        total_tokens=520,
                        reasoning_tokens=80,
                        reasoning_reported=True,
                        reasoning_in_output="included",
                        reported=True,
                        prompt_reported=True,
                        completion_reported=True,
                        total_reported=True,
                    ),
                )

            provider.configure_context_window(1_000, {})
            monkeypatch.setattr(provider, "generate", MethodType(generate, provider))
            stream = LiveStream(app, headers, session_id, "wait")
            try:
                await stream.event("text_chunk")
                state = app.state.web
                active = state.active
                assert active is not None and active.session_id == session_id
                assert active.context_reply == "partial "

                response = await client.get(f"/api/sessions/{session_id}/context", headers=headers)
                assert response.status_code == 200
                partial = response.json()
                components = _components(partial)
                assert components.get("streaming_reply", 0) > 0
                _assert_sums(partial)
                assert partial["observed_prompt_tokens"] == 400
                assert partial["observed_completion_tokens"] == 0
                assert partial["observed_context_tokens"] == 400
                assert partial["context_tokens_source"] == "mixed"
                assert partial["context_tokens"] == 400 + partial["pending_context_tokens"]
                assert partial["provider"] and partial["model"]
                assert not stream.task.done() and state.active is active

                release_measured_output.set()
                await stream.event("text_chunk")
                observed = (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()
                _assert_sums(observed)
                assert observed["observed_prompt_tokens"] == 400
                assert observed["observed_completion_tokens"] == 120
                assert observed["observed_reasoning_tokens"] == 80
                assert observed["observed_reasoning_in_output"] == "included"
                assert observed["observed_context_tokens"] == 520
                assert observed["pending_context_tokens"] == 0
                assert observed["context_tokens"] == 520
                assert observed["remaining_tokens"] == 480
                assert observed["context_tokens_source"] == "provider"
                assert observed["context_tokens"] != 400 + 120 + 80

                release_completion.set()
                await stream.event("run_finished")
                assert (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()[
                    "observed_context_tokens"
                ] == 520
            finally:
                active = app.state.web.active
                if active is not None:
                    release_measured_output.set()
                    release_completion.set()
                    await client.post("/api/cancel", json={"run_id": active.id}, headers=headers)
                    await stream.event("run_finished")
                await stream.disconnect()

    asyncio.run(check())


@pytest.mark.requires_posix
def test_active_run_context_excludes_child_text_and_clears_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
        async with client_app(tmp_path, controlled=False, config=config) as (app, client, headers, harnesses):
            harness = harnesses[0]
            session_id = harness.session_id
            provider = harness.agent.provider
            never = asyncio.Event()

            async def generate(
                _provider: Provider,
                messages: list[Message],
                tools: list[ToolDefinition] | None = None,
                config: GenerationConfig | None = None,
                stream: bool = True,
                verify_model: bool = False,
            ) -> AsyncIterator[Event]:
                assert messages and stream
                del tools, config, verify_model
                yield TextChunkEvent(
                    chunk="partial text", usage=Usage(prompt_tokens=400, prompt_reported=True, reported=True)
                )
                await never.wait()

            provider.configure_context_window(1_000, {})
            monkeypatch.setattr(provider, "generate", MethodType(generate, provider))
            stream = LiveStream(app, headers, session_id, "wait")
            try:
                await stream.event("text_chunk")
                state = app.state.web
                active = state.active
                assert active is not None
                # A child-scoped chunk arrives while the root reply is live.
                active.remember({"event": "text_chunk", "session_id": "s1", "activation": 1, "chunk": "child draft"})
                assert active.context_reply == "partial text"

                live = (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()
                assert _components(live)["streaming_reply"] == len("partial text") // 4 + 4
                _assert_sums(live)
                assert live["observed_prompt_tokens"] == 400
                assert live["context_tokens"] == 400 + live["pending_context_tokens"]
                assert live["pending_context_tokens"] == _components(live)["streaming_reply"]

                await client.post("/api/cancel", json={"run_id": active.id}, headers=headers)
                await stream.event("run_finished")
                assert state.active is None

                idle = (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()
                assert "streaming_reply" not in _components(idle)
                _assert_sums(idle)
            finally:
                await stream.disconnect()

    asyncio.run(check())
