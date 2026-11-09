"""Cache-friendly additions and safe fallback at the public Provider boundary."""

from collections.abc import AsyncIterator
from copy import deepcopy
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from nagents import Agent
from nagents import SessionManager
from nagents.adapters._additional_tools import AdditionalToolsUnsupported
from nagents.adapters._additional_tools import ToolTimeline
from nagents.adapters._additional_tools import rejects_additional_tools
from nagents.adapters._validation import list_data
from nagents.adapters._validation import object_data
from nagents.adapters.responses import format_request
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.http import HTTPError
from nagents.provider import Provider as BaseProvider
from nagents.provider import ProviderType
from nagents.provider.gateway import GatewayHTTPClient
from nagents.types import Message
from nagents.types import RetryConfig
from nagents.types import ToolDefinition
from tests.providers.test_gateway_provider import endpoint
from tests.providers.test_gateway_provider import payload

Provider = partial(BaseProvider, api_key="fake-test-key")


A = ToolDefinition("a", "first", {"type": "object"})
B = ToolDefinition("b", "second", {"type": "object"})
C = ToolDefinition("c", "third", {"type": "object"})


def request(messages: list[Message], tools: list[ToolDefinition]) -> dict[str, object]:
    return format_request("gpt-6-astra", messages, tools, None, False)


def inputs(body: dict[str, object]) -> list[dict[str, object]]:
    return [object_data(item) for item in list_data(body["input"])]


def added_tools(item: dict[str, object]) -> list[dict[str, object]]:
    return [object_data(tool) for tool in list_data(item["tools"])]


def test_chronological_additions_preserve_prefix_and_order() -> None:
    timeline = ToolTimeline()
    messages = [Message("user", "one")]
    first = request(messages, [A])
    timeline.apply(first)
    messages.append(Message("assistant", "answer"))
    second = request(messages, [A, C, B])
    timeline.apply(second)
    assert second["tools"] == first["tools"]
    assert inputs(second)[:-1] == request(messages, [A])["input"]
    assert [tool["name"] for tool in added_tools(inputs(second)[-1])] == ["c", "b"]
    messages.append(Message("user", "two"))
    third = request(messages, [B, A, C])
    timeline.apply(third)
    assert inputs(third)[:3] == second["input"]
    repeated = request(messages, [C, B, A])
    timeline.apply(repeated)
    assert repeated == third


@pytest.mark.parametrize("change", ["remove", "schema", "description", "compact", "edit"])
def test_invalidated_timeline_rebases_current_tools(change: str) -> None:
    timeline = ToolTimeline()
    messages = [Message("user", "one")]
    timeline.apply(request(messages, [A]))
    timeline.apply(request(messages, [A, B]))
    tools = deepcopy([A, B])
    if change == "remove":
        tools = [B]
    elif change == "schema":
        tools[0].parameters["required"] = ["new"]
    elif change == "description":
        tools[0].description = "changed"
    elif change == "compact":
        messages = []
    else:
        messages[0].content = "edited"
    body = request(messages, tools)
    expected = deepcopy(body)
    timeline.apply(body)
    assert body == expected


