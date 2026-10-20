"""Authenticated SDK handler behavior at the host-owned task boundary."""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest
from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCapabilities
from a2a.types import AgentCard
from a2a.types import AgentInterface
from a2a.types import AgentSkill
from a2a.types import CancelTaskRequest
from a2a.types import GetTaskRequest
from a2a.types import ListTasksRequest
from a2a.types import Part
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import SubscribeToTaskRequest
from a2a.types import Task
from a2a.types import TaskState
from a2a.utils.errors import ContentTypeNotSupportedError
from a2a.utils.errors import InvalidParamsError
from a2a.utils.errors import TaskNotCancelableError
from a2a.utils.errors import TaskNotFoundError
from starlette.requests import Request

from nagents.a2a.auth import AuthenticationError
from nagents.a2a.auth import Principal
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.handlers import AuthenticatedContextBuilder
from nagents.a2a.handlers import OwnedRequestHandler
from nagents.a2a.store import SQLiteEventStream
from nagents.a2a.store import SQLiteTaskStore
from nagents.a2a.store import principal as context_principal

if TYPE_CHECKING:
    from pathlib import Path

    from a2a.server.events import EventQueue


ISSUER = "https://issuer.example"


def user(subject: str = "alice", *scopes: str) -> Principal:
    return Principal(ISSUER, subject, "tenant-a", frozenset({"ngn:access", *scopes}), time.time() + 3600)


def call(principal: Principal | None = None) -> ServerCallContext:
    return ServerCallContext(user=principal or user("alice", "ngn:invoke", "ngn:read", "ngn:cancel"))


def request(message_id: str = "message-1", text: str = "Find one result") -> SendMessageRequest:
    message = new_text_message(text, role=Role.ROLE_USER)
    message.message_id = message_id
    return SendMessageRequest(message=message)


class ProbeExecutor(AgentExecutor):
    def __init__(self, store: SQLiteTaskStore, *, hold: bool = False) -> None:
        self.store = store
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.input_ids: list[str] = []
        self.input_texts: list[str] = []

    async def execute(self, context: object, event_queue: EventQueue) -> None:
        # Use the SDK's real context and updater interface while keeping the
        # test independent from providers and the ngn model runtime.
        from a2a.server.agent_execution import RequestContext

        assert isinstance(context, RequestContext)
        self.calls += 1
        assert context.message is not None
        self.input_ids.append(context.message.message_id)
        self.input_texts.append(context.message.parts[0].text)
        assert context.task_id is not None and context.context_id is not None
        claim = context.call_context.state["ngn.claim"]
        await self.store.started(claim, context_principal(context.call_context))
        self.started.set()
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact([Part(text="intermediate")], artifact_id="probe-result")
        await self.release.wait()
        await updater.complete(new_text_message("done", role=Role.ROLE_AGENT))

    async def cancel(self, context: object, event_queue: EventQueue) -> None:
        from a2a.server.agent_execution import RequestContext

        assert isinstance(context, RequestContext)
        assert context.task_id is not None and context.context_id is not None
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def test_context_builder_keeps_only_validated_identity_and_extensions() -> None:
    principal = user("alice", "ngn:invoke")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer synthetic-secret-value"),
                (b"a2a-extensions", b"https://example.test/ext"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 443),
            "ngn.a2a.principal": principal,
            "ngn.request_id": "request-safe-id",
        }
    )
    context = AuthenticatedContextBuilder().build(request)
    assert context.user is principal
    assert context.state == {"ngn.request_id": "request-safe-id"}
    assert "synthetic-secret-value" not in repr(context)
    assert "authorization" not in repr(context).lower()
    assert list(context.requested_extensions) == ["https://example.test/ext"]


