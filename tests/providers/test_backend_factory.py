"""The public facade delegates generation attempts to protocol backends."""

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.provider import Provider
from nagents.provider import ProviderBackend
from nagents.provider import ProviderType
from nagents.provider.anthropic import AnthropicBackend
from nagents.provider.chat_completions import ChatCompletionsBackend
from nagents.provider.factory import create_backend
from nagents.provider.gemini import GeminiBackend
from nagents.provider.responses import ResponsesBackend
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolDefinition


@pytest.mark.parametrize(
    ("provider_type", "api", "backend_type"),
    [
        (ProviderType.ANTHROPIC, "messages", AnthropicBackend),
        (ProviderType.OPENAI_COMPATIBLE, "messages", AnthropicBackend),
        (ProviderType.OPENAI_COMPATIBLE, "responses", ResponsesBackend),
        (ProviderType.OPENROUTER, "responses", ResponsesBackend),
        (ProviderType.OPENAI_COMPATIBLE, "chat_completions", ChatCompletionsBackend),
        (ProviderType.OPENROUTER, "chat_completions", ChatCompletionsBackend),
        (ProviderType.LITELLM, "chat_completions", ChatCompletionsBackend),
        (ProviderType.GEMINI_NATIVE, "auto", GeminiBackend),
    ],
)
def test_backend_selected_by_protocol(
    provider_type: ProviderType, api: str, backend_type: type[ProviderBackend]
) -> None:
    provider = Provider(provider_type, "fake-key", "model", base_url="https://example.test/v1", api=api)
    assert isinstance(create_backend(provider), backend_type)


def test_backend_contract_is_abstract() -> None:
    assert ProviderBackend.__abstractmethods__ == {"generate"}


@pytest.mark.asyncio
async def test_facade_delegates_and_observes_route_changes() -> None:
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, "fake-key", "model", api="responses")
    messages = [Message(role="user", content="Hello")]
    config = GenerationConfig(max_tokens=10)
    calls: list[str] = []

    class RecordingBackend(ProviderBackend):
        async def generate(
            self,
            messages_arg: list[Message],
            tools: list[ToolDefinition] | None,
            config_arg: GenerationConfig | None,
            stream: bool,
        ) -> AsyncIterator[Event]:
            assert messages_arg is messages
            assert tools is None and config_arg is config and stream is False
            calls.append(self._provider.api)
            yield TextDoneEvent(text="OK")

    try:
        with patch("nagents.provider.factory.create_backend", side_effect=RecordingBackend):
            for api in ("responses", "chat_completions"):
                provider.api = api
                events = [event async for event in provider.generate(messages, config=config, stream=False)]
                assert any(isinstance(event, TextDoneEvent) and event.text == "OK" for event in events)
        assert calls == ["responses", "chat_completions"]
    finally:
        await provider.close()
