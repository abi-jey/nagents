"""Cancellation-safe ownership of bounded async work.

``nagents._async.join_owned`` must finish the owned task before propagating
cancellation, including repeated cancels, and must pass through the task's
result or exception unchanged. ``finish_on_cancel`` wraps a coroutine in an
owned task with the same guarantees.
"""

from __future__ import annotations

import asyncio

import pytest

from nagents._async import finish_on_cancel
from nagents._async import join_owned


def test_join_owned_returns_result_without_cancellation() -> None:
    async def check() -> None:
        async def work() -> str:
            await asyncio.sleep(0)
            return "completed"

        task = asyncio.create_task(work())
        assert await join_owned(task) == "completed"
        assert task.done() and not task.cancelled()

    asyncio.run(check())


def test_join_owned_propagates_task_error_unchanged() -> None:
    async def check() -> None:
        error = ValueError("owned failure")

        async def work() -> None:
            raise error

        task = asyncio.create_task(work())
        with pytest.raises(ValueError) as raised:
            await join_owned(task)
        assert raised.value is error

    asyncio.run(check())


def test_join_owned_repeated_cancellation_joins_child_before_propagating() -> None:
    async def check() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        finished = False

        async def work() -> str:
            nonlocal finished
            started.set()
            await release.wait()
            finished = True
            return "joined"

        task = asyncio.create_task(work())
        joiner = asyncio.create_task(join_owned(task))
        await started.wait()

        # Cancel repeatedly while the owned task is still running. The joiner
        # must not propagate until the child has actually finished.
        joiner.cancel()
        await asyncio.sleep(0)
        joiner.cancel()
        await asyncio.sleep(0)
        assert not joiner.done() and not finished

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await joiner
        assert finished and task.done() and not task.cancelled()

    asyncio.run(check())


def test_finish_on_cancel_joins_owned_coroutine_and_passes_result() -> None:
    async def check() -> None:
        release = asyncio.Event()
        finished = False

        async def work() -> int:
            nonlocal finished
            await release.wait()
            finished = True
            return 7

        runner = asyncio.create_task(finish_on_cancel(work()))
        await asyncio.sleep(0)
        runner.cancel()
        await asyncio.sleep(0)
        assert not runner.done() and not finished

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert finished

    asyncio.run(check())


def test_finish_on_cancel_passes_result_when_not_cancelled() -> None:
    async def check() -> None:
        async def work() -> str:
            await asyncio.sleep(0)
            return "value"

        assert await finish_on_cancel(work()) == "value"

    asyncio.run(check())


@pytest.mark.parametrize("cancellations", [1, 3])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_cancelled_owner_observes_child_without_unhandled_loop_reports(cancellations: int, outcome: str) -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        notifications: list[dict[str, object]] = []
        started, release = asyncio.Event(), asyncio.Event()
        error = ValueError("PRIVATE owned operation data must not reach the loop exception handler")

        def notified(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            notifications.append(context)

        async def work() -> str:
            started.set()
            await release.wait()
            if outcome == "error":
                raise error
            return "completed"

        loop.set_exception_handler(notified)
        child = asyncio.create_task(work())
        owner = asyncio.create_task(join_owned(child))
        try:
            await started.wait()
            for _ in range(cancellations):
                owner.cancel()
                # A scheduling checkpoint delivers each cancellation separately
                # while the explicit release barrier keeps the child unfinished.
                await asyncio.sleep(0)
                assert not owner.done() and not child.done()
                assert child.cancelling() == 0
            if outcome == "cancel":
                child.cancel()
            else:
                release.set()
            if outcome == "error":
                with pytest.raises(ValueError) as raised:
                    await owner
                assert raised.value is error and child.exception() is error
            else:
                with pytest.raises(asyncio.CancelledError):
                    await owner
                assert child.cancelled() if outcome == "cancel" else child.result() == "completed"
            assert owner.cancelling() == cancellations and child.done()
            # Finish done callbacks before restoring the previous handler. This
            # catches 3.14's cancelled-shield notification despite task.result()
            # having already consumed exactly the same child exception.
            await asyncio.sleep(0)
            assert notifications == []
        finally:
            release.set()
            await asyncio.gather(child, owner, return_exceptions=True)
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_handler)

    asyncio.run(check())
