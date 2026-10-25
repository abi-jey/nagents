"""Protocol lifecycle shared by native nagents Agents and the ngn adapter."""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from contextlib import aclosing
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from a2a.types import Task
from a2a.types import TaskState
from a2a.utils.errors import InvalidParamsError

from nagents.artifacts import ImageArtifact
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent

from .agent import AgentContext
from .agent import NativeAgentRuntime
from .authorization import AuthorizationDenied
from .authorization import AuthorizationSession
from .authorization import current_authorization
from .authorization import unavailable_credentials
from .diagnostics import error_frames
from .images import ImagePublisher
from .images import current_image_publisher
from .store import MessageClaim
from .store import principal

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext
    from a2a.server.events import EventQueue

    from .agent import AgentFactory
    from .agent import AgentRuntime
    from .auth import Principal
    from .authorization import AuthorizationRegistry
    from .authorization import CredentialHandler
    from .config import A2AConfig
    from .store import SQLiteTaskStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicStatus:
    state: TaskState
    text: str


@dataclass(frozen=True)
class PublicText:
    id: str
    text: str
    append: bool
    last: bool


PublicUpdate = PublicStatus | PublicText | ImageArtifact


@dataclass
class Execution:
    task_id: str
    context_id: str
    user: Principal
    events: asyncio.Queue[PublicUpdate] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    worker: asyncio.Task[None] | None = None
    runtime: AgentRuntime | None = None
    input: asyncio.Future[str] | None = None
    accepted_output_modes: tuple[str, ...] = ()

    async def report(self, state: TaskState, text: str) -> None:
        await self.events.put(PublicStatus(state, text))

    async def request_input(self, question: str) -> str:
        """Pause a host tool until a fresh owned A2A message supplies its input.

        The SDK executor yields at INPUT_REQUIRED; this private Harness worker
        remains paused. It never retains an SDK request or event queue. There
        is no automatic continuation after a process restart.
        """
        if not question.strip() or len(question.encode()) > 2048 or self.input is not None:
            raise ValueError("Input requires one bounded question at a time")
        self.input = asyncio.get_running_loop().create_future()
        try:
            await self.report(TaskState.TASK_STATE_INPUT_REQUIRED, question)
            return await self.input
        finally:
            self.input = None


class TextOutput:
    def __init__(self, execution: Execution, maximum: int) -> None:
        self.execution = execution
        self.maximum = maximum
        self.total = 0
        self.response = 0
        self.buffer = ""
        self.append = False
        self.has_chunks = False
        self.published = False

    def consume(self, size: int) -> None:
        if self.total + size > self.maximum:
            raise ValueError("Public output limit exceeded")
        self.total += size

    async def chunk(self, text: str) -> None:
        if text and self.execution.accepted_output_modes and "text/plain" not in self.execution.accepted_output_modes:
            raise ValueError("The client did not accept text/plain output")
        self.consume(len(text.encode()))
        self.buffer += text
        self.has_chunks = True
        if len(self.buffer.encode()) >= 512:
            await self.flush(False)

    async def flush(self, last: bool) -> None:
        if self.buffer or (last and self.append):
            await self.execution.events.put(PublicText(f"answer-{self.response}", self.buffer, self.append, last))
            self.published = True
            self.append = True
            self.buffer = ""

    async def done(self, text: str) -> None:
        if not self.has_chunks and text:
            await self.chunk(text)
        await self.flush(True)
        self.response += 1
        self.append = False
        self.has_chunks = False