def test_sdk_handler_runs_once_for_same_message_and_replays_owned_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ProbeExecutor.__new__(ProbeExecutor)
        # The executor needs its store for the start marker; initialize the
        # store first, then bind the probe used by the handler.
        settings = A2AConfig(
            public_url="https://a2a.example",
            agent="assistant",
            issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
            data_dir=tmp_path / "data",
        )
        store = SQLiteTaskStore(settings)
        await store.initialize()
        ProbeExecutor.__init__(executor, store, hold=True)
        card = AgentCard(
            name="assistant",
            supported_interfaces=[
                AgentInterface(url="https://a2a.example", protocol_binding="JSONRPC", protocol_version="1.0")
            ],
            version="1.0.0",
            description="Test assistant",
            skills=[AgentSkill(id="assistant", name="Assistant", description="Test skill", tags=["test"])],
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
        original = request()
        original.configuration.return_immediately = True
        original_message = original.message.SerializeToString()
        first_result, retry_result = await asyncio.gather(
            handler.on_message_send(original, call()),
            handler.on_message_send(original, call()),
        )
        first = first_result
        assert isinstance(first, Task)
        assert isinstance(retry_result, Task)
        assert retry_result.id == first.id
        await executor.started.wait()
        assert original.message.SerializeToString() == original_message
        assert executor.calls == 1

        executor.release.set()
        for _ in range(100):
            saved = await handler.on_get_task(GetTaskRequest(id=first.id), call())
            if saved is not None and saved.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.01)
        assert saved is not None and saved.status.state == TaskState.TASK_STATE_COMPLETED

        replay = await handler.on_message_send(request(), call())
        assert isinstance(replay, Task)
        assert replay.id == first.id
        assert replay.status.state == TaskState.TASK_STATE_COMPLETED
        assert executor.calls == 1
        assert executor.input_ids == ["message-1"]
        assert executor.input_texts == ["Find one result"]
        terminal_stream = handler.on_message_send_stream(request(), call())
        async with aclosing(terminal_stream):
            replayed_terminal = await anext(terminal_stream)
        assert isinstance(replayed_terminal, Task)
        assert replayed_terminal.id == first.id
        with pytest.raises(InvalidParamsError):
            await handler.on_message_send(request("message-1", "changed retry"), call())
        assert executor.calls == 1

    asyncio.run(scenario())