def test_empty_baseline_and_multiple_additions_at_same_position() -> None:
    timeline = ToolTimeline()
    messages = [Message("user", "one")]
    timeline.apply(request(messages, []))
    timeline.apply(request(messages, [A]))
    body = request(messages, [A, B])
    timeline.apply(body)
    assert "tools" not in body
    assert [added_tools(item)[0]["name"] for item in inputs(body)[1:]] == ["a", "b"]
    # Caller mutations must not rewrite previously observed schemas.
    added_tools(inputs(body)[1])[0]["description"] = "mutated"
    again = request(messages, [A, B])
    timeline.apply(again)
    assert added_tools(inputs(again)[1])[0]["description"] == "first"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_provider_fallback_is_one_time_and_route_scoped(stream: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    async with Provider(ProviderType.OPENAI_COMPATIBLE, model="gpt-6-astra", api="responses") as provider:
        bodies = []

        async def post(url: str, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
            bodies.append(deepcopy(body))
            if any(item.get("type") == "additional_tools" for item in inputs(body)):
                raise AdditionalToolsUnsupported()
            return payload("responses")

        async def chunks(url: str, body: dict[str, object], headers: dict[str, str]) -> AsyncIterator[str]:
            import json

            result = await post(url, body, headers)
            yield json.dumps({"type": "response.completed", "response": result})

        monkeypatch.setattr(provider._http, "post_json", post)
        monkeypatch.setattr(provider._http, "post_stream", chunks)
        messages = [Message("user", "one")]
        for tools in ([A], [A, B], [A, B, C]):
            events = [event async for event in provider.generate(messages, tools, stream=stream)]
            assert any(isinstance(event, TextDoneEvent) for event in events)
            assert not any(isinstance(event, ErrorEvent) for event in events)
            messages.append(Message("assistant", "Hello"))
        assert len(bodies) == 4
        assert bodies[1]["tools"] == bodies[0]["tools"]
        assert len(list_data(bodies[2]["tools"])) == 2
        assert len(list_data(bodies[3]["tools"])) == 3
        assert not any(item.get("type") == "additional_tools" for item in inputs(bodies[3]))


@pytest.mark.parametrize("failure", ["arbitrary", "partial"])
@pytest.mark.asyncio
async def test_no_feature_fallback_after_arbitrary_error_or_stream_frame(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Provider(
        ProviderType.OPENAI_COMPATIBLE, model="gpt-6-astra", api="responses", retry_config=RetryConfig(max_retries=0)
    ) as provider:
        monkeypatch.setattr(provider._http, "post_json", AsyncMock(return_value=payload("responses")))
        messages = [Message("user", "one")]
        _ = [event async for event in provider.generate(messages, [A], stream=False)]
        calls = []

        async def chunks(url: str, body: dict[str, object], headers: dict[str, str]) -> AsyncIterator[str]:
            calls.append(body)
            if failure == "partial":
                yield '{"type":"response.created"}'
                raise AdditionalToolsUnsupported()
            raise HTTPError(400, "Invalid tool schema")
            yield ""  # pragma: no cover

        monkeypatch.setattr(provider._http, "post_stream", chunks)
        events = [event async for event in provider.generate(messages, [A, B])]
        assert len(calls) == 1
        assert any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    "model,base", [("gpt-4o", None), ("unknown-model", None), ("gpt-6-astra", "https://gateway.test/v1")]
)
@pytest.mark.asyncio
async def test_unknown_models_and_routes_keep_full_tools(
    model: str, base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Provider(ProviderType.OPENAI_COMPATIBLE, model=model, base_url=base, api="responses") as provider:
        post = AsyncMock(return_value=payload("responses"))
        monkeypatch.setattr(provider._http, "post_json", post)
        messages = [Message("user", "one")]
        _ = [event async for event in provider.generate(messages, [A], stream=False)]
        _ = [event async for event in provider.generate(messages, [A, B], stream=False)]
        assert len(post.call_args.args[1]["tools"]) == 2
        assert not any(item.get("type") == "additional_tools" for item in post.call_args.args[1]["input"])


@pytest.mark.asyncio
async def test_sessions_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    async with Provider(ProviderType.OPENAI_COMPATIBLE, model="gpt-6-astra", api="responses") as provider:
        post = AsyncMock(return_value=payload("responses"))
        monkeypatch.setattr(provider._http, "post_json", post)
        messages = [Message("user", "one")]
        for session, tools in [("s1", [A]), ("s2", [A, B]), ("s1", [A, B])]:
            with provider.session_context(session):
                _ = [event async for event in provider.generate(messages, tools, stream=False)]
        assert len(post.call_args_list[1].args[1]["tools"]) == 2
        assert len(post.call_args_list[2].args[1]["tools"]) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.asyncio
async def test_transport_classifies_only_explicit_rejection(stream: bool, explicit: bool) -> None:
    async def handle(request: web.Request) -> web.Response:
        return web.json_response(
            {"error": {"message": "additional_tools is not supported" if explicit else "Invalid parameters"}},
            status=400,
        )

    body: dict[str, object] = {"input": [{"type": "additional_tools", "role": "developer", "tools": []}]}
    async with endpoint(handle) as url:
        client = GatewayHTTPClient()
        try:
            with pytest.raises(HTTPError) as caught:
                if stream:
                    _ = [chunk async for chunk in client.post_stream(url, body, {})]
                else:
                    await client.post_json(url, body, {})
            assert isinstance(caught.value, AdditionalToolsUnsupported) == explicit
        finally:
            await client.close()


@pytest.mark.parametrize(
    "error", [None, {}, {"message": "Invalid additional_tools schema"}, {"message": "unsupported model"}]
)
def test_rejection_classifier_is_narrow(error: object) -> None:
    assert not rejects_additional_tools(error)


@pytest.mark.asyncio
async def test_agent_registry_additions_reach_responses_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Provider(ProviderType.OPENAI_COMPATIBLE, model="gpt-6-astra", api="responses") as provider:
        post = AsyncMock(return_value=payload("responses"))
        monkeypatch.setattr(provider._http, "post_json", post)
        provider._model_verified = True
        agent = Agent(
            provider=provider, session_manager=SessionManager(db_path=tmp_path / "sessions.db"), streaming=False
        )
        agent.tool_registry.register(lambda: "a", name="a")
        _ = [event async for event in agent.run("one", session_id="timeline")]
        agent.tool_registry.register(lambda: "b", name="b")
        _ = [event async for event in agent.run("two", session_id="timeline")]
        assert len(post.call_args_list) == 2
        initial = post.call_args_list[0].args[1]
        added = post.call_args_list[1].args[1]
        assert added["tools"] == initial["tools"]
        assert added["input"][-1]["tools"][0]["name"] == "b"
        await agent.close()
