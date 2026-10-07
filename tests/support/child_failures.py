"""Deterministic native child-budget failure with a running descendant."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.harness import runtime
from nagents.harness.subagents import SubagentManager
from nagents.harness.tools import CodingTools
from tests.support.providers import FakeProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

    from nagents.events import Event
    from nagents.harness.config import HarnessConfig
    from nagents.harness.subagents import TaskInfo
    from nagents.harness.types import TaskCompleted
    from nagents.harness.types import TaskMessage
    from nagents.types import Message


class ChildFailureScenario:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, hold_child: bool = False, wrong_parent: bool = False
    ) -> None:
        self.providers: list[FakeProvider] = []
        self.grandchild_started = asyncio.Event()
        self.root_preview = asyncio.Event()
        self.descendant_published = asyncio.Event()
        self.allow_child_failure = asyncio.Event()
        self.never_complete = asyncio.Event()
        if not hold_child:
            self.allow_child_failure.set()

        def provider_factory(config: HarnessConfig, login_store: object = None) -> FakeProvider:
            if len(self.providers) == 1:
                config.max_tool_rounds = 1
            provider = FakeProvider(config, len(self.providers), self.script)
            self.providers.append(provider)
            return provider

        async def read_file(tools: CodingTools, path: str) -> dict[str, str]:
            assert path == "owned-barrier"
            await self.grandchild_started.wait()
            await self.root_preview.wait()
            await self.allow_child_failure.wait()
            return {"content": "The descendant is running and the root preview is observable."}

        publish = SubagentManager._publish

        def observed_publish(manager: SubagentManager, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
            previous = info.parent_session_id
            try:
                if wrong_parent and info.depth == 2:
                    info.parent_session_id = "wrong-parent-session"
                publish(manager, info, event)
            finally:
                info.parent_session_id = previous
                if info.depth == 2:
                    self.descendant_published.set()

        monkeypatch.setattr(runtime, "HarnessProvider", provider_factory)
        monkeypatch.setattr(runtime, "build_provider", lambda profile, config, auth: provider_factory(config))
        monkeypatch.setattr(runtime.Harness, "load_project_instructions", lambda self: None)
        monkeypatch.setattr(CodingTools, "read_file", read_file)
        monkeypatch.setattr(SubagentManager, "_publish", observed_publish)

    async def script(self, provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        number = len(provider.requests)
        if provider.index == 0:
            if number == 1:
                yield ToolCallEvent(id="root-parent", name="delegate", arguments={"prompt": "CHILD"})
            elif number == 2:
                # The real child cancellation diagnostic must not abandon this
                # root draft. Its own later abandonment is an explicit event.
                yield ToolCallProgressEvent(
                    generation_id="root-progress", index=0, name="read_file", arguments_text="{"
                )
                self.root_preview.set()
                await self.descendant_published.wait()
                yield ToolCallProgressEvent(
                    generation_id="root-progress", index=0, name="read_file", status="abandoned"
                )
                yield TextDoneEvent(text="Waiting for the native child outcome.")
            elif number == 3:
                assert any("tool-round limit" in str(message.content) for message in messages)
                yield ToolCallEvent(id="replacement", name="delegate", arguments={"prompt": "REPLACEMENT"})
            else:
                yield TextDoneEvent(text="ROOT_RECOVERED" if number >= 5 else "Replacement requested.")
        elif provider.index == 1:
            yield ToolCallEvent(id="descendant", name="delegate", arguments={"prompt": "DESCENDANT_PRIVATE_TASK"})
            yield ToolCallEvent(id="barrier", name="read_file", arguments={"path": "owned-barrier"})
        elif provider.index == 2:
            self.grandchild_started.set()
            yield TextChunkEvent(chunk="DESCENDANT_PRIVATE_PARTIAL")
            await self.never_complete.wait()
            raise AssertionError("Only cancellation may end the descendant")
        elif provider.index == 3:
            yield TextDoneEvent(text="REPLACEMENT_OK")
        else:
            raise AssertionError("Unexpected provider")

    def assert_recovered(self) -> None:
        assert [len(provider.requests) for provider in self.providers] == [5, 1, 1, 1]
        assert "DESCENDANT_PRIVATE" not in str(self.providers[0].requests)
        assert all(provider.closed for provider in self.providers[1:])
