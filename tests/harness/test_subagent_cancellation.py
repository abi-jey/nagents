"""A parent stop remains a stop when owned child work also fails."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from nagents.events import ToolCallEvent
from nagents.harness.runtime import Harness
from nagents.harness.types import TaskStarted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import assert_balanced
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.followups import RunInput
    from nagents.harness.types import HarnessEvent
    from nagents.harness.types import TaskCompleted
    from nagents.harness.types import TaskMessage
    from nagents.types import Message


pytestmark = pytest.mark.requires_posix
PRIVATE_ERROR = "fake-secret-never-in-task-data"
CANCELLED_FAILURE = "Subagent cancelled; it will not be replayed. Subagent setup or execution failed."


@pytest.mark.parametrize(
    ("cancel", "failure", "repeat"),
    [
        (False, "runtime", False),
        (False, "timeout", False),
        (True, "none", False),
        (True, "runtime", False),
        (True, "timeout", False),
        (True, "runtime", True),
        (True, "timeout", True),
    ],
)
def test_parent_stop_preserves_cancellation_after_owned_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool, failure: str, repeat: bool
) -> None:
    async def scenario() -> None:
        initialized, release_init = asyncio.Event(), asyncio.Event()
        closing, release_close = asyncio.Event(), asyncio.Event()
        if not repeat:
            release_close.set()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})
            else:
                await asyncio.Event().wait()

        original_initialize, original_close = Harness.initialize, FakeProvider.close

        async def initialize(child: Harness, *, create_session: bool = True) -> None:
            await original_initialize(child, create_session=create_session)
            if child._is_subagent:
                initialized.set()
                await release_init.wait()
                if failure == "runtime":
                    raise RuntimeError(PRIVATE_ERROR)
                if failure == "timeout":
                    raise TimeoutError(PRIVATE_ERROR)

        async def close(provider: FakeProvider) -> None:
            if provider.index:
                closing.set()
                await release_close.wait()
            await original_close(provider)

        monkeypatch.setattr(Harness, "initialize", initialize)
        monkeypatch.setattr(FakeProvider, "close", close)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        stream = harness.run("Stop during child initialization")
        closers: list[asyncio.Task[None]] = []
        try:
            async with asyncio.timeout(HANG_GUARD):
                async for event in stream:
                    if not isinstance(event, TaskStarted):
                        continue
                    await initialized.wait()
                    worker = harness.tasks._workers[event.task_id]
                    if cancel:
                        closers.append(asyncio.create_task(stream.aclose()))
                        while not worker.cancelling():
                            await asyncio.sleep(0)
                    if repeat:
                        worker.cancel()
                        await asyncio.sleep(0)
                    assert not worker.done() and not providers[1].closed
                    release_init.set()
                    if repeat:
                        await closing.wait()
                        worker.cancel()
                        await asyncio.sleep(0)
                        worker.cancel()
                        await asyncio.sleep(0)
                        assert not worker.done() and not providers[1].closed
                        release_close.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    await asyncio.gather(*closers)
                    await stream.aclose()
                    break
                else:
                    pytest.fail("Child was not admitted")

            info = harness.tasks.list()[0]
            assert info.status == ("cancelled" if cancel else "failed"), info.error
            assert worker.cancelled() is cancel
            assert all(task.done() for task in harness.tasks._workers.values())
            assert providers[1].closed and providers[1].requests == []
            assert info.result == "" and info.followups == 0
            assert PRIVATE_ERROR not in repr(info)
            if cancel and failure != "none":
                assert info.error == CANCELLED_FAILURE
            elif cancel:
                assert info.error == "Subagent cancelled with its parent run; it will not be replayed."
            elif failure == "timeout":
                assert info.error == (
                    "Subagent operation timed out during setup or execution; no successful result is available."
                )
            else:
                assert info.error == "Subagent failed during setup or execution; no successful result is available."
            history = await harness.history()
            assert_balanced(history)
            if cancel:
                with pytest.raises(RuntimeError, match="cancellation"):
                    _ = [event async for event in harness.continue_task(info.id, "Do not replay stopped work")]
                assert await harness.history() == history
                assert len(providers) == 2 and providers[1].requests == []
        finally:
            release_init.set()
            release_close.set()
            await asyncio.gather(*closers, return_exceptions=True)
            await stream.aclose()
            await harness.close()
        assert all(provider.closed for provider in providers)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
def test_parent_stop_preserves_cancellation_when_child_execution_also_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                started.set()
                await asyncio.Event().wait()
            elif len(provider.requests) == 1:
                yield ToolCallEvent(id="a", name="delegate", arguments={"prompt": "Review"})
            else:
                await asyncio.Event().wait()

        original_run = Harness._run

        async def run(
            child: Harness,
            message: str,
            *,
            task_id: str = "",
            trigger: str = "human",
            notifications: tuple[TaskCompleted | TaskMessage, ...] = (),
            initial_input: RunInput | None = None,
            start_profile: str = "",
        ) -> AsyncIterator[HarnessEvent]:
            try:
                async with aclosing(
                    original_run(
                        child,
                        message,
                        task_id=task_id,
                        trigger=trigger,
                        notifications=notifications,
                        initial_input=initial_input,
                        start_profile=start_profile,
                    )
                ) as events:
                    async for event in events:
                        yield event
            except asyncio.CancelledError:
                if child._is_subagent:
                    raise failure(PRIVATE_ERROR) from None
                raise

        monkeypatch.setattr(Harness, "_run", run)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        root = asyncio.create_task(collect(harness))
        try:
            async with asyncio.timeout(HANG_GUARD):
                await started.wait()
                root.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await root
            info = harness.tasks.list()[0]
            worker = harness.tasks._workers[info.id]
            assert worker.cancelled() and info.status == "cancelled"
            assert info.error == CANCELLED_FAILURE and PRIVATE_ERROR not in repr(info)
            assert info.result == "" and info.followups == 0
            assert providers[1].closed and len(providers[1].requests) == 1
            assert_balanced(await harness.history())
            assert_balanced(await harness.task_history(info.id))
        finally:
            root.cancel()
            await asyncio.gather(root, return_exceptions=True)
            await harness.close()
        assert all(provider.closed for provider in providers)

    asyncio.run(scenario())
