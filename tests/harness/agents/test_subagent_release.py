"""Deleting one root releases its runtime tree without affecting other roots."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.types import TaskCompleted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.agents.subagents import TaskInfo
    from nagents.types import Message
    from tests.support.providers import FakeProvider


pytestmark = pytest.mark.requires_posix


def test_release_nested_tree_preserves_other_root_and_saved_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and messages[-1].role == "user" and messages[-1].content == "Create tree":
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Review"})
            elif provider.index % 2 and len(provider.requests) == 1:
                yield ToolCallEvent(id="nested", name="to_agent", arguments={"prompt": "Check details"})
            else:
                yield TextDoneEvent(text="Finished")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        try:
            async with asyncio.timeout(HANG_GUARD):
                await collect(harness, "Create tree")
                first_root = harness.session_id
                first = harness.tasks.list()
                assert len(first) == 2 and {info.depth for info in first} == {1, 2}
                first_ids = {info.session_id for info in first}
                child_histories = {
                    info._storage_id: await harness.agent.session.get_history(info._storage_id) for info in first
                }
                assert all(child_histories.values())
                second_root = await harness.new_session()
                await collect(harness, "Create tree")
                second = harness.tasks.list()
                assert len(second) == 2 and not first_ids.intersection(info.session_id for info in second)
                managers = harness.tasks
                survivor = TaskCompleted(second[0].session_id, second[0].title, "Keep this result")
                obsolete = TaskCompleted(first[0].session_id, first[0].title, "Deleted root result")
                managers._ready.extend((obsolete, survivor))
                managers._observed.extend((obsolete, survivor))
                managers._pending[second[0].session_id] = [survivor]
                managers._pending[first[0].session_id] = [obsolete]
                managers._activity_gaps.update((first[0].session_id, second[0].session_id))
                managers.notification_sources = (first[0].session_id, second[0].session_id)

                await managers.release_session(first_root)
                await managers.release_session(first_root)  # Repeating host cleanup is harmless.

                assert harness.session_id == second_root and not harness._closed
                assert managers.list() == second
                assert list(managers._ready) == list(managers._observed) == [survivor]
                assert managers._pending == {second[0].session_id: [survivor]}
                assert managers._activity_gaps == {second[0].session_id}
                assert list(managers.notification_sources) == [second[0].session_id]
                for registry in (
                    managers._infos,
                    managers._workers,
                    managers._children,
                    managers._profile_snapshots,
                    managers._model_snapshots,
                    managers._options,
                    managers._next_options,
                    managers._provider_overrides,
                    managers._next_providers,
                    managers._inputs,
                    managers._admitted,
                    managers._initial_inputs,
                ):
                    assert not first_ids.intersection(registry)
                for child_id, history in child_histories.items():
                    assert await harness.agent.session.get_history(child_id) == history
                assert all(provider.closed for provider in providers[1:]) and not providers[0].closed

                await managers.release_session(second_root)
                assert not managers._infos and not managers._children and not managers._workers
                await collect(harness, "Continue normally")
                assert not harness._closed and (await harness.history())[-1].content == "Finished"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_release_requires_root_stop_before_forgetting_active_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        child_started = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                child_started.set()
                await asyncio.Event().wait()
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Wait"})
            else:
                yield TextDoneEvent(text="Waiting for my child")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        running = asyncio.create_task(collect(harness))
        try:
            async with asyncio.timeout(HANG_GUARD):
                await child_started.wait()
                info = harness.tasks.list()[0]
                with pytest.raises(RuntimeError, match="Stop the root run"):
                    await harness.tasks.release_session(harness.session_id)
                with pytest.raises(PermissionError, match="Only the root"):
                    await harness.tasks.child(info.session_id).tasks.release_session(harness.session_id)
                assert not running.done() and not providers[1].closed
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                await harness.tasks.release_session(harness.session_id)
                assert not harness.tasks.list() and not harness.tasks._children
                assert providers[1].closed and not providers[0].closed
                with pytest.raises(ValueError, match="Unknown or unavailable child session"):
                    harness.tasks.child(info.session_id)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


def test_release_joins_surviving_worker_cleanup_despite_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Review"})
            else:
                yield TextDoneEvent(text="Finished")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        started, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def surviving_worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await finish.wait()

        worker: asyncio.Task[None] | None = None
        releasing: asyncio.Task[list[TaskInfo]] | None = None
        try:
            async with asyncio.timeout(HANG_GUARD):
                await collect(harness)
                info = harness.tasks.list()[0]
                worker = asyncio.create_task(surviving_worker())
                harness.tasks._workers[info.session_id] = worker
                await started.wait()
                releasing = asyncio.create_task(harness.tasks.release_session(harness.session_id))
                await closing.wait()
                releasing.cancel()
                await asyncio.sleep(0)
                releasing.cancel()
                await asyncio.sleep(0)
                assert not releasing.done() and info.session_id in harness.tasks._infos
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await releasing
                assert worker.cancelled()
                assert not harness.tasks._infos and not harness.tasks._workers and not harness.tasks._children
        finally:
            finish.set()
            if releasing is not None:
                await asyncio.gather(releasing, return_exceptions=True)
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())
