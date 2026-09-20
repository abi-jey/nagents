"""Scripted provider and harness doubles shared across harness/web/channel suites."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from nagents.harness import runtime
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from nagents.events import Event
    from nagents.harness.types import HarnessEvent
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition

    Script = Callable[["FakeProvider", list[Message]], AsyncIterator[Event]]


class FakeProvider(HarnessProvider):
    def __init__(self, config: HarnessConfig, index: int, script: Script) -> None:
        super().__init__(config)
        self.index = index
        self.script = script
        self.requests: list[list[Message]] = []
        self.schemas: list[list[str]] = []
        self.closed = False
        self.api_key = "fake-secret-never-in-task-data"

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        assert_balanced(messages)
        self.requests.append(copy.deepcopy(messages))
        self.schemas.append([tool.name for tool in tools or []])
        async for event in self.script(self, messages):
            yield event

    async def close(self) -> None:
        self.closed = True
        await super().close()


def assert_balanced(messages: list[Message]) -> None:
    pending: set[str] = set()
    for message in messages:
        if message.role == "tool":
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
        else:
            assert not pending, "A notification must never split a pending tool block"
            pending.update(call.id for call in message.tool_calls)
    assert not pending


def setup_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: Script, *, agent: str = "assistant"
) -> tuple[Harness, list[FakeProvider]]:
    providers: list[FakeProvider] = []

    def provider_factory(config: HarnessConfig, login_store: object | None = None) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(runtime, "HarnessProvider", provider_factory)
    profiles = {"reviewer": AgentProfile(mode="reviewer")}
    if agent == "build":
        agent = "assistant"
    config = HarnessConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "state",
        auth="api-key",
        model="fake-model",
        agent=agent,
        profiles=profiles,
    )
    harness = Harness(config)
    harness.agent.compactor = None
    return harness, providers


async def collect(harness: Harness, prompt: str = "review this") -> list[HarnessEvent]:
    return [event async for event in harness.run(prompt)]


def notifications(messages: list[Message]) -> list[Message]:
    return [
        message
        for message in messages
        if message.role == "user" and str(message.content).startswith("BACKGROUND TASK NOTIFICATION:")
    ]
