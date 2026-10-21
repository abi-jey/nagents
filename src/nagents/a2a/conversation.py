"""A framework-level remote conversation, independent of ngn and LLM providers."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from a2a.types import Message
from a2a.types import Part
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import StreamResponse
from a2a.types import TaskState

from .client import RemoteAgent
from .client import RemoteAgentError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import Iterable

TERMINAL_STATES = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_REJECTED,
    }
)


@dataclass(frozen=True)
class RemoteUpdate:
    task_id: str = ""
    context_id: str = ""
    state: int = TaskState.TASK_STATE_UNSPECIFIED
    text: str = ""
    status_text: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class RemoteConversation:
    """Merge A2A snapshots and artifact deltas without duplicating output.

    The caller owns persistence/admission and supplies a stable message ID.
    Network failures never replay a send. Reconnect with ``resume`` after a
    task ID is known; closing a stream does not silently cancel remote work.
    Task status text remains separate from the assistant's answer.
    """

    def __init__(self, remote: RemoteAgent, *, task_id: str = "", context_id: str = "") -> None:
        self.remote = remote
        self.task_id = task_id
        self.context_id = context_id
        self.state = TaskState.TASK_STATE_UNSPECIFIED
        self._artifacts: dict[str, str] = {}
        self._message = ""
        self.status_text = ""

    @property
    def text(self) -> str:
        return "\n\n".join(self._artifacts.values()) if self._artifacts else self._message

    @staticmethod
    def _text(parts: Iterable[Part]) -> str:
        return "".join(part.text for part in parts if part.WhichOneof("content") == "text")

    def accept(self, event: StreamResponse) -> RemoteUpdate:
        previous_task, previous_context = self.task_id, self.context_id
        kind = event.WhichOneof("payload")
        status_text = ""
        if kind == "message":
            self.context_id = event.message.context_id or self.context_id
            self.task_id = event.message.task_id or self.task_id
            self._message = self._text(event.message.parts)
            self.state = TaskState.TASK_STATE_COMPLETED
        elif kind == "task":
            task = event.task
            self.task_id, self.context_id, self.state = task.id, task.context_id, task.status.state
            for artifact in task.artifacts:
                self._artifacts[artifact.artifact_id] = self._text(artifact.parts)
            status_text = self._text(task.status.message.parts)
        elif kind == "status_update":
            update = event.status_update
            self.task_id, self.context_id, self.state = update.task_id, update.context_id, update.status.state
            status_text = self._text(update.status.message.parts)
        elif kind == "artifact_update":
            artifact_update = event.artifact_update
            self.task_id, self.context_id = artifact_update.task_id, artifact_update.context_id
            artifact = artifact_update.artifact
            previous = self._artifacts.get(artifact.artifact_id, "") if artifact_update.append else ""
            self._artifacts[artifact.artifact_id] = previous + self._text(artifact.parts)
        else:
            raise RemoteAgentError("Remote agent returned an unsupported event")
        if kind != "message" and (not self.context_id or not self.task_id):
            raise RemoteAgentError("Remote event is missing its task or context identity")
        if (previous_task and self.task_id != previous_task) or (
            previous_context and self.context_id != previous_context
        ):
            raise RemoteAgentError("Remote stream changed its accepted task or context identity")
        if len(self._artifacts) > 256 or len(self.text) > 1048576 or len(status_text) > 32768:
            raise RemoteAgentError("Remote response exceeds the supported text limit")
        self.status_text = status_text or self.status_text
        return RemoteUpdate(self.task_id, self.context_id, self.state, self.text, status_text)

    async def send(self, text: str, *, message_id: str) -> AsyncGenerator[RemoteUpdate, None]:
        if not message_id or not text.strip():
            raise ValueError("A remote send requires a message ID and nonempty text")
        self._artifacts.clear()
        self._message = ""
        self.status_text = ""
        if self.state in TERMINAL_STATES:
            self.task_id = ""
            self.state = TaskState.TASK_STATE_UNSPECIFIED
        request = SendMessageRequest(
            message=Message(
                message_id=message_id,
                context_id=self.context_id,
                task_id="" if self.state in TERMINAL_STATES else self.task_id,
                role=Role.ROLE_USER,
                parts=[Part(text=text)],
            )
        )
        async with aclosing(self.remote.send(request)) as events:
            async for event in events:
                yield self.accept(event)

    async def resume(self) -> AsyncGenerator[RemoteUpdate, None]:
        if not self.task_id:
            raise RemoteAgentError("No remote task ID was received; sending again could duplicate work")
        yield self.accept(StreamResponse(task=await self.remote.get_task(self.task_id)))
        if self.state not in TERMINAL_STATES:
            async with aclosing(self.remote.subscribe(self.task_id)) as events:
                async for event in events:
                    yield self.accept(event)

    async def exchange(self, text: str, *, message_id: str) -> AsyncGenerator[RemoteUpdate, None]:
        """Send once, then recover a lost stream with one read/subscription.

        Never replay an uncertain send. Input-required completes this exchange;
        the next answer goes to the same task rather than starting it again.
        """
        settled = TERMINAL_STATES | {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
        try:
            async with aclosing(self.send(text, message_id=message_id)) as updates:
                async for update in updates:
                    yield update
        except RemoteAgentError as error:
            # A rejected new message must not reuse a previous task's answer.
            if not self.task_id or error.code in {
                "A2A_AUTH_REQUIRED",
                "A2A_AUTH_REJECTED",
                "A2A_AUTH_STORAGE",
                "A2A_TOKEN_MISSING",
                "A2A_FORBIDDEN",
                "A2A_RATE_LIMIT",
            }:
                raise
        if self.state not in settled:
            async with aclosing(self.resume()) as updates:
                async for update in updates:
                    yield update
        if self.state not in settled:
            raise RemoteAgentError(
                "The remote task is still pending; inspect its state before sending another request",
                code="A2A_OUTCOME_UNKNOWN",
            )
