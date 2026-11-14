"""Compaction continuation uses real local HTTP, never a paid provider."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents import Agent
from nagents import ModelCompaction
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.adapters._validation import list_data
from nagents.adapters._validation import object_data
from nagents.compaction import DEFAULT_COMPACTION_PROMPT
from nagents.events import CompactionDoneEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.types import Message
from nagents.types import RetryConfig
from nagents.types import ToolCall
from tests.providers.test_gateway_provider import APIS
from tests.providers.test_gateway_provider import endpoint
from tests.providers.test_gateway_provider import payload
from tests.providers.test_gateway_provider import reply

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("trigger", ["automatic", "forced", "preflight"])
def test_compaction_continues_through_tools_and_reopened_session(
    tmp_path: Path, api: str, stream: bool, trigger: str
) -> None:
    async def scenario() -> None:
        requests: list[dict[str, object]] = []
        generations: list[dict[str, object]] = []
        executed: list[str] = []
        summary = (
            "The user requested work and then inspection; neither has run yet."
            if trigger == "preflight"
            else "The user requested work. The work already completed. Inspect it next; never repeat it."
        )

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "GET":
                return web.json_response({"data": [{"id": "fixture-model"}]})
            body = object_data(await request.json())
            requests.append(body)
            wire = list_data(body["input" if api == "responses" else "messages"])
            if not wire:
                return web.json_response({"error": {"type": "invalid_request_error"}}, status=400)
            if DEFAULT_COMPACTION_PROMPT in json.dumps(body):
                assert body["stream"] is False
                return await reply(request, api, payload(api, summary), False)
            generations.append(body)
            if api == "messages":
                assert object_data(wire[0])["role"] == "user"
            elif api == "chat_completions":
                # Some gateways reject an assistant-only/prefill continuation.
                assert object_data(wire[-1])["role"] in {"user", "tool"}
            else:
                assert any(object_data(item).get("role") == "user" for item in wire)
            number = len(generations)
            calls = (
                (ToolCall("work-1", "work"),)
                if number == 1
                else (ToolCall("inspect-1", "inspect"),)
                if number == 2
                else ()
            )
            text = (
                "Working"
                if number == 1
                else "Inspecting"
                if number == 2
                else "Finished"
                if number == 3
                else "Follow-up"
            )
            return await reply(request, api, payload(api, text, calls), stream)

        async with endpoint(handle) as url:
            provider = Provider(
                ProviderType.LITELLM,
                "offline-fixture-not-a-secret",
                "fixture-model",
                base_url=url,
                api=api,
                context_window=1000,
                retry_config=RetryConfig(max_retries=0),
            )
            session = SessionManager(tmp_path / "continuation.db")
            agent = Agent(provider, session, streaming=stream, compaction_strategy=ModelCompaction())

            async def work() -> str:
                """Perform a local action exactly once."""
                executed.append("work")
                if trigger == "forced":
                    agent.trigger_compaction("s")
                return "Completed " + ("x" * 4000 if trigger == "automatic" else "work")

            async def inspect() -> str:
                """Inspect the completed action without repeating it."""
                executed.append("inspect")
                return "Verified"

            agent.register_tool(work)
            agent.register_tool(inspect)
            if trigger == "preflight":
                agent.trigger_compaction("s")
            try:
                events = [event async for event in agent.run("Do the work, then inspect it", session_id="s")]
                assert not [event for event in events if isinstance(event, ErrorEvent)]
                assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "Finished"
                assert sum(isinstance(event, CompactionDoneEvent) for event in events) == 1
                assert len(generations) == 3 and executed == ["work", "inspect"]
                history = await session.get_history("s")
                assert history[0] == Message(role="compaction_summary", content=summary)
                # The synthetic continuation is wire-only, never a saved user
                # request, history entry, approval, or replay of the work tool.
                assert all(message.role != "user" for message in history)
                if trigger != "preflight":
                    assert [call.name for message in history for call in message.tool_calls] == ["inspect"]
                before = len(requests)
                agent.session = SessionManager(session.db_path)
                followup = [event async for event in agent.run("Anything else?", session_id="s")]
                assert not [event for event in followup if isinstance(event, ErrorEvent)]
                assert isinstance(followup[-1], DoneEvent) and followup[-1].final_text == "Follow-up"
                assert len(requests) == before + 1 and executed == ["work", "inspect"]
                reopened = await agent.session.get_history("s")
                assert [message for message in reopened if message.role == "user"] == [
                    Message(role="user", content="Anything else?")
                ]
                assert len(requests) == 5  # Three rounds, one summary, one follow-up.
            finally:
                await agent.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", [ProviderType.LITELLM, ProviderType.ANTHROPIC])
@pytest.mark.parametrize(
    "context",
    [
        [],
        [Message(role="system", content="rules")],
        [Message(role="developer", content="rules")],
        [Message(role="compaction_summary", content="facts")],
    ],
)
def test_messages_rejects_empty_wire_conversation_before_http(
    kind: ProviderType, stream: bool, context: list[Message]
) -> None:
    async def scenario() -> None:
        received: list[str] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            received.append(request.path)
            return web.json_response({"error": {"type": "invalid_request_error"}}, status=400)

        async with endpoint(handle) as url:
            async with Provider(
                kind,
                "offline-fixture-not-a-secret",
                "fixture-model",
                base_url=url + "/v1",
                api="messages",
                retry_config=RetryConfig(max_retries=0),
            ) as provider:
                events = [event async for event in provider.generate(context, stream=stream)]
            assert received == []
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            assert events[0].code == "PROVIDER_PROTOCOL_ERROR" and not events[0].recoverable
            assert "at least one" in events[0].message and "message" in events[0].message

    asyncio.run(scenario())
