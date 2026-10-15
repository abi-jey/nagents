"""Read-only live context accounting during root and child streaming activity."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.web.service import Run
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


def _components(data: dict[str, object]) -> dict[str, int]:
    components = cast("list[dict[str, object]]", data["components"])
    return {str(item["key"]): cast("int", item["tokens"]) for item in components}


def _assert_sums(data: dict[str, object]) -> None:
    components = cast("list[dict[str, object]]", data["components"])
    total = cast("int", data["total_tokens"])
    assert total == sum(cast("int", item["tokens"]) for item in components)
    window = data["context_window"]
    if isinstance(window, int):
        observed = data.get("observed_prompt_tokens")
        assert data["remaining_tokens"] == window - (observed if isinstance(observed, int) else total)


def test_run_context_reply_tracks_root_chunks_and_clears_on_boundaries() -> None:
    run = Run("ngn-root")
    assert run.context_reply == ""

    run.remember({"event": "text_chunk", "chunk": "first "})
    run.remember({"event": "text_chunk", "chunk": "second"})
    assert run.context_reply == "first second"

    # A child-scoped chunk must never leak into the root streaming reply.
    run.remember({"event": "text_chunk", "task_id": "child", "activation": 1, "chunk": "child draft"})
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
    run.remember({"event": "text_chunk", "task_id": "child", "activation": 1, "chunk": "child only"})
    assert run.context_reply == ""

    run.remember({"event": "text_chunk", "chunk": "x" * 300000})
    assert len(run.context_reply) == 262144


@pytest.mark.requires_posix
def test_active_run_context_reports_streaming_reply_before_completion(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (app, client, headers, harnesses):
            harness = harnesses[0]
            session_id = harness.session_id
            stream = LiveStream(app, headers, session_id, "wait")
            try:
                await stream.event("text_chunk")
                state = app.state.web
                active = state.active
                assert active is not None and active.session_id == session_id
                assert active.context_reply == "partial text"

                response = await client.get(f"/api/sessions/{session_id}/context", headers=headers)
                assert response.status_code == 200
                data = response.json()
                components = _components(data)
                assert components.get("streaming_reply", 0) > 0
                _assert_sums(data)
                # The streaming component is the bounded root reply plus overhead.
                assert components["streaming_reply"] == len("partial text") // 4 + 4
                assert data["provider"] and data["model"]
                # Read-only: the run is still active and unfinished.
                assert not stream.task.done() and state.active is active
                measured = await harness.agent.context_stats(session_id)
                assert measured.context_window is not None
                measured = replace(measured, observed_prompt_tokens=321, remaining_tokens=measured.context_window - 321)
                with patch.object(harness.agent, "context_stats", AsyncMock(return_value=measured)):
                    observed = (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()
                _assert_sums(observed)
                assert observed["observed_prompt_tokens"] == 321
                assert observed["remaining_tokens"] == measured.remaining_tokens
                assert observed["total_tokens"] > measured.total_tokens
            finally:
                active = app.state.web.active
                if active is not None:
                    await client.post("/api/cancel", json={"run_id": active.id}, headers=headers)
                    await stream.event("run_finished")
                await stream.disconnect()

    asyncio.run(check())


@pytest.mark.requires_posix
def test_active_run_context_excludes_child_text_and_clears_after_completion(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (app, client, headers, harnesses):
            harness = harnesses[0]
            session_id = harness.session_id
            stream = LiveStream(app, headers, session_id, "wait")
            try:
                await stream.event("text_chunk")
                state = app.state.web
                active = state.active
                assert active is not None
                # A child-scoped chunk arrives while the root reply is live.
                active.remember({"event": "text_chunk", "task_id": "child", "activation": 1, "chunk": "child draft"})
                assert active.context_reply == "partial text"

                live = (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()
                assert _components(live)["streaming_reply"] == len("partial text") // 4 + 4
                _assert_sums(live)

                await client.post("/api/cancel", json={"run_id": active.id}, headers=headers)
                await stream.event("run_finished")
                assert state.active is None

                idle = (await client.get(f"/api/sessions/{session_id}/context", headers=headers)).json()
                assert "streaming_reply" not in _components(idle)
                _assert_sums(idle)
            finally:
                await stream.disconnect()

    asyncio.run(check())
