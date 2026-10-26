"""Capture real auxiliary provider requests without network access."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from nagents.events import TextDoneEvent
from nagents.harness.provider import HarnessProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


@dataclass
class TitleRequest:
    provider: HarnessProvider
    messages: list[Message]
    tools: list[ToolDefinition]
    config: GenerationConfig
    stream: bool


@dataclass
class TitleModel:
    reply: str = "Fix tool approvals"
    models: tuple[str, ...] = ("gpt-6.1-sol", "gpt-6-luna")
    requests: list[TitleRequest] = field(default_factory=list)
    closed: list[HarnessProvider] = field(default_factory=list)


def title_model(monkeypatch: pytest.MonkeyPatch) -> TitleModel:
    stub = TitleModel()
    close = HarnessProvider.close

    async def catalog(provider: HarnessProvider) -> list[str]:
        return list(stub.models)

    async def generate(
        provider: HarnessProvider,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        config: GenerationConfig,
        stream: bool,
    ) -> AsyncGenerator[Event, None]:
        stub.requests.append(TitleRequest(provider, messages, tools, config, stream))
        yield TextDoneEvent(text=stub.reply)

    async def closed(provider: HarnessProvider) -> None:
        stub.closed.append(provider)
        await close(provider)

    monkeypatch.setattr(HarnessProvider, "get_model_list", catalog)
    monkeypatch.setattr(HarnessProvider, "generate", generate)
    monkeypatch.setattr(HarnessProvider, "close", closed)
    return stub
