"""Actual model boundaries and bounded, authenticated opt-in voice inspection."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from aiohttp import web

from nagents.extensions import AgentPlugin
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.observation import observe
from nagents.observation import observer
from nagents.observation import scope
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.web.live_bridge import MainAgentBridge
from nagents.web.live_context import LiveSeed
from nagents.web.live_handoff import LoginHandoff
from nagents.web.live_inspection import MAX_CONTEXT_BYTES
from nagents.web.live_inspection import MAX_INSTRUCTION_BYTES
from nagents.web.live_inspection import MAX_MODEL_BYTES
from nagents.web.live_inspection import MAX_MODEL_PAYLOAD
from nagents.web.live_inspection import MAX_MODEL_REQUESTS
from nagents.web.live_inspection import model_preview
from nagents.web.live_inspection import model_requests
from nagents.web.live_inspection import seed_details
from nagents.web.live_runtime import LiveService
from nagents.web.live_runtime import _Call
from nagents.web.live_runtime import _Record
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.test_web_live_delegations import update

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext


def test_real_harness_captures_post_plugin_context_and_actual_provider_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setenv("INSPECTION_TEST_KEY", "synthetic-only-key")

    class Transform(AgentPlugin):
        async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
            request.messages.append(Message(role="user", content="Post-plugin model input"))
            request.tools = request.tools[:1]
            request.config = GenerationConfig(temperature=0.37)
            return request

    async def scenario() -> None:
        async def completion(request: web.Request) -> web.Response:
            received.append(await request.text())
            chunk = {"choices": [{"index": 0, "delta": {"content": "Confirmed answer"}, "finish_reason": "stop"}]}
            return web.Response(text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", content_type="text/event-stream")

        upstream = web.Application()
        upstream.router.add_post("/v1/chat/completions", completion)
        runner = web.AppRunner(upstream)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        config = HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "data",
            provider="openai_compatible",
            auth="api-key",
            model="fixture",
            base_url=f"http://127.0.0.1:{runner.addresses[0][1]}/v1",
            api_key_env="INSPECTION_TEST_KEY",
        )
        try:
            with (
                patch.object(ControlledHarness, "run", Harness.run),
                patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            ):
                async with client_app(tmp_path, config=config) as (app, client, headers, _):
                    state, service = app.state.web, cast("LiveService", app.state.live)
                    state.harness.agent.plugins.append(Transform())
                    record = _Record()
                    service._records[record.identifier] = record
                    bridge = MainAgentBridge(
                        state,
                        state.selected_session_id,
                        voice_session_id=record.identifier,
                        report=service.delegation_reporter(record.identifier),
                    )
                    assert (
                        await bridge.handle_native(LoginHandoff("native-request", "Caller input")) == "Confirmed answer"
                    )
                    identifier = next(iter(record.delegations))
                    path = f"/api/live/sessions/{record.identifier}/delegations/{identifier}"
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 200
                    assert "synthetic-only-key" not in response.text
                    details = response.json()
                    captures = details["model_requests"]
                    assert [item["type"] for item in captures] == ["model_context", "http_request_body"]
                    context = json.loads(captures[0]["payload"]["text"])
                    assert context["messages"][-1]["content"] == "Post-plugin model input"
                    assert context["tools"] and context["config"]["temperature"] == 0.37
                    assert captures[1]["payload"]["text"] == received[0]
                    assert captures[1]["segmented"] is True
                    assert captures[0]["model_call_id"] == captures[1]["model_call_id"]
                    assert all(item["round"] == 0 for item in captures)
                    assert [item["type"] for item in details["timeline"]].count("http_request_body") == 1
                    poll = await client.get(f"/api/live/sessions/{record.identifier}", headers=headers)
                    assert "Post-plugin model input" not in poll.text and "model_requests" not in poll.text
                    assert (await client.get(path)).status_code == 403
                    assert observer.get() is None
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("existing", [False, True])
def test_observer_binding_chaining_late_children_and_unrelated_http(existing: bool) -> None:
    captured: list[dict[str, object]] = []
    chained: list[str] = []
    provider = Provider(
        ProviderType.OPENAI_COMPATIBLE, model="fixture", api_key="synthetic", base_url="https://provider.invalid/v1"
    )

    async def scenario() -> None:
        previous = (lambda kind, data: chained.append(kind)) if existing else None
        token = observer.set(previous)
        scope_token = scope.set({"session_id": "outer"})
        original_scope = scope.get()
        release = asyncio.Event()

        async def late() -> None:
            await release.wait()
            observe("model_context", messages=["late"], tools=[], config=None)

        try:
            with model_requests("ngn-root", provider, captured.append):
                scope.set({"session_id": "ngn-root", "model_call_id": "a" * 32, "round": 0})
                observe("model_context", messages=["root"], tools=[], config=None)
                worker = asyncio.create_task(late())
                child_scope = scope.set({"session_id": "ngn-child", "model_call_id": "b" * 32, "round": 0})
                observe("model_context", messages=["child-secret"], tools=[], config=None)
                scope.reset(child_scope)
                observe(
                    "http_request",
                    attempt_id="c" * 32,
                    method="POST",
                    url="https://auth.invalid/token",
                    headers={"Authorization": "secret"},
                )
                observe("http_request_body", attempt_id="c" * 32, body="refresh-secret")
                observe(
                    "http_request",
                    attempt_id="d" * 32,
                    method="POST",
                    url="https://provider.invalid/v1/chat/completions",
                    headers={"Authorization": "secret"},
                )
                observe("http_request_body", attempt_id="d" * 32, body='{"messages":[]}')
                observe("http_response_body", attempt_id="d" * 32, body="response-secret")
                observe("tool_started")
                observe("http_request_body", attempt_id="d" * 32, body="tool-secret")
            release.set()
            await worker
            assert observer.get() is previous and scope.get() is original_scope
            assert len(captured) == 2
            assert "secret" not in str(captured) and "late" not in str(captured)
            assert bool(chained) is existing
            if existing:
                assert chained.count("model_context") == 3
        finally:
            observer.reset(token)
            scope.reset(scope_token)
            await provider.close()

    asyncio.run(scenario())


def test_capture_limits_keep_valid_typed_marker_and_bound_all_retained_bytes() -> None:
    record = _Record()
    record.delegation(update(record, "task", "queued"))
    record.delegation(update(record, "task", "working", "run"))
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, model="fixture", api_key="synthetic")
    scope_token = scope.set({"session_id": "ngn-chat", "model_call_id": "a" * 32, "round": 0})
    try:

        def capture(payload: dict[str, object]) -> None:
            record.delegation({**update(record, "task", "working", "run"), "model_request": payload})

        with model_requests("ngn-chat", provider, capture):
            for _ in range(MAX_MODEL_REQUESTS + 3):
                observe("model_context", messages=["🌍" * MAX_MODEL_PAYLOAD], tools=[], config=None)
        details = record.delegation_details["task"]
        assert details.model_bytes <= MAX_MODEL_BYTES
        assert 0 < len(details.model_requests) <= MAX_MODEL_REQUESTS
        assert details.model_requests_truncated
        assert all(len(str(item["payload"]).encode()) < 2 * MAX_MODEL_PAYLOAD for item in details.model_requests)
        marker = cast("list[dict[str, object]]", details.snapshot()["timeline"])[-1]
        assert marker["capture_limited"] and marker["model_call_id"] == "a" * 32 and marker["round"] == 0
        assert marker["type"] == "model_context" and "payload" not in marker
        assert "🌍" not in str(record.snapshot())
    finally:
        scope.reset(scope_token)


def test_preview_does_not_deepcopy_unbounded_objects_and_labels_omitted_sizes() -> None:
    @dataclass
    class Input:
        text: str

        def __deepcopy__(self, memo: object) -> object:
            raise AssertionError("Unbounded recursive copy")

    preview = model_preview({"messages": [Input("x" * (MAX_MODEL_PAYLOAD * 4))]}, MAX_MODEL_PAYLOAD)
    assert len(str(preview["text"]).encode()) <= MAX_MODEL_PAYLOAD
    assert preview["truncated"] and preview["characters_complete"] is False


def test_custom_provider_inspection_failure_is_nonfatal_and_observer_resets() -> None:
    class Custom:
        @property
        def base_url(self) -> str:
            raise RuntimeError("Not a network provider")

    captured: list[dict[str, object]] = []
    token = scope.set({"session_id": "ngn-chat", "model_call_id": "a" * 32, "round": 0})
    try:
        with pytest.raises(TimeoutError), model_requests("ngn-chat", cast("Provider", Custom()), captured.append):
            observe("model_context", messages=["still captured"], tools=[], config=None)
            raise TimeoutError
        assert len(captured) == 1 and observer.get() is None
    finally:
        scope.reset(token)


def test_capture_failure_does_not_suppress_the_existing_observer_or_change_scope() -> None:
    seen: list[str] = []

    def previous(kind: str, data: dict[str, object]) -> None:
        seen.append(kind)

    observer_token = observer.set(previous)
    scope_token = scope.set({"session_id": "ngn-chat", "model_call_id": "a" * 32, "round": 0})
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, model="fixture", api_key="synthetic")

    def unavailable(_payload: dict[str, object]) -> None:
        raise RuntimeError("Inspector unavailable")

    try:
        with model_requests("ngn-chat", provider, unavailable):
            observe("model_context", messages=["The provider must still run"], tools=[], config=None)
        assert seen == ["model_context"] and observer.get() is previous
        assert scope.get()["model_call_id"] == "a" * 32
    finally:
        observer.reset(observer_token)
        scope.reset(scope_token)


@pytest.mark.parametrize(
    "change",
    [
        {"type": []},
        {"model_call_id": "bad id"},
        {"round": True},
        {"type": "http_request_body", "attempt_id": "missing"},
        {"payload": {"text": "payload", "characters": True, "truncated": False}},
    ],
)
def test_malformed_capture_cannot_mutate_retained_request_details(change: dict[str, object]) -> None:
    record = _Record()
    record.delegation(update(record, "task", "queued"))
    record.delegation(update(record, "task", "working", "run"))
    before = record.delegation_details["task"].snapshot()
    payload = {
        "type": "model_context",
        "model_call_id": "a" * 32,
        "round": 0,
        "payload": {"text": "payload", "characters": 7, "truncated": False},
        **change,
    }
    record.delegation({**update(record, "task", "working", "run"), "model_request": payload})
    assert record.delegation_details["task"].snapshot() == before


def test_seed_context_is_immutable_bounded_authenticated_and_not_in_poll(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            service = cast("LiveService", app.state.live)
            item: dict[str, object] = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Exact seed text"}],
            }
            history = (item,)
            call = _Call(seed=LiveSeed(history=history, chat_session_id="ngn-original"))
            call.record.context = call.seed.report()
            service._records[call.record.identifier] = call.record
            path = f"/api/live/sessions/{call.record.identifier}/context"
            assert (await client.get(path, headers=headers)).json()["available"] is False
            service._capture_context(call, "Effective custom instructions", history)
            item["content"] = []
            service._capture_context(call, "Later instructions", ())
            response = await client.get(path, headers=headers)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            details = response.json()
            assert details["available"] and details["chat_session_id"] == "ngn-original"
            assert details["instructions"]["text"] == "Effective custom instructions"
            assert details["history"][0]["content"][0]["text"] == "Exact seed text"
            details["history"][0]["content"][0]["text"] = "Client mutation"
            assert "Client mutation" not in str(await service.context_details(call.record.identifier))
            poll = (await client.get(f"/api/live/sessions/{call.record.identifier}", headers=headers)).text
            assert "Exact seed text" not in poll and "Effective custom instructions" not in poll
            assert (await client.get(path)).status_code == 403
            assert (await client.get(path + "?after=0", headers=headers)).status_code == 400
            assert (
                await client.get(path.replace(call.record.identifier, "missing"), headers=headers)
            ).status_code == 404
            call.record.transition("closed", "Closed")
            assert (await client.get(path, headers=headers)).json()["history"]

    asyncio.run(scenario())
    large: dict[str, object] = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "🌍" * MAX_CONTEXT_BYTES}],
    }
    details = seed_details("x" * (MAX_INSTRUCTION_BYTES + 1), (large,) * 20)
    assert details["history"] == [] and details["history_truncated"]
    assert cast("dict[str, object]", details["instructions"])["truncated"]
