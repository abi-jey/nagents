"""Framework-level remote task stream reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from a2a.types import Artifact
from a2a.types import Message
from a2a.types import Part
from a2a.types import Role
from a2a.types import StreamResponse
from a2a.types import Task
from a2a.types import TaskArtifactUpdateEvent
from a2a.types import TaskState
from a2a.types import TaskStatus
from a2a.types import TaskStatusUpdateEvent

from nagents.a2a.client import RemoteAgentError
from nagents.a2a.conversation import RemoteConversation

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class RemoteStub:
    def __init__(self, *, task: Task, events: tuple[StreamResponse, ...] = ()) -> None:
        self.task = task
        self.events = events
        self.sent: list[object] = []
        self.subscriptions: list[str] = []

    async def send(self, request: object) -> AsyncGenerator[StreamResponse, None]:
        self.sent.append(request)
        for event in self.events:
            yield event

    async def get_task(self, task_id: str) -> Task:
        assert task_id == self.task.id
        return self.task

    async def subscribe(self, task_id: str) -> AsyncGenerator[StreamResponse, None]:
        self.subscriptions.append(task_id)
        for event in self.events:
            yield event


def task_snapshot(state: TaskState = TaskState.TASK_STATE_WORKING) -> Task:
    return Task(
        id="remote-task-1",
        context_id="remote-context-1",
        status=TaskStatus(
            state=state,
            message=Message(role=Role.ROLE_AGENT, parts=[Part(text="Searching the repository")]),
        ),
        artifacts=[Artifact(artifact_id="answer", parts=[Part(text="Hello")])],
    )


@pytest.mark.asyncio
async def test_snapshots_and_artifact_deltas_merge_while_status_stays_separate() -> None:
    conversation = RemoteConversation(RemoteStub(task=task_snapshot()))  # type: ignore[arg-type]

    snapshot = conversation.accept(StreamResponse(task=task_snapshot()))
    status = conversation.accept(
        StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id="remote-task-1",
                context_id="remote-context-1",
                status=TaskStatus(
                    state=TaskState.TASK_STATE_WORKING,
                    message=Message(role=Role.ROLE_AGENT, parts=[Part(text="Reading files")]),
                ),
            )
        )
    )
    delta = conversation.accept(
        StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id="remote-task-1",
                context_id="remote-context-1",
                artifact=Artifact(artifact_id="answer", parts=[Part(text=" world")]),
                append=True,
            )
        )
    )

    assert snapshot.text == "Hello"
    assert status.text == "Hello"
    assert status.status_text == "Reading files"
    assert delta.text == "Hello world"
    assert delta.status_text == ""
    assert conversation.text == "Hello world"


@pytest.mark.asyncio
async def test_resume_reads_snapshot_then_subscribes_for_active_task() -> None:
    remote = RemoteStub(
        task=task_snapshot(),
        events=(
            StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    task_id="remote-task-1",
                    context_id="remote-context-1",
                    artifact=Artifact(artifact_id="answer", parts=[Part(text=" world")]),
                    append=True,
                )
            ),
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="remote-task-1",
                    context_id="remote-context-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            ),
        ),
    )
    conversation = RemoteConversation(remote, task_id="remote-task-1", context_id="remote-context-1")  # type: ignore[arg-type]

    updates = [update async for update in conversation.resume()]

    assert [update.text for update in updates] == ["Hello", "Hello world", "Hello world"]
    assert updates[-1].terminal
    assert remote.subscriptions == ["remote-task-1"]


@pytest.mark.parametrize(
    "state",
    [TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_FAILED],
)
@pytest.mark.asyncio
async def test_resume_of_terminal_task_does_not_subscribe(state: TaskState) -> None:
    remote = RemoteStub(task=task_snapshot(state))
    conversation = RemoteConversation(remote, task_id="remote-task-1", context_id="remote-context-1")  # type: ignore[arg-type]

    updates = [update async for update in conversation.resume()]

    assert len(updates) == 1
    assert updates[0].terminal
    assert remote.subscriptions == []


@pytest.mark.asyncio
async def test_send_uses_one_stable_message_id_and_never_replays_after_failure() -> None:
    class FailingRemote(RemoteStub):
        async def send(self, request: object) -> AsyncGenerator[StreamResponse, None]:
            self.sent.append(request)
            yield StreamResponse(task=task_snapshot())
            raise OSError("private transport detail")

    remote = FailingRemote(task=task_snapshot())
    conversation = RemoteConversation(remote)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="private transport detail"):
        _ = [update async for update in conversation.send("inspect this", message_id="stable-message")]

    assert len(remote.sent) == 1
    sent = remote.sent[0]
    assert sent.message.message_id == "stable-message"  # type: ignore[attr-defined]
    assert sent.message.parts[0].text == "inspect this"  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["task", "status", "artifact"])
@pytest.mark.asyncio
async def test_events_without_remote_identity_are_rejected(kind: str) -> None:
    conversation = RemoteConversation(RemoteStub(task=task_snapshot()))  # type: ignore[arg-type]
    if kind == "task":
        event = StreamResponse(task=Task(status=TaskStatus(state=TaskState.TASK_STATE_WORKING)))
    elif kind == "status":
        event = StreamResponse(
            status_update=TaskStatusUpdateEvent(status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        )
    else:
        event = StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(artifact=Artifact(artifact_id="answer", parts=[Part(text="x")]))
        )

    with pytest.raises(RemoteAgentError, match="identity"):
        conversation.accept(event)


@pytest.mark.asyncio
async def test_exchange_recovers_transient_stream_with_one_read_and_no_resend() -> None:
    class RecoveringRemote(RemoteStub):
        def __init__(self) -> None:
            super().__init__(task=task_snapshot())
            self.reads = 0

        async def send(self, request: object) -> AsyncGenerator[StreamResponse, None]:
            self.sent.append(request)
            yield StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="remote-task-1",
                    context_id="remote-context-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
            raise RemoteAgentError("stream interrupted")

        async def get_task(self, task_id: str) -> Task:
            self.reads += 1
            return await super().get_task(task_id)

        async def subscribe(self, task_id: str) -> AsyncGenerator[StreamResponse, None]:
            self.subscriptions.append(task_id)
            yield StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id="remote-context-1",
                    artifact=Artifact(artifact_id="answer", parts=[Part(text=" world")]),
                    append=True,
                )
            )
            yield StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id="remote-context-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            )

    remote = RecoveringRemote()
    conversation = RemoteConversation(remote)  # type: ignore[arg-type]

    updates = [update async for update in conversation.exchange("work once", message_id="stable-exchange")]

    assert len(remote.sent) == 1
    assert remote.reads == 1
    assert remote.subscriptions == ["remote-task-1"]
    assert [update.text for update in updates] == ["", "Hello", "Hello world", "Hello world"]
    assert updates[-1].terminal


@pytest.mark.asyncio
async def test_exchange_stops_at_input_required_and_continues_same_task_on_next_message() -> None:
    remote = RemoteStub(
        task=task_snapshot(),
        events=(
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="remote-task-1",
                    context_id="remote-context-1",
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_INPUT_REQUIRED,
                        message=Message(role=Role.ROLE_AGENT, parts=[Part(text="Please provide a branch name")]),
                    ),
                )
            ),
        ),
    )
    conversation = RemoteConversation(remote)  # type: ignore[arg-type]

    first = [update async for update in conversation.exchange("start", message_id="request-1")]

    assert first[-1].state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert first[-1].status_text == "Please provide a branch name"
    assert len(remote.sent) == 1
    assert remote.subscriptions == []

    remote.events = (
        StreamResponse(
            status_update=TaskStatusUpdateEvent(
                task_id="remote-task-1",
                context_id="remote-context-1",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            )
        ),
    )
    second = [update async for update in conversation.exchange("main", message_id="request-2")]

    assert len(remote.sent) == 2
    assert remote.sent[1].message.task_id == "remote-task-1"  # type: ignore[attr-defined]
    assert second[-1].terminal
    assert remote.subscriptions == []