class TaskExecutor(AgentExecutor):
    def __init__(
        self,
        config: A2AConfig,
        store: SQLiteTaskStore,
        authorizations: AuthorizationRegistry,
        *,
        credential_handler: CredentialHandler = unavailable_credentials,
    ) -> None:
        # The factory and profile are trusted embedding configuration, never
        # taken from a remote message or message metadata.
        self.config = config
        self.store = store
        self.authorizations = authorizations
        self.credential_handler = credential_handler
        self._executions: dict[str, Execution] = {}
        self._contexts: dict[str, asyncio.Lock] = {}
        self._closed = False

    @abstractmethod
    def build_runtime(self, execution: Execution, session_id: str, authorization: AuthorizationSession) -> AgentRuntime:
        """Construct a host-configured runtime without consulting wire metadata."""

    async def _produce(self, execution: Execution, text: str) -> None:
        lock = self._contexts.setdefault(execution.context_id, asyncio.Lock())
        try:
            await execution.report(TaskState.TASK_STATE_WORKING, "Processing request.")
            async with lock:
                session_id = await self.store.session_id(execution.context_id, execution.user)
                authorization = AuthorizationSession(
                    self.authorizations,
                    execution.task_id,
                    execution.user,
                    execution.report,
                    input_handler=execution.request_input,
                    credential_handler=self.credential_handler,
                )
                token = current_authorization.set(authorization)
                output = TextOutput(execution, self.config.max_output_bytes)
                publisher = ImagePublisher(
                    execution.events.put, output.consume, lambda: execution.accepted_output_modes
                )
                image_token = current_image_publisher.set(publisher)
                try:
                    runtime = self.build_runtime(execution, session_id, authorization)
                    execution.runtime = runtime
                    finished = False
                    async with aclosing(runtime.run(text)) as events:
                        async for event in events:
                            if authorization.failure is not None:
                                raise authorization.failure
                            if isinstance(event, TextChunkEvent):
                                await output.chunk(event.chunk)
                            elif isinstance(event, TextDoneEvent):
                                await output.done(event.text)
                            elif isinstance(event, ErrorEvent) and not event.recoverable:
                                raise RuntimeError("Agent generation failed")
                            elif isinstance(event, PublicStatus):
                                await execution.report(event.state, event.text)
                            elif isinstance(event, DoneEvent):
                                if output.has_chunks:
                                    await output.done("")
                                elif not output.published and event.final_text:
                                    await output.done(event.final_text)
                                finished = True
                    if authorization.failure is not None:
                        raise authorization.failure
                    if not finished:
                        raise RuntimeError("Agent ended without a completion event")
                except Exception:
                    if authorization.failure is not None:
                        raise authorization.failure from None
                    raise
                finally:
                    publisher.active = False
                    current_image_publisher.reset(image_token)
                    try:
                        if execution.runtime is not None:
                            await execution.runtime.close()
                    finally:
                        current_authorization.reset(token)
            await execution.report(TaskState.TASK_STATE_COMPLETED, "Request complete.")
        except asyncio.CancelledError:
            raise
        except AuthorizationDenied:
            await execution.report(
                TaskState.TASK_STATE_REJECTED,
                "The pending operation was denied and was not executed. Earlier completed work is retained.",
            )
        except Exception as exc:
            # SDK/HTTP logs must not receive raw provider/tool exception bodies,
            # prompts, arguments, bearer credentials, or Python frame locals.
            logger.warning(
                "a2a.task.failed task=%s error_type=%s frames=%s",
                execution.task_id,
                type(exc).__name__,
                error_frames(exc),
            )
            await execution.report(
                TaskState.TASK_STATE_FAILED,
                "The task could not complete. Pending work was stopped; inspect earlier results before retrying.",
            )
        finally:
            await self.authorizations.cancel_task(execution.task_id)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if self._closed:
            raise InvalidParamsError("The service is shutting down")
        user = principal(context.call_context)
        claim = context.call_context.state.get("ngn.claim")
        if not isinstance(claim, MessageClaim) or context.message is None:
            raise InvalidParamsError("Message was not admitted by this service")
        await self.store.started(claim, user)
        task = context.current_task
        if task is None:
            stored = await self.store.get(claim.task_id, context.call_context)
            if stored is None:
                raise InvalidParamsError("The reserved task is unavailable")
            task = stored.task
        first = Task()
        first.CopyFrom(task)
        execution = self._executions.get(claim.task_id)
        if execution is not None:
            if execution.user.user_name != user.user_name or execution.input is None or execution.input.done():
                raise InvalidParamsError("This task is not awaiting new input")
            # A non-streaming send returns on an interrupted Task. Describe
            # resumed work in its initial snapshot, not the previous pause.
            first.status.state = TaskState.TASK_STATE_WORKING
            first.status.message.Clear()
            first.status.timestamp.GetCurrentTime()
        await event_queue.enqueue_event(first)
        if execution is None:
            execution = Execution(
                claim.task_id,
                claim.context_id,
                user,
                accepted_output_modes=tuple(context.configuration.accepted_output_modes)
                if context.configuration
                else (),
            )
            self._executions[claim.task_id] = execution
            execution.worker = asyncio.create_task(
                self._produce(execution, context.get_user_input()), name=f"ngn-a2a-{claim.task_id}"
            )
        else:
            if execution.user.user_name != user.user_name or execution.input is None or execution.input.done():
                raise InvalidParamsError("This task is not awaiting new input")
            execution.accepted_output_modes = (
                tuple(context.configuration.accepted_output_modes) if context.configuration else ()
            )
            execution.input.set_result(context.get_user_input())
            await execution.report(TaskState.TASK_STATE_WORKING, "Input received; resuming work.")
        updater = TaskUpdater(event_queue, claim.task_id, claim.context_id)
        waiting_input = False
        try:
            while True:
                update = await execution.events.get()
                if isinstance(update, ImageArtifact):
                    parts = [Part(text=update.caption, media_type="text/plain")] if update.caption else []
                    parts.append(Part(raw=update.data, media_type=update.media_type, filename=update.filename))
                    await updater.add_artifact(
                        parts, artifact_id=update.artifact_id, name=update.filename, last_chunk=True
                    )
                    continue
                if isinstance(update, PublicText):
                    await updater.add_artifact(
                        [Part(text=update.text, media_type="text/plain")],
                        artifact_id=update.id,
                        name="Assistant response",
                        append=update.append,
                        last_chunk=update.last,
                    )
                    continue
                await updater.update_status(
                    update.state,
                    new_text_message(update.text, task_id=claim.task_id, context_id=claim.context_id),
                )
                if update.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                    waiting_input = True
                    return
                if update.state in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_REJECTED,
                }:
                    return
        finally:
            if not waiting_input:
                await self._stop(claim.task_id)

    async def _stop(self, task_id: str) -> None:
        execution = self._executions.pop(task_id, None)
        if execution is not None and execution.worker is not None:
            execution.worker.cancel()
            with suppress(asyncio.CancelledError):
                await execution.worker
        await self.authorizations.cancel_task(task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise InvalidParamsError("Task identity is required")
        await self._stop(context.task_id)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    async def close(self) -> None:
        self._closed = True
        for task_id in list(self._executions):
            await self._stop(task_id)


class NagentsAgentExecutor(TaskExecutor):
    def __init__(
        self,
        config: A2AConfig,
        agent_factory: AgentFactory,
        store: SQLiteTaskStore,
        authorizations: AuthorizationRegistry,
        *,
        read_only_tools: frozenset[str] = frozenset(),
        credential_handler: CredentialHandler = unavailable_credentials,
    ) -> None:
        super().__init__(config, store, authorizations, credential_handler=credential_handler)
        self.agent_factory = agent_factory
        self.read_only_tools = read_only_tools

    def build_runtime(self, execution: Execution, session_id: str, authorization: AuthorizationSession) -> AgentRuntime:
        directory = self.config.data_dir / "agents"
        directory.mkdir(mode=0o700, exist_ok=True)
        context = AgentContext(
            self.config.agent, execution.task_id, execution.context_id, session_id, execution.user, directory
        )
        return NativeAgentRuntime(self.agent_factory(context), context, authorization, self.read_only_tools)


async def request_input(question: str) -> str:
    """Explicit input-required hook for a trusted server-side tool.

    The returned text is untrusted user input, not authorization. Input remains
    pending until a new owned message, cancellation, or server shutdown. Tools
    must separately obtain action approval and downstream credentials as needed.
    """
    return await current_authorization.get().request_input(question)
