"""Live inspection can attach to an existing root without capturing inactive runs."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

from aiohttp import web

from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.live.delegation import ClientDelegationRequest
from nagents.observation import observe
from nagents.observation import scope
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.live_inspection import MAX_MODEL_REQUESTS
from nagents.web.live_inspection import model_preview
from nagents.web.live_inspection import model_requests
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from nagents.live.delegation import LiveAppendKind


def test_live_attached_during_typed_request_captures_next_voice_model_input_and_actual_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTACH_INSPECTION_TEST_KEY", "synthetic-only-credential")

    async def scenario() -> None:
        first_started, release_first, voice_spoken, detached_started = (asyncio.Event() for _ in range(4))
        received: list[str] = []
        reports: list[dict[str, object]] = []

        async def completion(request: web.Request) -> web.Response:
            received.append(await request.text())
            if len(received) == 1:
                first_started.set()
                await release_first.wait()
                answer = "Initial typed answer"
            elif len(received) == 2:
                answer = "Voice follow-up answer"
            else:
                detached_started.set()
                answer = "Answer after voice detached"
            chunk = {"choices": [{"index": 0, "delta": {"content": answer}, "finish_reason": "stop"}]}
            return web.Response(text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", content_type="text/event-stream")

        async def append(kind: LiveAppendKind, content: str, identifier: str) -> str:
            if content == "Voice follow-up answer":
                voice_spoken.set()
            return f"session.{kind}.append"

        upstream = web.Application()
        upstream.router.add_post("/v1/chat/completions", completion)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        config = HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "data",
            providers=connection(
                "openai_compatible",
                auth="api-key",
                base_url=f"http://127.0.0.1:{runner.addresses[0][1]}/v1",
                api_key_env="ATTACH_INSPECTION_TEST_KEY",
            ),
            model="fixture",
        )
        try:
            with (
                patch.object(ControlledHarness, "run", Harness.run),
                patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
                patch("nagents.web.live_inspection.model_preview", wraps=model_preview) as preview,
            ):
                async with client_app(tmp_path, config=config) as (app, _, _, _), asyncio.timeout(HANG_GUARD):
                    state = app.state.web
                    root = state.selected_session_id
                    assert not state.run_observers
                    await state.queued_inputs.submit(root, "initial-typed", "Initial typed question")
                    await first_started.wait()
                    run = state.active
                    assert run is not None and preview.call_count == 0
                    bridge = MainAgentBridge(state, root, report=reports.append)
                    bridge.attach(append)
                    try:
                        await bridge.handle_request(ClientDelegationRequest("voice-followup", "Exact caller question"))
                        release_first.set()
                        await voice_spoken.wait()
                        await run.task
                        captures = [record["model_request"] for record in reports if "model_request" in record]
                        assert len(captures) == 2
                        context, body = captures
                        assert isinstance(context, dict) and isinstance(body, dict)
                        assert [context["type"], body["type"]] == ["model_context", "http_request_body"]
                        assert context["model_call_id"] == body["model_call_id"]
                        context_payload, body_payload = context["payload"], body["payload"]
                        assert isinstance(context_payload, dict) and isinstance(body_payload, dict)
                        messages = json.loads(str(context_payload["text"]))["messages"]
                        assert "Exact caller question" in messages[-1]["content"]
                        assert body_payload["text"] == received[1]
                        assert all(
                            record["delegation_id"] == "voice-followup"
                            for record in reports
                            if "model_request" in record
                        )
                        assert "synthetic-only-credential" not in json.dumps(reports)
                        assert preview.call_count == 1
                        await bridge.close()
                        assert not state.run_observers
                        await state.queued_inputs.submit(root, "after-detach", "No voice consumer now")
                        await detached_started.wait()
                        while state.active is not None or await state.channels.store.has_pending():
                            await asyncio.sleep(0.001)
                        assert len(received) == 3 and preview.call_count == 1
                    finally:
                        release_first.set()
                        await bridge.close()
        finally:
            release_first.set()
            await runner.cleanup()

    asyncio.run(scenario())


def test_disabled_observation_does_not_spend_capture_budget_or_prevent_later_enable() -> None:
    enabled = False
    captured: list[dict[str, object]] = []
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, "synthetic-only-key", "fixture")
    token = scope.set({"session_id": "fixture-root", "model_call_id": "a" * 32, "round": 0})
    try:
        with (
            model_requests("fixture-root", provider, captured.append, enabled=lambda: enabled),
            patch("nagents.web.live_inspection.model_preview", wraps=model_preview) as preview,
        ):
            for _ in range(MAX_MODEL_REQUESTS + 1):
                observe("model_context", messages=[{"role": "user", "content": "Inactive request"}])
            assert not captured and preview.call_count == 0
            enabled = True
            observe("model_context", messages=[{"role": "user", "content": "First active request"}])
            enabled = False
            observe("model_context", messages=[{"role": "user", "content": "Detached request"}])
            enabled = True
            observe("model_context", messages=[{"role": "user", "content": "Reattached request"}])
            assert len(captured) == 2 and preview.call_count == 2
            assert all("capture_limited" not in record for record in captured)
            assert "Detached request" not in json.dumps(captured)
    finally:
        scope.reset(token)
