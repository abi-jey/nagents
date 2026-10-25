"""SDK request handlers with host-owned identities and message retry admission."""

from __future__ import annotations

from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast

from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.extensions.common import get_requested_extensions
from a2a.server.agent_execution import RequestContext
from a2a.server.agent_execution import RequestContextBuilder
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.types import GetTaskRequest
from a2a.types import SendMessageRequest
from a2a.types import SubscribeToTaskRequest
from a2a.types import TaskState
from a2a.utils.errors import ContentTypeNotSupportedError
from a2a.utils.errors import InvalidParamsError
from a2a.utils.errors import TaskNotFoundError
from a2a.utils.errors import UnsupportedOperationError
from a2a.utils.input_mode_validator import validate_input_modes
from a2a.utils.proto_utils import validate_proto_required_fields
from a2a.utils.task import apply_history_length
from a2a.utils.task import validate_history_length

from .auth import AuthenticationError
from .auth import Principal
from .store import TERMINAL
from .store import MessageClaim
from .store import SQLiteTaskStore
from .store import principal

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.cluster.event_stream import TaskEventStream
    from a2a.server.context import ServerCallContext
    from a2a.server.events import Event
    from a2a.types import AgentCard
    from a2a.types import CancelTaskRequest
    from a2a.types import ListTasksRequest
    from a2a.types import ListTasksResponse
    from a2a.types import Message
    from a2a.types import Task
    from starlette.requests import Request


