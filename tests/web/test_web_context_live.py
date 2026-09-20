"""Live context accounting: active-run streaming replies and pinned designed agents.

The context endpoint must stay read-only and safe while a run is active. It reads
the running harness for the active session (no idle gate), folds in the bounded
uncommitted root reply, and otherwise assembles a metadata-only pinned harness for
designed sessions without starting MCP servers or issuing provider requests.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.designer.schema import STARTER
from nagents.designer.schema import parse
from nagents.designer.schema import serialize
from nagents.web.service import Run
from tests.support.channels import site
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.channels import Site


def _components(data: dict[str, object]) -> dict[str, int]:
    components = cast("list[dict[str, object]]", data["components"])
    return {str(item["key"]): cast("int", item["tokens"]) for item in components}


def _assert_sums(data: dict[str, object]) -> None:
    components = cast("list[dict[str, object]]", data["components"])
    total = cast("int", data["total_tokens"])
    assert total == sum(cast("int", item["tokens"]) for item in components)
    window = data["context_window"]
    if isinstance(window, int):
        assert data["remaining_tokens"] == window - total


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


def _publish(app: Site, source: str) -> None:
    current = app.client.get("/api/designer/designs/my-team", headers=app.headers)
    revision = current.json().get("revision", "") if current.status_code == 200 else ""
    saved = app.client.post("/api/designer/save", headers=app.headers, json={"source": source, "revision": revision})
    assert saved.status_code == 200, saved.text
    response = app.client.post(
        "/api/designer/channels", headers=app.headers, json={"source": source, "revision": saved.json()["revision"]}
    )
    assert response.status_code == 200, response.text


@pytest.mark.requires_posix
def test_idle_pinned_context_uses_configured_provider_model_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        with site(tmp_path, monkeypatch) as app:
            app.configure(auto_reply=True)
            design = parse(STARTER)
            design.channels = {"fixture": "assistant"}
            design.agents["assistant"].instructions.text = "PINNED INSTRUCTIONS"
            design.providers["primary"].model = "pinned-model"
            _publish(app, serialize(design))
            app.emit("hello pinned agent")
            app.idle()
            session = app.bindings()["chat-a"]

            # The pinned harness is assembled metadata-only: no MCP startup and no
            # provider request. Fail loudly if either is attempted.
            from nagents.designer.runtime import DesignedHarness

            async def forbidden_initialize(self: DesignedHarness, *, create_session: bool = True) -> None:
                raise AssertionError("Pinned context must not initialize the designed harness")

            async def forbidden_generate(self: object, *args: object, **kwargs: object) -> object:
                raise AssertionError("Pinned context must not issue a provider request")

            monkeypatch.setattr(DesignedHarness, "initialize", forbidden_initialize)
            monkeypatch.setattr("nagents.designer.runtime.DesignProvider.generate", forbidden_generate)

            response = app.client.get(f"/api/sessions/{session}/context", headers=app.headers)
            assert response.status_code == 200, response.text
            data = response.json()
            components = _components(data)
            assert data["provider"] == "openai_compatible"
            assert data["model"] == "pinned-model"
            # The pinned agent's own tool schemas are estimated, not the host's.
            assert components["tools"] > 0
            assert components["system_prompt"] > 0
            _assert_sums(data)

    asyncio.run(check())


@pytest.mark.requires_posix
def test_idle_pinned_context_does_not_start_mcp_or_contact_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        with site(tmp_path, monkeypatch) as app:
            app.configure(auto_reply=True)
            design = parse(STARTER)
            design.channels = {"fixture": "assistant"}
            _publish(app, serialize(design))
            app.emit("hello")
            app.idle()
            session = app.bindings()["chat-a"]

            from nagents.mcp import MCPManager

            async def forbidden_add_server(self: MCPManager, config: object) -> None:
                raise AssertionError("Pinned context must not start MCP servers")

            monkeypatch.setattr(MCPManager, "add_server", forbidden_add_server)
            response = app.client.get(f"/api/sessions/{session}/context", headers=app.headers)
            assert response.status_code == 200, response.text
            _assert_sums(response.json())

    asyncio.run(check())
