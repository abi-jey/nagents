"""Ownership, idempotency, CAS, and bounded persistence for A2A tasks."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from a2a.helpers import new_text_message
from a2a.server.cluster.event_stream import VersionedEvent
from a2a.server.cluster.task_store import ConcurrentTaskModificationError
from a2a.server.cluster.version import TaskVersion
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import Task
from a2a.types import TaskState
from a2a.utils.errors import InvalidParamsError
from a2a.utils.errors import TaskNotFoundError
from a2a.utils.errors import UnsupportedOperationError

from nagents.a2a.auth import Principal
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.store import SQLiteEventStream
from nagents.a2a.store import SQLiteTaskStore

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(os.name != "posix", reason="A2A SQLite permissions require POSIX")


def owner(subject: str, tenant: str = "tenant-a") -> Principal:
    return Principal("https://issuer.example", subject, tenant, frozenset({"ngn:access"}), time.time() + 3600)


def context(principal: Principal) -> ServerCallContext:
    return ServerCallContext(user=principal)


def settings(
    data_dir: Path,
    *,
    max_events_per_task: int = 256,
    max_retained_tasks: int = 4096,
    max_tasks_per_principal: int = 256,
) -> A2AConfig:
    return A2AConfig(
        public_url="https://a2a.example",
        agent="assistant",
        issuer=IssuerConfig(issuer="https://issuer.example", audience="nagents-a2a"),
        data_dir=data_dir,
        max_events_per_task=max_events_per_task,
        max_retained_tasks=max_retained_tasks,
        max_tasks_per_principal=max_tasks_per_principal,
    )


async def open_store(
    data_dir: Path,
    *,
    max_events_per_task: int = 256,
    max_retained_tasks: int = 4096,
    max_tasks_per_principal: int = 256,
) -> SQLiteTaskStore:
    store = SQLiteTaskStore(
        settings(
            data_dir,
            max_events_per_task=max_events_per_task,
            max_retained_tasks=max_retained_tasks,
            max_tasks_per_principal=max_tasks_per_principal,
        )
    )
    await store.initialize()
    return store


def request(
    message_id: str, text: str = "Find one result", *, task_id: str = "", context_id: str = ""
) -> SendMessageRequest:
    message = new_text_message(text, role=Role.ROLE_USER)
    message.message_id = message_id
    message.task_id = task_id
    message.context_id = context_id
    return SendMessageRequest(message=message)


def task_copy(task: Task, state: TaskState) -> Task:
    result = Task()
    result.CopyFrom(task)
    result.status.state = state
    result.status.timestamp.GetCurrentTime()
    return result


def test_reservation_issues_distinct_ids_and_retries_idempotently(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice = owner("alice")
        first = await store.reserve(request("message-1"), alice)
        assert first.duplicate is False
        assert first.task_id != first.context_id
        assert first.task_id != await store.session_id(first.context_id, alice)
        assert (await store.session_id(first.context_id, alice)).startswith("ngn-")

        retry = await store.reserve(request("message-1"), alice)
        assert retry.duplicate is True
        assert (retry.task_id, retry.context_id) == (first.task_id, first.context_id)
        assert (await store.list(ListTasksRequest(), context(alice))).total_size == 1

        with pytest.raises(InvalidParamsError):
            await store.reserve(request("message-1", "different content"), alice)
        assert (await store.list(ListTasksRequest(), context(alice))).total_size == 1

    asyncio.run(scenario())


def test_concurrent_duplicate_reservations_commit_only_one_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice = owner("alice")
        claims = await asyncio.gather(*(store.reserve(request("same-message"), alice) for _ in range(8)))
        assert len({claim.task_id for claim in claims}) == 1
        assert len({claim.context_id for claim in claims}) == 1
        assert sum(not claim.duplicate for claim in claims) == 1
        assert (await store.list(ListTasksRequest(), context(alice))).total_size == 1

    asyncio.run(scenario())


def test_task_context_and_listing_are_owner_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice, bob = owner("alice"), owner("bob")
        claim = await store.reserve(request("alice-message"), alice)
        assert await store.get(claim.task_id, context(bob)) is None
        with pytest.raises(TaskNotFoundError):
            await store.session_id(claim.context_id, bob)
        assert (await store.list(ListTasksRequest(), context(bob))).total_size == 0
        with pytest.raises(InvalidParamsError):
            await store.reserve(request("bob-context", context_id=claim.context_id), bob)
        with pytest.raises(TaskNotFoundError):
            await store.reserve(request("bob-task", task_id=claim.task_id), bob)

        alice_task = await store.get(claim.task_id, context(alice))
        assert alice_task is not None
        with pytest.raises(TaskNotFoundError):
            await store.save(
                alice_task.task,
                event=None,
                prev=alice_task.task,
                prev_version=alice_task.version,
                context=context(bob),
            )
        assert (await store.list(ListTasksRequest(), context(alice))).total_size == 1

    asyncio.run(scenario())


def test_stale_nonterminal_writer_loses_optimistic_cas(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice = owner("alice")
        claim = await store.reserve(request("cas-message"), alice)
        call = context(alice)
        original = await store.get(claim.task_id, call)
        assert original is not None
        current = task_copy(original.task, TaskState.TASK_STATE_WORKING)
        version = await store.save(
            current, event=current, prev=original.task, prev_version=original.version, context=call
        )
        stale = task_copy(original.task, TaskState.TASK_STATE_INPUT_REQUIRED)
        with pytest.raises(ConcurrentTaskModificationError):
            await store.save(stale, event=stale, prev=original.task, prev_version=original.version, context=call)
        stored = await store.get(claim.task_id, call)
        assert stored is not None
        assert stored.version == version
        assert stored.task.status.state == TaskState.TASK_STATE_WORKING

    asyncio.run(scenario())


def test_cancellation_wins_nonterminal_race_but_cannot_cancel_terminal_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice = owner("alice")
        call = context(alice)
        active = await store.reserve(request("active-task"), alice)
        original = await store.get(active.task_id, call)
        assert original is not None
        working = task_copy(original.task, TaskState.TASK_STATE_WORKING)
        await store.save(working, event=working, prev=original.task, prev_version=original.version, context=call)
        stale_cancel = task_copy(original.task, TaskState.TASK_STATE_CANCELED)
        canceled_version = await store.save(
            stale_cancel,
            event=stale_cancel,
            prev=original.task,
            prev_version=original.version,
            context=call,
        )
        canceled = await store.get(active.task_id, call)
        assert canceled is not None
        assert canceled.version == canceled_version
        assert canceled.task.status.state == TaskState.TASK_STATE_CANCELED

        completed_claim = await store.reserve(request("completed-task"), alice)
        completed_before = await store.get(completed_claim.task_id, call)
        assert completed_before is not None
        completed = task_copy(completed_before.task, TaskState.TASK_STATE_COMPLETED)
        await store.save(
            completed,
            event=completed,
            prev=completed_before.task,
            prev_version=completed_before.version,
            context=call,
        )
        late_cancel = task_copy(completed_before.task, TaskState.TASK_STATE_CANCELED)
        with pytest.raises(ConcurrentTaskModificationError):
            await store.save(
                late_cancel,
                event=late_cancel,
                prev=completed_before.task,
                prev_version=completed_before.version,
                context=call,
            )
        terminal = await store.get(completed_claim.task_id, call)
        assert terminal is not None and terminal.task.status.state == TaskState.TASK_STATE_COMPLETED

    asyncio.run(scenario())


def test_pagination_token_binds_owner_filters_and_page_size(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice, bob = owner("alice"), owner("bob")
        alice_claims = [await store.reserve(request(f"alice-{index}"), alice) for index in range(3)]
        await store.reserve(request("bob-1"), bob)

        params = ListTasksRequest(page_size=1)
        first_page = await store.list(params, context(alice))
        assert len(first_page.tasks) == 1 and first_page.next_page_token
        next_page = ListTasksRequest(page_size=1, page_token=first_page.next_page_token)
        second_page = await store.list(next_page, context(alice))
        assert len(second_page.tasks) == 1 and second_page.next_page_token
        third_page = ListTasksRequest(page_size=1, page_token=second_page.next_page_token)
        third = await store.list(third_page, context(alice))
        assert len(third.tasks) == 1 and not third.next_page_token
        seen = {task.id for page in (first_page, second_page, third) for task in page.tasks}
        assert seen == {claim.task_id for claim in alice_claims}

        with pytest.raises(InvalidParamsError):
            await store.list(ListTasksRequest(page_size=1, page_token=first_page.next_page_token), context(bob))
        with pytest.raises(InvalidParamsError):
            await store.list(ListTasksRequest(page_size=2, page_token=first_page.next_page_token), context(alice))
        tampered = first_page.next_page_token[:-1] + ("A" if first_page.next_page_token[-1] != "A" else "B")
        with pytest.raises(InvalidParamsError):
            await store.list(ListTasksRequest(page_size=1, page_token=tampered), context(alice))
        assert (await store.list(ListTasksRequest(page_size=10), context(bob))).total_size == 1

    asyncio.run(scenario())


def test_quota_rejection_leaves_no_context_or_message_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a", max_retained_tasks=1, max_tasks_per_principal=1)
        alice = owner("alice")
        await store.reserve(request("first-message"), alice)
        orphan_candidate = uuid.UUID("00000000-0000-0000-0000-000000000123")
        with monkeypatch.context() as patcher:
            patcher.setattr("nagents.a2a.store.uuid.uuid4", lambda: orphan_candidate)
            with pytest.raises(UnsupportedOperationError):
                await store.reserve(request("quota-rejected"), alice)
            with pytest.raises(TaskNotFoundError):
                await store.session_id(orphan_candidate.hex, alice)
        assert (await store.list(ListTasksRequest(), context(alice))).total_size == 1
        store.config = replace(store.config, max_retained_tasks=2, max_tasks_per_principal=2)
        retry = await store.reserve(request("quota-rejected"), alice)
        assert not retry.duplicate
        assert (await store.list(ListTasksRequest(), context(alice))).total_size == 2

    asyncio.run(scenario())


def test_restart_marks_nonterminal_work_failed_without_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "a2a"
        store = await open_store(data_dir)
        alice = owner("alice")
        claim = await store.reserve(request("restart-message"), alice)
        await store.started(claim, alice)
        before = await store.get(claim.task_id, context(alice))
        assert before is not None and before.task.status.state == TaskState.TASK_STATE_SUBMITTED

        reopened = await open_store(data_dir)
        assert await reopened.reconcile_restart() == 1
        interrupted = await reopened.get(claim.task_id, context(alice))
        assert interrupted is not None
        assert interrupted.task.status.state == TaskState.TASK_STATE_FAILED
        assert "Pending operations were not replayed" in interrupted.task.status.message.parts[0].text
        assert interrupted.version == TaskVersion(2)
        assert await reopened.reconcile_restart() == 0

        duplicate = await reopened.reserve(request("restart-message"), alice)
        assert duplicate.duplicate
        assert duplicate.task_id == claim.task_id and duplicate.context_id == claim.context_id
        assert (await reopened.list(ListTasksRequest(), context(alice))).total_size == 1

    asyncio.run(scenario())


def test_abandon_unstarted_fails_only_a_still_submitted_unstarted_reservation(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a")
        alice = owner("alice")
        call_context = context(alice)

        unstarted = await store.reserve(request("unstarted-message"), alice)
        await store.abandon_unstarted(unstarted, alice)
        failed = await store.get(unstarted.task_id, call_context)
        assert failed is not None
        assert failed.task.status.state == TaskState.TASK_STATE_FAILED
        assert failed.version == TaskVersion(2)
        assert "No pending work was replayed" in failed.task.status.message.parts[0].text

        started = await store.reserve(request("started-message"), alice)
        await store.started(started, alice)
        started_before = await store.get(started.task_id, call_context)
        assert started_before is not None
        await store.abandon_unstarted(started, alice)
        started_after = await store.get(started.task_id, call_context)
        assert started_after is not None
        assert started_after.task.status.state == TaskState.TASK_STATE_SUBMITTED
        assert started_after.version == started_before.version

        running = await store.reserve(request("running-message"), alice)
        await store.started(running, alice)
        running_before = await store.get(running.task_id, call_context)
        assert running_before is not None
        working = task_copy(running_before.task, TaskState.TASK_STATE_WORKING)
        working_version = await store.save(
            working,
            event=working,
            prev=running_before.task,
            prev_version=running_before.version,
            context=call_context,
        )
        await store.abandon_unstarted(running, alice)
        running_after = await store.get(running.task_id, call_context)
        assert running_after is not None
        assert running_after.task.status.state == TaskState.TASK_STATE_WORKING
        assert running_after.version == working_version == TaskVersion(2)

    asyncio.run(scenario())


def test_event_stream_wakes_on_publish_and_replays_only_bounded_tail(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await open_store(tmp_path / "a2a", max_events_per_task=2)
        alice = owner("alice")
        call = context(alice)
        claim = await store.reserve(request("stream-message"), alice)
        stream = SQLiteEventStream(store)
        subscription = stream.subscribe(claim.task_id, after=TaskVersion(1))
        waiting = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0.02)

        previous = await store.get(claim.task_id, call)
        assert previous is not None
        working = task_copy(previous.task, TaskState.TASK_STATE_WORKING)
        first_version = await store.save(
            working, event=working, prev=previous.task, prev_version=previous.version, context=call
        )
        await stream.publish(claim.task_id, VersionedEvent(working, first_version))
        first_event = await asyncio.wait_for(waiting, timeout=1)
        assert first_event.version == first_version
        assert isinstance(first_event.event, Task)
        await subscription.aclose()

        versions = [first_version]
        for _ in range(3):
            previous = await store.get(claim.task_id, call)
            assert previous is not None
            update = task_copy(previous.task, TaskState.TASK_STATE_WORKING)
            versions.append(
                await store.save(update, event=update, prev=previous.task, prev_version=previous.version, context=call)
            )
        replay = SQLiteEventStream(store)
        recovered: list[VersionedEvent] = []
        subscription = replay.subscribe(claim.task_id, after=TaskVersion(0))
        try:
            async for item in subscription:
                recovered.append(item)
                if len(recovered) == 2:
                    break
        finally:
            await subscription.aclose()
        assert [item.version for item in recovered] == versions[-2:]

    asyncio.run(scenario())


def test_unsafe_directory_permissions_and_symlinked_database_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        unsafe_directory = tmp_path / "unsafe"
        unsafe_directory.mkdir(mode=0o755)
        unsafe_directory.chmod(0o755)
        unsafe_store = SQLiteTaskStore(settings(unsafe_directory))
        with pytest.raises(ValueError):
            await unsafe_store.initialize()

        private_directory = tmp_path / "private"
        private_directory.mkdir(mode=0o700)
        private_directory.chmod(0o700)
        target = tmp_path / "outside.sqlite"
        target.write_bytes(b"untouched")
        target.chmod(0o600)
        (private_directory / "tasks.db").symlink_to(target)
        symlink_store = SQLiteTaskStore(settings(private_directory))
        with pytest.raises(OSError):
            await symlink_store.initialize()
        assert target.read_bytes() == b"untouched"

    asyncio.run(scenario())
