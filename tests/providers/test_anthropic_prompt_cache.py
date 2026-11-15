"""Automatic prompt caching is enabled only for the Anthropic provider."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.anthropic import AnthropicBackend
from nagents.types import Message
from tests.providers.test_gateway_provider import payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider_type", [ProviderType.ANTHROPIC, ProviderType.OPENAI_COMPATIBLE])
async def test_anthropic_automatic_cache_control(provider_type: ProviderType, stream: bool) -> None:
    provider = Provider(
        provider_type, "fake-test-key", "claude-opus-5-5", base_url="https://example.test/v1", api="messages"
    )
    body: dict[str, object] = {}

    async def streaming_response(
        url: str, request: dict[str, object], headers: dict[str, str]
    ) -> AsyncIterator[TextDoneEvent]:
        body.update(request)
        yield TextDoneEvent(text="OK")

    try:
        if stream:
            with patch.object(AnthropicBackend, "_stream_anthropic", side_effect=streaming_response):
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=True)
                ]
        else:
            with patch.object(
                provider._http, "post_json", new_callable=AsyncMock, return_value=payload("messages", "OK")
            ) as post:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=False)
                ]
                body.update(post.call_args.args[1])

        assert any(isinstance(event, TextDoneEvent) and event.text == "OK" for event in events)
        assert body["stream"] is stream
        assert body["messages"] == [{"role": "user", "content": "Hello"}]
        if provider_type == ProviderType.ANTHROPIC:
            assert body["cache_control"] == {"type": "ephemeral"}
        else:
            assert "cache_control" not in body
    finally:
        await provider.close()
