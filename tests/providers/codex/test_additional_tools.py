"""OAuth dynamic tools use the same chronological input contract as Responses."""

import pytest
from aiohttp import web

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.provider.codex.openai import OpenAIProvider
from nagents.types import Message
from tests.providers.codex.test_openai_provider import completion
from tests.providers.codex.test_openai_provider import credentials
from tests.providers.codex.test_openai_provider import endpoint
from tests.providers.codex.test_openai_provider import text_item
from tests.providers.test_additional_tools import A
from tests.providers.test_additional_tools import B
from tests.providers.test_additional_tools import C
from tests.providers.test_gateway_provider import sse


@pytest.mark.asyncio
async def test_oauth_additions_reorder_and_revocation(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = []

    async def handle(request: web.Request) -> web.Response:
        bodies.append(await request.json())
        return web.Response(body=sse([completion([text_item("ready")])]), content_type="text/event-stream")

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials, model="gpt-6.1-sol") as provider:
        messages = [Message("user", "one")]
        for tools in ([A], [A, B], [B, A, C], [C]):
            events = [event async for event in provider.generate(messages, tools)]
            assert not any(isinstance(event, ErrorEvent) for event in events)
            messages.extend([Message("assistant", "ready"), Message("user", "next")])
        assert bodies[0]["tools"] == bodies[1]["tools"] == bodies[2]["tools"]
        assert bodies[1]["input"][-1] == {
            "type": "additional_tools",
            "role": "developer",
            "tools": [bodies[2]["input"][-4]["tools"][0]],
        }
        assert bodies[2]["input"][: len(bodies[1]["input"])] == bodies[1]["input"]
        assert bodies[2]["input"][-1]["tools"][0]["name"] == "c"
        assert [tool["name"] for tool in bodies[3]["tools"]] == ["c"]
        assert not any(item.get("type") == "additional_tools" for item in bodies[3]["input"])


@pytest.mark.parametrize("fault", ["http", "first-error", "after-created", "after-text", "arbitrary-http"])
@pytest.mark.asyncio
async def test_oauth_explicit_rejection_fallback_only_before_stream(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    bodies = []
    rejected = False
    error: dict[str, object] = {"type": "error", "error": {"message": "additional_tools is unsupported"}}

    async def handle(request: web.Request) -> web.Response:
        nonlocal rejected
        body = await request.json()
        bodies.append(body)
        if not rejected and any(item.get("type") == "additional_tools" for item in body["input"]):
            rejected = True
            if fault in {"http", "arbitrary-http"}:
                return web.json_response(
                    {"error": {"message": "additional_tools is unsupported" if fault == "http" else "Invalid schema"}},
                    status=400,
                )
            frames: list[dict[str, object] | str] = [error]
            if fault == "after-created":
                frames.insert(0, {"type": "response.created"})
            elif fault == "after-text":
                frames.insert(0, {"type": "response.output_text.delta", "delta": "ready"})
            return web.Response(body=sse(frames), content_type="text/event-stream")
        return web.Response(body=sse([completion([text_item("ready")])]), content_type="text/event-stream")

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials, model="gpt-6.1-sol") as provider:
        messages = [Message("user", "one")]
        _ = [event async for event in provider.generate(messages, [A])]
        events = [event async for event in provider.generate(messages, [A, B])]
        safe = fault in {"http", "first-error"}
        assert len(bodies) == (3 if safe else 2)
        assert any(isinstance(event, ErrorEvent) for event in events) != safe
        if safe:
            assert any(isinstance(event, TextDoneEvent) for event in events)
            assert [tool["name"] for tool in bodies[-1]["tools"]] == ["a", "b"]
            assert not any(item.get("type") == "additional_tools" for item in bodies[-1]["input"])
            _ = [event async for event in provider.generate(messages, [A, B, C])]
            assert [tool["name"] for tool in bodies[-1]["tools"]] == ["a", "b", "c"]
