"""A framework-level remote conversation, independent of ngn and LLM providers."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from a2a.types import Message
from a2a.types import Part
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import StreamResponse
from a2a.types import TaskState

from nagents.artifacts import IMAGE_ARTIFACT_TYPES
from nagents.artifacts import MAX_ARTIFACT_IMAGES
from nagents.artifacts import MAX_ARTIFACT_IMAGE_BYTES
from nagents.artifacts import ImageArtifact

from .transport import RemoteAgent
from .transport import RemoteAgentError

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
    images: tuple[ImageArtifact, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass
class _Artifact:
    parts: list[Part] = field(default_factory=list)
    complete: bool = False
    description: str = ""


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
        self._artifacts: dict[str, _Artifact] = {}
        self._message = ""
        self.status_text = ""

    @property
    def text(self) -> str:
        return (
            "\n\n".join(text for artifact in self._artifacts.values() if (text := self._artifact_text(artifact)))
            if self._artifacts
            else self._message
        )

    @classmethod
    def _artifact_text(cls, artifact: _Artifact) -> str:
        text = cls._text(artifact.parts)
        if not text and any(part.WhichOneof("content") == "raw" for part in artifact.parts):
            text = artifact.description
        return text

    @property
    def images(self) -> tuple[ImageArtifact, ...]:
        """Complete image parts, deduplicated by their task/artifact/part identity.

        URL-backed files require a separate fetch/credential policy and are
        rejected explicitly. No browser or host automatically fetches them.
        """
        result: list[ImageArtifact] = []
        for identifier, artifact in self._artifacts.items():
            if not artifact.complete:
                continue
            caption = self._artifact_text(artifact)
            for index, part in enumerate(artifact.parts):
                if part.WhichOneof("content") != "raw":
                    continue
                suffix = "png" if part.media_type == "image/png" else "jpg"
                try:
                    result.append(
                        ImageArtifact(
                            identifier,
                            index,
                            bytes(part.raw),
                            part.media_type,
                            part.filename or f"image-{index + 1}.{suffix}",
                            caption,
                        )
                    )
                except (ValueError, TypeError):
                    raise RemoteAgentError(
                        "Remote image artifact is malformed or incomplete", code="A2A_ARTIFACT_INVALID"
                    ) from None
        return tuple(result)

    def _artifact(
        self,
        identifier: str,
        parts: Iterable[Part],
        *,
        append: bool = False,
        complete: bool = False,
        description: str = "",
    ) -> None:
        if not identifier or len(identifier.encode("utf-8")) > 256:
            raise RemoteAgentError("Remote artifact has an invalid identity", code="A2A_ARTIFACT_INVALID")
        if len(description.encode("utf-8")) > 1048576:
            raise RemoteAgentError("Remote artifact description exceeds its limit", code="A2A_ARTIFACT_LIMIT")
        previous = self._artifacts.get(identifier)
        if append and previous is None:
            raise RemoteAgentError("Remote artifact appended before its first part", code="A2A_ARTIFACT_INVALID")
        retained = list(previous.parts) if append and previous else []
        for part in parts:
            kind = part.WhichOneof("content")
            if kind == "url":
                raise RemoteAgentError(
                    "URL-backed artifacts are unsupported; the remote agent must send inline image bytes",
                    code="A2A_ARTIFACT_URL",
                )
            if kind != "text" and (kind != "raw" or part.media_type not in IMAGE_ARTIFACT_TYPES):
                raise RemoteAgentError("Remote artifact type is unsupported", code="A2A_ARTIFACT_TYPE")
            if (
                len(part.raw) > MAX_ARTIFACT_IMAGE_BYTES
                or len(part.text.encode("utf-8")) > 1048576
                or len(part.filename) > 255
                or len(part.media_type) > 128
            ):
                raise RemoteAgentError("Remote artifact part exceeds its limit", code="A2A_ARTIFACT_LIMIT")
            # Opaque upstream metadata is not needed for images or model input.
            copied = (
                Part(text=part.text, media_type=part.media_type)
                if kind == "text"
                else Part(raw=part.raw, media_type=part.media_type, filename=part.filename)
            )
            retained.append(copied)
        if len(retained) > 256:
            raise RemoteAgentError("Remote artifact has too many parts", code="A2A_ARTIFACT_LIMIT")
        self._artifacts[identifier] = _Artifact(
            retained, complete, description or (previous.description if previous else "")
        )

    def _limits(self) -> None:
        raw = [
            part
            for artifact in self._artifacts.values()
            for part in artifact.parts
            if part.WhichOneof("content") == "raw"
        ]
        if len(raw) > MAX_ARTIFACT_IMAGES or sum(len(part.raw) for part in raw) > MAX_ARTIFACT_IMAGE_BYTES:
            raise RemoteAgentError("Remote image output exceeds 3 images or 8 MiB", code="A2A_ARTIFACT_LIMIT")

    @staticmethod
    def _text(parts: Iterable[Part]) -> str:
        return "".join(part.text for part in parts if part.WhichOneof("content") == "text")

    def accept(self, event: StreamResponse) -> RemoteUpdate:
        previous_task, previous_context = self.task_id, self.context_id
        kind = event.WhichOneof("payload")
        if kind == "message":
            candidate_task = event.message.task_id or previous_task
            candidate_context = event.message.context_id or previous_context
        elif kind == "task":
            candidate_task, candidate_context = event.task.id, event.task.context_id
        elif kind == "status_update":
            candidate_task, candidate_context = event.status_update.task_id, event.status_update.context_id
        elif kind == "artifact_update":
            candidate_task, candidate_context = event.artifact_update.task_id, event.artifact_update.context_id
        else:
            raise RemoteAgentError("Remote agent returned an unsupported event", code="A2A_PROTOCOL_INVALID")
        if kind != "message" and (not candidate_task or not candidate_context):
            raise RemoteAgentError("Remote event is missing its task or context identity", code="A2A_PROTOCOL_INVALID")
        if (previous_task and candidate_task != previous_task) or (
            previous_context and candidate_context != previous_context
        ):
            raise RemoteAgentError(
                "Remote stream changed its accepted task or context identity", code="A2A_PROTOCOL_INVALID"
            )
        status_text = ""
        if kind == "message":
            self.context_id = event.message.context_id or self.context_id
            self.task_id = event.message.task_id or self.task_id
            self._message = self._text(event.message.parts)
            if any(part.WhichOneof("content") != "text" for part in event.message.parts):
                self._artifact(event.message.message_id or "message", event.message.parts, complete=True)
            self.state = TaskState.TASK_STATE_COMPLETED
        elif kind == "task":
            task = event.task
            self.task_id, self.context_id, self.state = task.id, task.context_id, task.status.state
            for artifact in task.artifacts:
                previous = self._artifacts.get(artifact.artifact_id)
                self._artifact(
                    artifact.artifact_id,
                    artifact.parts,
                    complete=task.status.state == TaskState.TASK_STATE_COMPLETED
                    or bool(previous and previous.complete),
                    description=artifact.description,
                )
            status_text = self._text(task.status.message.parts)
        elif kind == "status_update":
            update = event.status_update
            self.task_id, self.context_id, self.state = update.task_id, update.context_id, update.status.state
            status_text = self._text(update.status.message.parts)
        elif kind == "artifact_update":
            artifact_update = event.artifact_update
            self.task_id, self.context_id = artifact_update.task_id, artifact_update.context_id
            artifact = artifact_update.artifact
            self._artifact(
                artifact.artifact_id,
                artifact.parts,
                append=artifact_update.append,
                complete=artifact_update.last_chunk,
                description=artifact.description,
            )
        else:
            raise RemoteAgentError("Remote agent returned an unsupported event", code="A2A_PROTOCOL_INVALID")
        if len(self._artifacts) > 256 or len(self.text) > 1048576 or len(status_text) > 32768:
            raise RemoteAgentError("Remote response exceeds the supported text limit", code="A2A_PROTOCOL_INVALID")
        self._limits()
        if self.state == TaskState.TASK_STATE_COMPLETED:
            for artifact in self._artifacts.values():
                artifact.complete = True
        self.status_text = status_text or self.status_text
        return RemoteUpdate(self.task_id, self.context_id, self.state, self.text, status_text, self.images)

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
                "A2A_ARTIFACT_INVALID",
                "A2A_ARTIFACT_LIMIT",
                "A2A_ARTIFACT_TYPE",
                "A2A_ARTIFACT_URL",
                "A2A_PROTOCOL_INVALID",
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