def test_stream_disconnect_does_not_duplicate_execution_and_task_can_be_resubscribed(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ProbeExecutor.__new__(ProbeExecutor)
        settings = A2AConfig(
            public_url="https://a2a.example",
            agent="assistant",
            issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
            data_dir=tmp_path / "data",
        )
        store = SQLiteTaskStore(settings)
        await store.initialize()
        ProbeExecutor.__init__(executor, store, hold=True)
        card = AgentCard(
            name="assistant",
            supported_interfaces=[
                AgentInterface(url="https://a2a.example", protocol_binding="JSONRPC", protocol_version="1.0")
            ],
            version="1.0.0",
            description="Test assistant",
            skills=[AgentSkill(id="assistant", name="Assistant", description="Test skill", tags=["test"])],
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
        params = request()
        stream = handler.on_message_send_stream(params, call())
        first_event = await asyncio.wait_for(anext(stream), timeout=2)
        assert first_event is not None
        await executor.started.wait()
        await stream.aclose()

        retry_stream = handler.on_message_send_stream(request(), call())
        replayed: list[object] = []
        async with aclosing(retry_stream):
            replayed.append(await asyncio.wait_for(anext(retry_stream), timeout=2))
        assert executor.calls == 1
        assert replayed
        executor.release.set()
        terminal: Task | None = None
        for _ in range(100):
            terminal = await handler.on_get_task(GetTaskRequest(id=first_event.task_id), call())
            if terminal is not None and terminal.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.01)
        assert terminal is not None and terminal.status.state == TaskState.TASK_STATE_COMPLETED
        assert executor.calls == 1

    asyncio.run(scenario())


def test_cancel_reaches_active_sdk_executor_and_records_terminal_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ProbeExecutor.__new__(ProbeExecutor)
        settings = A2AConfig(
            public_url="https://a2a.example",
            agent="assistant",
            issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
            data_dir=tmp_path / "data",
        )
        store = SQLiteTaskStore(settings)
        await store.initialize()
        ProbeExecutor.__init__(executor, store, hold=True)
        card = AgentCard(
            name="assistant",
            description="Test assistant",
            supported_interfaces=[
                AgentInterface(url="https://a2a.example", protocol_binding="JSONRPC", protocol_version="1.0")
            ],
            version="1.0.0",
            skills=[AgentSkill(id="assistant", name="Assistant", description="Test skill", tags=["test"])],
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
        params = request()
        params.configuration.return_immediately = True
        submitted = await handler.on_message_send(params, call())
        assert isinstance(submitted, Task)
        await executor.started.wait()
        canceled = await handler.on_cancel_task(CancelTaskRequest(id=submitted.id), call())
        assert canceled is not None and canceled.status.state == TaskState.TASK_STATE_CANCELED
        saved = await handler.on_get_task(GetTaskRequest(id=submitted.id), call())
        assert saved is not None and saved.status.state == TaskState.TASK_STATE_CANCELED
        assert executor.calls == 1

    asyncio.run(scenario())


def test_handler_enforces_owner_scopes_and_validates_duplicate_payloads(tmp_path: Path) -> None:
    async def scenario() -> None:
        executor = ProbeExecutor.__new__(ProbeExecutor)
        settings = A2AConfig(
            public_url="https://a2a.example",
            agent="assistant",
            issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
            data_dir=tmp_path / "data",
        )
        store = SQLiteTaskStore(settings)
        await store.initialize()
        ProbeExecutor.__init__(executor, store)
        card = AgentCard(
            name="assistant",
            description="Test assistant",
            supported_interfaces=[
                AgentInterface(url="https://a2a.example", protocol_binding="JSONRPC", protocol_version="1.0")
            ],
            version="1.0.0",
            skills=[AgentSkill(id="assistant", name="Assistant", description="Test skill", tags=["test"])],
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
        with pytest.raises(AuthenticationError):
            await handler.on_message_send(request(), call(user("alice", "ngn:read")))
        with pytest.raises(AuthenticationError):
            await handler.on_list_tasks(ListTasksRequest(), call(user("alice", "ngn:invoke")))
        with pytest.raises(AuthenticationError):
            await handler.on_cancel_task(CancelTaskRequest(id="unknown"), call(user("alice", "ngn:read")))
        with pytest.raises(TaskNotFoundError):
            await handler.on_get_task(GetTaskRequest(id="unknown"), call())

        foreign_claim = await store.reserve(request("bob-seed"), user("bob", "ngn:invoke"))
        foreign_reference = request("foreign-reference")
        foreign_reference.message.reference_task_ids.append(foreign_claim.task_id)
        with pytest.raises(TaskNotFoundError):
            await handler.on_message_send(foreign_reference, call())
        unknown_context = request("unknown-context")
        unknown_context.message.context_id = "not-an-owned-context"
        with pytest.raises(InvalidParamsError):
            await handler.on_message_send(unknown_context, call())

        completed = await handler.on_message_send(request(), call())
        assert isinstance(completed, Task)
        with pytest.raises(TaskNotFoundError):
            await handler.on_get_task(GetTaskRequest(id=completed.id), call(user("bob", "ngn:read")))
        bob_tasks = await handler.on_list_tasks(ListTasksRequest(), call(user("bob", "ngn:read")))
        assert bob_tasks.total_size == 1
        assert [task.id for task in bob_tasks.tasks] == [foreign_claim.task_id]
        with pytest.raises(TaskNotFoundError):
            await handler.on_cancel_task(CancelTaskRequest(id=completed.id), call(user("bob", "ngn:cancel")))
        with pytest.raises(TaskNotCancelableError):
            await handler.on_cancel_task(CancelTaskRequest(id=completed.id), call())
        with pytest.raises(TaskNotFoundError):
            async with aclosing(
                handler.on_subscribe_to_task(SubscribeToTaskRequest(id=completed.id), call(user("bob", "ngn:read")))
            ) as events:
                await anext(events)

        malformed = request("malformed")
        malformed.message.ClearField("parts")
        with pytest.raises(InvalidParamsError):
            await handler.on_message_send(malformed, call())
        unsupported = request("unsupported-output")
        unsupported.configuration.accepted_output_modes.append("application/json")
        with pytest.raises(ContentTypeNotSupportedError):
            await handler.on_message_send(unsupported, call())
        assert executor.calls == 1

    asyncio.run(scenario())
