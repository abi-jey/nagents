"""Display correlation is invocation-local, including inherited child contexts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents import Agent
from nagents import SessionManager
from nagents.agent import _tool_invocation_scope
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.observation import scope
from tests.agent.test_extensions import OfflineProvider
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path


def test_real_agent_invocations_assign_fresh_identity_to_legacy_reused_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        parent = {"tool_call_id": "same", "tool_name": "inspect_scope", "tool_generation_id": "parent", "tool_index": 1}
        token = scope.set(parent)
        observed: list[dict[str, object]] = []

        async def inspect_scope() -> str:
            observed.append(dict(scope.get()))
            return "ok"

        provider = OfflineProvider(
            [
                [ToolCallEvent(id="same", name="inspect_scope", extra={"generation_id": "current", "index": 0})],
                [ToolCallEvent(id="same", name="inspect_scope")],
                [TextDoneEvent(text="finished")],
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "sessions.db"), tools=[inspect_scope], compactor=None)
        try:
            async with asyncio.timeout(HANG_GUARD):
                events = [event async for event in agent.run("Inspect both calls")]
            assert len(observed) == 2
            assert observed[0]["tool_generation_id"] == "current" and observed[0]["tool_index"] == 0
            assert observed[1]["tool_call_id"] == "same" and observed[1]["tool_name"] == "inspect_scope"
            assert str(observed[1]["tool_generation_id"]).startswith("agent-")
            assert observed[1]["tool_generation_id"] not in {"parent", "current"}
            assert observed[1]["tool_index"] == 0
            calls = [event for event in events if isinstance(event, ToolCallEvent)]
            assert calls[1].extra == {"generation_id": observed[1]["tool_generation_id"], "index": 0}
            assert {key: scope.get()[key] for key in parent} == parent
        finally:
            await agent.close()
            scope.reset(token)

    asyncio.run(scenario())


def test_cancelled_child_restores_scope_and_never_borrows_parent_generation_for_legacy_call() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        restored: list[dict[str, object]] = []
        with _tool_invocation_scope("same", "parent", {"generation_id": "parent-generation", "index": 2}):
            parent = dict(scope.get())

            async def child() -> None:
                try:
                    with _tool_invocation_scope("same", "child"):
                        assert scope.get()["tool_name"] == "child"
                        assert "tool_generation_id" not in scope.get() and "tool_index" not in scope.get()
                        entered.set()
                        await asyncio.Event().wait()
                finally:
                    restored.append(dict(scope.get()))

            task = asyncio.create_task(child())
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert restored == [parent]
            assert dict(scope.get()) == parent

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"generation_id": "", "index": 0},
        {"generation_id": "g", "index": True},
        {"generation_id": "g", "index": -1},
        {"generation_id": "g", "index": 1024},
    ],
)
def test_invalid_or_missing_generation_masks_existing_tool_scope(identity: dict[str, object]) -> None:
    with (
        _tool_invocation_scope("parent", "parent", {"generation_id": "parent", "index": 1}),
        _tool_invocation_scope("current", "current", identity),
    ):
        assert scope.get()["tool_call_id"] == "current"
        assert "tool_generation_id" not in scope.get() and "tool_index" not in scope.get()
