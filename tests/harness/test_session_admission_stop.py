"""Host starts own their mandatory emission through backpressure and shutdown."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.followups import RunInput
from nagents.harness.subagent_settings import SubagentLimits
from nagents.harness.types import Notice
from nagents.harness.types import TaskStarted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.followups import RunTurn
    from nagents.harness.runtime import Harness
    from nagents.harness.types import HarnessEvent
    from nagents.types import ContentPart
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.usefixtures("workspace_discovery_only")


async def finished(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="Completed")


def fill_queue(harness: Harness) -> None:
    queue: asyncio.Queue[HarnessEvent | RunTurn] = asyncio.Queue(maxsize=1)
    queue.put_nowait(Notice("Occupied root queue"))
    harness._queue = queue


def assert_no_emission_tasks() -> None:
    assert not [
        task for task in asyncio.all_tasks() if not task.done() and task.get_name().startswith("ngn-agent-start-")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["end", "close"])
async def test_full_queue_stop_releases_host_start_and_guards_root_until_owned_cleanup_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: str,
) -> None:
    harness, providers = setup_harness(tmp_path, monkeypatch, finished)
    await harness.initialize()
    harness.tasks.begin(harness.session_id)
    original_root = harness.session_id
    fill_queue(harness)
    entered, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_emit = harness.emit

    async def emit(event: HarnessEvent) -> None:
        if isinstance(event, TaskStarted):
            entered.set()
            try:
                await original_emit(event)
            finally:
                cleaning.set()
                await release_cleanup.wait()
        else:
            await original_emit(event)

    monkeypatch.setattr(harness, "emit", emit)
    pending = asyncio.create_task(harness.tasks.to_agent("Must not execute"))
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        assert harness.tasks._running()
        assert harness.tasks.usage()["occupied_slots"] == 1
        assert harness.tasks._starting == harness.tasks._new_starting == 0
        assert all(not worker.done() for worker in harness.tasks._workers.values())
        if shutdown == "close":
            await asyncio.wait_for(harness.close(), HANG_GUARD)
        else:
            await asyncio.wait_for(harness.tasks.end(), HANG_GUARD)
        await asyncio.wait_for(cleaning.wait(), HANG_GUARD)
        with pytest.raises(RuntimeError, match="admission is still pending"):
            harness.tasks.begin(original_root, reset_budget=True)
        assert not pending.done(), "Shutdown does not abandon the owned emission cleanup"
        assert len(providers) == 1 and not providers[0].requests
        release_cleanup.set()
        with pytest.raises(RuntimeError, match="start stopped"):
            await asyncio.wait_for(pending, HANG_GUARD)
        assert not harness.tasks._start_admissions
        assert harness.tasks._used == 0
        assert harness.tasks.usage()["occupied_slots"] == 0
        assert all(info.status == "cancelled" for info in harness.tasks.list())
        assert_no_emission_tasks()
        if shutdown == "end":
            harness._queue = None
            new_root = await harness.new_session()
            harness.tasks.begin(new_root, reset_budget=True)
            accepted = await harness.tasks.to_agent("The new root's own work")
            await asyncio.wait_for(harness.tasks._workers[accepted["session_id"]], HANG_GUARD)
            assert all(info.root_session_id == new_root for info in harness.tasks.list())
            assert len(providers) == 2 and len(providers[1].requests) == 1
            assert harness.tasks._used == 1
    finally:
        release_cleanup.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        harness._queue = None
        await harness.tasks.end()
        await harness.close()
        assert_no_emission_tasks()


@pytest.mark.asyncio
async def test_cancelling_one_host_start_joins_its_emission_without_stopping_other_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, providers = setup_harness(tmp_path, monkeypatch, finished)
    await harness.initialize()
    harness.tasks.begin(harness.session_id)
    fill_queue(harness)
    entered = asyncio.Event()
    original_emit = harness.emit

    async def emit(event: HarnessEvent) -> None:
        if isinstance(event, TaskStarted):
            entered.set()
        await original_emit(event)

    monkeypatch.setattr(harness, "emit", emit)
    pending = asyncio.create_task(harness.tasks.to_agent("Cancelled admission"))
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, HANG_GUARD)
        assert not harness.tasks._start_admissions and not harness.tasks._stopped.is_set()
        assert harness.tasks._used == 0
        assert_no_emission_tasks()
        harness._queue = None
        accepted = await harness.tasks.to_agent("Independent accepted work")
        await asyncio.wait_for(harness.tasks._workers[accepted["session_id"]], HANG_GUARD)
        assert len(providers) == 2 and len(providers[1].requests) == 1
        assert harness.tasks._used == 1
    finally:
        harness._queue = None
        await harness.tasks.end()
        await harness.close()


@pytest.mark.asyncio
async def test_parent_shutdown_releases_its_blocked_descendant_start_without_stopping_the_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_started = asyncio.Event()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if messages[-1].content == "Parent waiting":
            parent_started.set()
            await asyncio.Event().wait()
        yield TextDoneEvent(text="Sibling completed")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    await harness.initialize()
    harness.tasks.begin(harness.session_id)
    parent_ack = await harness.tasks.to_agent("Parent waiting")
    parent_id = parent_ack["session_id"]
    await asyncio.wait_for(parent_started.wait(), HANG_GUARD)
    parent = harness.tasks.child(parent_id)
    fill_queue(harness)
    entered = asyncio.Event()
    original_emit = harness.emit

    async def emit(event: HarnessEvent) -> None:
        if isinstance(event, TaskStarted) and event.parent_session_id == parent_id:
            entered.set()
        await original_emit(event)

    monkeypatch.setattr(harness, "emit", emit)
    pending = asyncio.create_task(parent.tasks.to_agent("Nested work must not execute"))
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        await asyncio.wait_for(parent.close(), HANG_GUARD)
        with pytest.raises(RuntimeError, match="start stopped"):
            await asyncio.wait_for(pending, HANG_GUARD)
        await asyncio.wait_for(asyncio.gather(harness.tasks._workers[parent_id], return_exceptions=True), HANG_GUARD)
        assert harness.tasks._active and not harness.tasks._stopped.is_set()
        assert not harness.tasks._start_admissions
        assert harness.tasks._used == 1
        assert len(providers) == 2
        descendants = [info for info in harness.tasks.list() if info.parent_session_id == parent_id]
        assert len(descendants) == 1 and descendants[0].status == "cancelled"
        assert harness.tasks._workers[descendants[0].session_id].cancelled()
        harness._queue = None
        sibling = await harness.tasks.to_agent("Independent sibling")
        await asyncio.wait_for(harness.tasks._workers[sibling["session_id"]], HANG_GUARD)
        assert len(providers) == 3 and len(providers[2].requests) == 1
        assert_no_emission_tasks()
    finally:
        harness._queue = None
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await harness.tasks.end()
        await harness.close()


@pytest.mark.asyncio
async def test_parallel_blocked_starts_count_each_slot_once_and_refund_only_unstarted_executions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, providers = setup_harness(tmp_path, monkeypatch, finished)
    await harness.initialize()
    harness.tasks.accept_limits(SubagentLimits(2, 4, 2))
    harness.tasks.begin(harness.session_id)
    fill_queue(harness)
    one_entered, two_entered = asyncio.Event(), asyncio.Event()
    emitted = 0
    original_emit = harness.emit

    async def emit(event: HarnessEvent) -> None:
        nonlocal emitted
        if isinstance(event, TaskStarted):
            emitted += 1
            one_entered.set()
            if emitted == 2:
                two_entered.set()
        await original_emit(event)

    monkeypatch.setattr(harness, "emit", emit)
    first = asyncio.create_task(harness.tasks.to_agent("First pending start"))
    pending = [first]
    try:
        await asyncio.wait_for(one_entered.wait(), HANG_GUARD)
        assert harness.tasks.usage()["occupied_slots"] == 1
        second = asyncio.create_task(harness.tasks.to_agent("Second pending start"))
        pending.append(second)
        await asyncio.wait_for(two_entered.wait(), HANG_GUARD)
        assert harness.tasks.usage()["occupied_slots"] == 2
        assert len(harness.tasks._start_admissions) == 2
        assert harness.tasks._used == 2
        with pytest.raises(ValueError, match="concurrently"):
            await harness.tasks.to_agent("No remaining slot")
        assert len(harness.tasks._infos) == 2
        await asyncio.wait_for(harness.tasks.end(), HANG_GUARD)
        outcomes = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), HANG_GUARD)
        assert all(isinstance(outcome, RuntimeError) for outcome in outcomes)
        assert harness.tasks._used == 0
        assert harness.tasks._starting == harness.tasks._new_starting == 0
        assert not harness.tasks._start_admissions
        assert all(worker.cancelled() for worker in harness.tasks._workers.values())
        assert len(providers) == 1
        assert_no_emission_tasks()
    finally:
        harness._queue = None
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await harness.tasks.end()
        await harness.close()


@pytest.mark.asyncio
async def test_cancelling_model_producer_during_start_emission_has_no_shutdown_join_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index == 0:
            yield ToolCallEvent(id="start", name="to_agent", arguments={"prompt": "No child after Stop"})
        else:
            yield TextDoneEvent(text="Unexpected child model call")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    entered = asyncio.Event()
    original_emit = harness.emit

    async def emit(event: HarnessEvent) -> None:
        if isinstance(event, TaskStarted):
            entered.set()
            await asyncio.Event().wait()
        await original_emit(event)

    monkeypatch.setattr(harness, "emit", emit)
    run = asyncio.create_task(collect(harness))
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, HANG_GUARD)
        assert not harness.tasks._start_admissions
        assert harness.tasks._used == 0
        assert len(providers) == 1
        assert_no_emission_tasks()
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_ordinary_root_completion_waits_for_an_owned_host_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_started, finish_root = asyncio.Event(), asyncio.Event()
    started_emission, release_emission, boundary = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index == 0:
            root_started.set()
            await finish_root.wait()
            yield TextDoneEvent(text="Root response")
        else:
            yield TextDoneEvent(text="Host's child response")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    original_emit, original_notification = harness.emit, harness.tasks.notification

    async def emit(event: HarnessEvent) -> None:
        if isinstance(event, TaskStarted):
            started_emission.set()
            await release_emission.wait()
        await original_emit(event)

    async def notification(*, wait_for_tasks: bool = False) -> str | list[ContentPart] | None:
        if harness.tasks._start_admissions and finish_root.is_set():
            boundary.set()
        return await original_notification(wait_for_tasks=wait_for_tasks)

    monkeypatch.setattr(harness, "emit", emit)
    monkeypatch.setattr(harness.tasks, "notification", notification)
    run = asyncio.create_task(collect(harness))
    await asyncio.wait_for(root_started.wait(), HANG_GUARD)
    pending = asyncio.create_task(harness.tasks.start_task("Host work"))
    try:
        await asyncio.wait_for(started_emission.wait(), HANG_GUARD)
        finish_root.set()
        await asyncio.wait_for(boundary.wait(), HANG_GUARD)
        assert not run.done(), "A pending host start must keep the root notification boundary alive"
        release_emission.set()
        accepted = await asyncio.wait_for(pending, HANG_GUARD)
        await asyncio.wait_for(run, HANG_GUARD)
        assert harness.tasks._lookup(accepted.session_id).status == "completed"
        assert len(providers) == 2 and len(providers[1].requests) == 1
        assert not harness.tasks._start_admissions
        assert_no_emission_tasks()
    finally:
        finish_root.set()
        release_emission.set()
        for task in (pending, run):
            if not task.done():
                task.cancel()
        await asyncio.gather(pending, run, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_nonstring_agent_does_not_fall_back_to_the_default_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, providers = setup_harness(tmp_path, monkeypatch, finished)
    await harness.initialize()
    harness.tasks.begin(harness.session_id)
    try:
        for value in (False, None):
            with pytest.raises(ValueError, match="agent must name a profile"):
                await harness.tasks.to_agent("Invalid library argument", agent=value)  # type: ignore[arg-type]
        assert not harness.tasks._infos and not harness.tasks._start_admissions
        assert harness.tasks._used == 0 and len(providers) == 1
    finally:
        await harness.tasks.end()
        await harness.close()


@pytest.mark.asyncio
async def test_host_receipt_binding_is_installed_before_the_gated_worker_enters_its_first_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, providers = setup_harness(tmp_path, monkeypatch, finished)
    await harness.initialize()
    harness.tasks.begin(harness.session_id)
    picked_up: list[str] = []

    @asynccontextmanager
    async def receipt() -> AsyncIterator[None]:
        current, child = harness.tasks.input_child("initial-message")
        assert child.followups.current.message_id == "initial-message"
        picked_up.append(current.session_id)
        yield

    try:
        prompt = "Receipt-owned input"
        message = RunInput(prompt, identifier="input", message_id="initial-message", context=receipt)
        accepted = await harness.tasks.start_task(prompt, message=message)
        await asyncio.wait_for(harness.tasks._workers[accepted.session_id], HANG_GUARD)
        assert picked_up == [accepted.session_id]
        assert harness.tasks._lookup(accepted.session_id).status == "completed"
        assert len(providers) == 2 and len(providers[1].requests) == 1
        assert harness.tasks._used == 1
    finally:
        await harness.tasks.end()
        await harness.close()