class AuthenticatedContextBuilder(ServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        from a2a.server.context import ServerCallContext

        user = request.scope.get("ngn.a2a.principal")
        if not isinstance(user, Principal):
            raise AuthenticationError()
        state: dict[str, object] = {"ngn.request_id": request.scope.get("ngn.request_id", "")}
        protocol = request.headers.get("a2a-version")
        if protocol is not None:
            # SDK version negotiation reads this single protocol header from
            # context. No Authorization, cookies or other HTTP headers cross
            # into execution or the SDK's potentially logged request state.
            state["headers"] = {
                "a2a-version": protocol if len(protocol) <= 32 and protocol.isprintable() else "invalid"
            }
        # Deliberately exclude request headers: bearer tokens must not become
        # SDK request context, executor data, tool inputs, or debug-log fields.
        return ServerCallContext(
            user=user,
            state=state,
            requested_extensions=get_requested_extensions(request.headers.getlist(HTTP_EXTENSION_HEADER)),
        )


class ReservedContextBuilder(RequestContextBuilder):
    def __init__(self, store: SQLiteTaskStore) -> None:
        self.store = store

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        principal(context)
        related: list[Task] = []
        if params is not None:
            claim = context.state.get("ngn.claim")
            if not isinstance(claim, MessageClaim):
                raise InvalidParamsError("Message was not admitted by this service")
            task_id, context_id = claim.task_id, claim.context_id
            if len(params.message.reference_task_ids) > 8:
                raise InvalidParamsError("At most eight owned task references are supported")
            for reference in params.message.reference_task_ids:
                stored = await self.store.get(reference, context)
                if stored is None:
                    raise TaskNotFoundError()
                related.append(stored.task)
        return RequestContext(
            call_context=context,
            request=params,
            task_id=task_id,
            context_id=context_id,
            task=task,
            related_tasks=related,
        )


class OwnedRequestHandler(DefaultRequestHandlerV2):
    def __init__(
        self, executor: AgentExecutor, store: SQLiteTaskStore, stream: TaskEventStream, card: AgentCard
    ) -> None:
        super().__init__(
            agent_executor=executor,
            task_store=store,
            agent_card=card,
            request_context_builder=ReservedContextBuilder(store),
            event_stream=stream,
            validate_input_modes=True,
        )
        self.owned_store = store
        self.exported_card = card

    @staticmethod
    def _authorize(context: ServerCallContext, scope: str, tenant: str = "") -> Principal:
        user = principal(context)
        user.require("ngn:access")
        user.require(scope)
        if tenant:
            # The single exported interface has no routing tenant. Identity
            # tenants come exclusively from the validated configured claim.
            raise InvalidParamsError("This interface does not advertise a routing tenant")
        return user

    async def _admit(self, params: SendMessageRequest, context: ServerCallContext) -> MessageClaim:
        user = self._authorize(context, "ngn:invoke", params.tenant)
        validate_proto_required_fields(params)
        validate_history_length(params.configuration)
        validate_input_modes(params.message, self.exported_card)
        if params.configuration.accepted_output_modes and not set(
            params.configuration.accepted_output_modes
        ).intersection(self.exported_card.default_output_modes):
            raise ContentTypeNotSupportedError()
        if len(params.message.reference_task_ids) > 8:
            raise InvalidParamsError("At most eight owned task references are supported")
        for reference in params.message.reference_task_ids:
            if await self.owned_store.get(reference, context) is None:
                raise TaskNotFoundError()
        claim = await self.owned_store.reserve(params, user)
        context.state["ngn.claim"] = claim
        return claim

    async def _recorded(self, claim: MessageClaim, context: ServerCallContext) -> Task:
        stored = await self.owned_store.get(claim.task_id, context)
        if stored is None:
            raise TaskNotFoundError()
        return stored.task

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext) -> Message | Task:
        original = params
        params = SendMessageRequest()
        params.CopyFrom(original)
        claim = await self._admit(params, context)
        if claim.duplicate:
            task = await self._recorded(claim, context)
            interrupted = {TaskState.TASK_STATE_AUTH_REQUIRED, TaskState.TASK_STATE_INPUT_REQUIRED}
            if not params.configuration.return_immediately and task.status.state not in TERMINAL | interrupted:
                try:
                    async with aclosing(
                        super().on_subscribe_to_task(SubscribeToTaskRequest(id=claim.task_id), context)
                    ) as events:
                        async for _ in events:
                            task = await self._recorded(claim, context)
                            if task.status.state in TERMINAL | interrupted:
                                break
                except UnsupportedOperationError:
                    # Completion can race the read preceding subscription.
                    task = await self._recorded(claim, context)
                    if task.status.state not in TERMINAL:
                        raise
            return apply_history_length(task, params.configuration)
        try:
            return cast("Message | Task", await super().on_message_send(params, context))
        finally:
            await self.owned_store.abandon_unstarted(claim, principal(context))

    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        original = params
        params = SendMessageRequest()
        params.CopyFrom(original)
        if not self.exported_card.capabilities.streaming:
            raise UnsupportedOperationError("Streaming is not supported")
        claim = await self._admit(params, context)
        if claim.duplicate:
            task = await self._recorded(claim, context)
            if task.status.state in TERMINAL:
                yield apply_history_length(task, params.configuration)
                return
            try:
                async with aclosing(
                    super().on_subscribe_to_task(SubscribeToTaskRequest(id=claim.task_id), context)
                ) as events:
                    async for event in events:
                        yield event
            except UnsupportedOperationError:
                task = await self._recorded(claim, context)
                if task.status.state not in TERMINAL:
                    raise
                yield apply_history_length(task, params.configuration)
            return
        try:
            async with aclosing(super().on_message_send_stream(params, context)) as events:
                async for event in events:
                    yield event
        finally:
            await self.owned_store.abandon_unstarted(claim, principal(context))

    async def on_get_task(self, params: GetTaskRequest, context: ServerCallContext) -> Task | None:
        self._authorize(context, "ngn:read", params.tenant)
        return cast("Task | None", await super().on_get_task(params, context))

    async def on_list_tasks(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        self._authorize(context, "ngn:read", params.tenant)
        return cast("ListTasksResponse", await super().on_list_tasks(params, context))

    async def on_cancel_task(self, params: CancelTaskRequest, context: ServerCallContext) -> Task | None:
        self._authorize(context, "ngn:cancel", params.tenant)
        return cast("Task | None", await super().on_cancel_task(params, context))

    async def on_subscribe_to_task(
        self, params: SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event, None]:
        self._authorize(context, "ngn:read", params.tenant)
        async with aclosing(super().on_subscribe_to_task(params, context)) as events:
            async for event in events:
                yield event
