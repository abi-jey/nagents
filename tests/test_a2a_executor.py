"""The A2A adapter drives ordinary nagents Agents and their original tool executor."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import AgentCapabilities
from a2a.types import AgentCard
from a2a.types import AgentInterface
from a2a.types import AgentSkill
from a2a.types import CancelTaskRequest
from a2a.types import GetTaskRequest
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import Task
from a2a.types import TaskArtifactUpdateEvent
from a2a.types import TaskState
from a2a.types import TaskStatusUpdateEvent
from a2a.utils.errors import TaskNotFoundError
from a2a.utils.errors import UnsupportedOperationError

from nagents.a2a.auth import Principal
from nagents.a2a.authorization import AuthorizationRegistry
from nagents.a2a.authorization import AuthorizationUnavailable
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.executor import NagentsAgentExecutor
from nagents.a2a.executor import request_input
from nagents.a2a.handlers import OwnedRequestHandler
from nagents.a2a.harness import HarnessAgentExecutor
from nagents.a2a.harness import HarnessRuntime
from nagents.a2a.store import SQLiteEventStream
from nagents.a2a.store import SQLiteTaskStore
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import HarnessConfig
from nagents.harness.runtime import Harness
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolDefinition
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.a2a.agent import AgentContext
    from nagents.harness.auth import OpenAIAuth


pytestmark = pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")

ISSUER = "https://issuer.example"


class ScriptedProvider(Provider):
    """A real Provider implementation that yields scripted framework events."""

    def __init__(self, script: ProviderScript) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "fixture-only-key", "fixture-model")
        self.script = script
        self.requests: list[list[Message]] = []
        self.schemas: list[list[ToolDefinition]] = []
        self.closed = False

    async def verify_model(self, force: bool = False) -> bool:
        del force
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        del config, stream, verify_model
        self.requests.append(messages.copy())
        self.schemas.append(list(tools or []))
        async for event in self.script(self, messages):
            yield event

    async def close(self) -> None:
        self.closed = True
        await super().close()


ProviderScript = Callable[[ScriptedProvider, list[Message]], AsyncIterator[Event]]
HarnessScript = Callable[[FakeProvider, list[Message]], AsyncIterator[Event]]


class RuntimeFixture:
    def __init__(
        self,
        handler: OwnedRequestHandler,
        executor: NagentsAgentExecutor,
        registry: AuthorizationRegistry,
        store: SQLiteTaskStore,
        agents: list[Agent],
        providers: list[ScriptedProvider],
    ) -> None:
        self.handler = handler
        self.executor = executor
        self.registry = registry
        self.store = store
        self.agents = agents
        self.providers = providers

    async def close(self) -> None:
        try:
            await self.executor.close()
        finally:
            try:
                await self.handler.aclose()
            finally:
                await self.registry.close()


def make_principal() -> Principal:
    return Principal(
        ISSUER,
        "a2a-agent-test-user",
        "tenant-a",
        frozenset({"ngn:access", "ngn:invoke", "ngn:read", "ngn:cancel", "ngn:approve"}),
        time.time() + 3600,
    )


def call_context() -> ServerCallContext:
    return ServerCallContext(user=make_principal())


def request(message_id: str, text: str, *, task_id: str = "", context_id: str = "") -> SendMessageRequest:
    from a2a.helpers import new_text_message

    message = new_text_message(text, role=Role.ROLE_USER)
    message.message_id = message_id
    message.task_id = task_id
    message.context_id = context_id
    return SendMessageRequest(message=message)


async def setup_adapter(
    tmp_path: Path,
    script: ProviderScript,
    *,
    tools: list[Callable[..., object]] | None = None,
    read_only_tools: frozenset[str] = frozenset(),
) -> RuntimeFixture:
    config = A2AConfig(
        public_url="https://a2a.example",
        agent="assistant",
        issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
        data_dir=tmp_path / "a2a-state",
        max_output_bytes=8192,
    )
    config.data_dir.mkdir(mode=0o700, parents=True)
    store = SQLiteTaskStore(config)
    await store.initialize()
    registry = AuthorizationRegistry(config, store)
    await registry.initialize()
    agents: list[Agent] = []
    providers: list[ScriptedProvider] = []

    def factory(context: AgentContext) -> Agent:
        provider = ScriptedProvider(script)
        providers.append(provider)
        agent = Agent(
            provider=provider,
            session_manager=SessionManager(context.data_dir / "sessions.db"),
            tools=tools,
            system_prompt="You are a local test assistant.",
            streaming=True,
            compaction_strategy=None,
            save_tool_outputs=False,
        )
        agents.append(agent)
        return agent

    executor = NagentsAgentExecutor(config, factory, store, registry, read_only_tools=read_only_tools)
    card = AgentCard(
        name="assistant",
        description="Native Agent fixture",
        supported_interfaces=[
            AgentInterface(url="https://a2a.example", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        version="1.0.0",
        skills=[AgentSkill(id="assistant", name="Assistant", description="Fixture", tags=["test"])],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
    return RuntimeFixture(handler, executor, registry, store, agents, providers)


async def collect_to_state(stream: AsyncIterator[Event], state: TaskState) -> list[Event]:
    events: list[Event] = []
    async for event in stream:
        events.append(event)
        if isinstance(event, TaskStatusUpdateEvent) and event.status.state == state:
            return events
    raise AssertionError(f"A2A stream ended before {TaskState.Name(state)}")


async def drain_terminal(stream: AsyncIterator[Event]) -> list[Event]:
    events: list[Event] = []
    async for event in stream:
        events.append(event)
        if isinstance(event, TaskStatusUpdateEvent) and event.status.state in {
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_REJECTED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED,
        }:
            return events
    return events


@pytest.fixture(autouse=True)
def isolated_process_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("NGN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_native_agent_streams_only_public_text_not_reasoning(tmp_path: Path) -> None:
    async def script(provider: ScriptedProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield ReasoningChunkEvent(chunk="PRIVATE_REASONING_MARKER")
        yield TextChunkEvent(chunk="public answer ")
        yield TextDoneEvent(text="public answer final")
        yield DoneEvent(final_text="public answer final")

    runtime = await setup_adapter(tmp_path, script)
    try:
        events = await drain_terminal(
            runtime.handler.on_message_send_stream(request("visible", "Answer"), call_context())
        )
        assert any(isinstance(event, TaskArtifactUpdateEvent) for event in events)
        serialized = repr(events)
        assert "PRIVATE_REASONING_MARKER" not in serialized
        assert "public answer" in serialized
        assert len(runtime.agents) == len(runtime.providers) == 1
        assert runtime.providers[0].requests
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_native_custom_tool_requires_exact_approval_before_execution(tmp_path: Path, approved: bool) -> None:
    marker = tmp_path / "approved.marker"
    command = "private command argument"

    async def gated_tool(command: str) -> str:
        marker.write_text(command)
        return "tool finished"

    async def script(provider: ScriptedProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ReasoningChunkEvent(chunk="PRIVATE_TOOL_REASONING")
            yield TextChunkEvent(chunk="Before approval.")
            yield ToolCallEvent(id="invoke-tool", name="gated_tool", arguments={"command": command})
        else:
            yield TextDoneEvent(text="The operation completed.")
            yield DoneEvent(final_text="The operation completed.")
        del messages

    runtime = await setup_adapter(tmp_path, script, tools=[gated_tool])
    try:
        stream = runtime.handler.on_message_send_stream(request("tool-approval", "Please run the tool"), call_context())
        events = await collect_to_state(stream, TaskState.TASK_STATE_AUTH_REQUIRED)
        pending_status = next(
            event
            for event in events
            if isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_AUTH_REQUIRED
        )
        assert not marker.exists()
        public_text = pending_status.status.message.parts[0].text
        assert command not in public_text
        assert "PRIVATE_TOOL_REASONING" not in repr(events)
        record_id = public_text.rsplit("/", 1)[-1]
        owner = make_principal()
        record = await runtime.registry.inspect(record_id, owner)
        assert record.details["arguments"] == {"command": command}
        await runtime.registry.decide(record.id, owner, record.nonce, approved, record.digest)
        events.extend(await drain_terminal(stream))
        task = await runtime.handler.on_get_task(GetTaskRequest(id=pending_status.task_id), call_context())
        assert task is not None
        if approved:
            assert marker.read_text() == command
            assert task.status.state == TaskState.TASK_STATE_COMPLETED
            assert len(runtime.providers[0].requests) == 2
        else:
            assert not marker.exists()
            assert task.status.state == TaskState.TASK_STATE_REJECTED
            assert len(runtime.providers[0].requests) == 1
        assert command not in repr(events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_trusted_request_input_yields_and_resumes_same_native_agent_once(tmp_path: Path) -> None:
    async def script(provider: ScriptedProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(
                id="ask-user", name="request_input", arguments={"question": "Which draft should I use?"}
            )
        else:
            yield TextDoneEvent(text=f"Use {messages[-1].content}.")
            yield DoneEvent(final_text=f"Use {messages[-1].content}.")

    runtime = await setup_adapter(tmp_path, script, tools=[request_input], read_only_tools=frozenset({"request_input"}))
    try:
        first = await asyncio.wait_for(
            runtime.handler.on_message_send(request("ask", "Prepare a draft"), call_context()), timeout=5
        )
        assert isinstance(first, Task)
        assert first.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        assert len(runtime.agents) == len(runtime.providers) == 1
        assert len(runtime.providers[0].requests) == 1

        continuation = request("answer", "draft-7", task_id=first.id, context_id=first.context_id)
        resumed = await runtime.handler.on_message_send(continuation, call_context())
        assert isinstance(resumed, Task)
        assert resumed.status.state == TaskState.TASK_STATE_COMPLETED
        assert len(runtime.agents) == len(runtime.providers) == 1
        assert len(runtime.providers[0].requests) == 2
        assert any(any(part.text == "Use draft-7." for part in artifact.parts) for artifact in resumed.artifacts)

        paused = await runtime.handler.on_message_send(request("ask-cancel", "Prepare another draft"), call_context())
        assert isinstance(paused, Task) and paused.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
        canceled = await runtime.handler.on_cancel_task(CancelTaskRequest(id=paused.id), call_context())
        assert canceled is not None and canceled.status.state == TaskState.TASK_STATE_CANCELED
        with pytest.raises(UnsupportedOperationError):
            await runtime.handler.on_message_send(
                request("late-answer", "draft-8", task_id=paused.id, context_id=paused.context_id), call_context()
            )
        assert len(runtime.agents) == 2
        assert len(runtime.providers[1].requests) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_disconnect_retry_does_not_duplicate_generation_and_cancel_stops_pending_approval(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    command = "private command argument"
    marker = tmp_path / "must-not-run.marker"

    async def gated_tool(command: str) -> str:
        marker.write_text(command)
        return "ran"

    async def script(provider: ScriptedProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            entered.set()
            await release.wait()
            yield TextDoneEvent(text="Completed after disconnect")
            yield DoneEvent(final_text="Completed after disconnect")
        del messages

    runtime = await setup_adapter(tmp_path, script)
    try:
        first = request("disconnect", "Do not duplicate")
        first.configuration.return_immediately = True
        stream = runtime.handler.on_message_send_stream(first, call_context())
        async with aclosing(stream):
            first_event = await asyncio.wait_for(anext(stream), timeout=5)
        assert isinstance(first_event, Task)
        await asyncio.wait_for(entered.wait(), timeout=5)
        duplicate = await runtime.handler.on_message_send(first, call_context())
        assert isinstance(duplicate, Task) and duplicate.id == first_event.id
        assert len(runtime.agents) == len(runtime.providers) == 1
        assert len(runtime.providers[0].requests) == 1
        release.set()
        for _ in range(200):
            saved = await runtime.handler.on_get_task(GetTaskRequest(id=first_event.id), call_context())
            if saved is not None and saved.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.01)
        assert saved is not None and saved.status.state == TaskState.TASK_STATE_COMPLETED
    finally:
        release.set()
        await runtime.close()

    async def pending_script(provider: ScriptedProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield ToolCallEvent(id="cancel-me", name="gated_tool", arguments={"command": command})

    cancel_workspace = tmp_path / "cancel"
    cancel_workspace.mkdir(mode=0o700)
    runtime = await setup_adapter(cancel_workspace, pending_script, tools=[gated_tool])
    try:
        stream = runtime.handler.on_message_send_stream(request("cancel-auth", "Attempt the operation"), call_context())
        async with aclosing(stream):
            events = await collect_to_state(stream, TaskState.TASK_STATE_AUTH_REQUIRED)
        status = next(
            event
            for event in events
            if isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_AUTH_REQUIRED
        )
        record_id = status.status.message.parts[0].text.rsplit("/", 1)[-1]
        canceled = await runtime.handler.on_cancel_task(CancelTaskRequest(id=status.task_id), call_context())
        assert canceled is not None and canceled.status.state == TaskState.TASK_STATE_CANCELED
        with pytest.raises(AuthorizationUnavailable):
            await runtime.registry.inspect(record_id, make_principal())
        assert not marker.exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_harness_adapter_uses_the_shared_task_executor_and_real_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.harness import runtime as harness_runtime

    workspace = tmp_path / "harness-workspace"
    workspace.mkdir(mode=0o700)
    data_dir = tmp_path / "a2a-state"
    data_dir.mkdir(mode=0o700)
    config = A2AConfig(
        public_url="https://a2a.example",
        agent="assistant",
        issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
        data_dir=data_dir,
        max_output_bytes=8192,
    )
    store = SQLiteTaskStore(config)
    await store.initialize()
    registry = AuthorizationRegistry(config, store)
    await registry.initialize()
    harness_config = HarnessConfig(
        workspace=workspace,
        data_dir=tmp_path / "unused-harness-state",
        providers=connection(auth="api-key"),
        model="fixture-model",
    )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextChunkEvent(chunk="Harness adapter ")
        yield TextDoneEvent(text="Harness adapter output")
        yield DoneEvent(final_text="Harness adapter output")

    providers: list[FakeProvider] = []

    def provider_factory(config: HarnessConfig, _login_store: object | None = None) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(harness_runtime, "HarnessProvider", provider_factory)
    harnesses: list[Harness] = []

    def factory(config: HarnessConfig, auth: OpenAIAuth) -> Harness:
        harness = Harness(config, shared_auth=auth)
        harness.agent.compaction_strategy = None
        harnesses.append(harness)
        return harness

    executor = HarnessAgentExecutor(config, harness_config, store, registry, factory=factory)
    card = AgentCard(
        name="assistant",
        description="Harness adapter fixture",
        supported_interfaces=[
            AgentInterface(url="https://a2a.example", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        version="1.0.0",
        skills=[AgentSkill(id="assistant", name="Assistant", description="Fixture", tags=["test"])],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
    try:
        events = await drain_terminal(
            handler.on_message_send_stream(request("harness-native", "Hello Harness"), call_context())
        )
        assert any(isinstance(event, TaskArtifactUpdateEvent) for event in events)
        assert "Harness adapter" in repr(events)
        assert len(harnesses) == len(providers) == 1
    finally:
        await executor.close()
        await registry.close()


@pytest.mark.parametrize("outcome", ["deny", "cancel"])
@pytest.mark.asyncio
async def test_nested_harness_approval_stays_on_root_task_and_private_to_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from nagents.harness import runtime as harness_runtime

    workspace = tmp_path / "nested-harness-workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "protected").mkdir(mode=0o700)
    data_dir = tmp_path / "nested-a2a-state"
    data_dir.mkdir(mode=0o700)
    config = A2AConfig(
        public_url="https://a2a.example",
        agent="assistant",
        issuer=IssuerConfig(issuer=ISSUER, audience="nagents-a2a"),
        data_dir=data_dir,
        max_output_bytes=16384,
    )
    store = SQLiteTaskStore(config)
    await store.initialize()
    registry = AuthorizationRegistry(config, store)
    await registry.initialize()
    harness_config = HarnessConfig(
        workspace=workspace,
        data_dir=tmp_path / "unused-harness-data",
        providers=connection(auth="api-key"),
        model="fixture-model",
    )
    private_reasoning = "PRIVATE_GRANDCHILD_REASONING_MARKER"
    private_argument = "PRIVATE_GRANDCHILD_TOOL_ARGUMENT_MARKER"
    action_path = "protected/denied-grandchild-action.txt"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index == 0 and len(provider.requests) == 1:
            yield ReasoningChunkEvent(chunk="PRIVATE_ROOT_REASONING_MARKER")
            yield ToolCallEvent(id="root-delegate", name="to_agent", arguments={"prompt": "Create child"})
        elif provider.index == 1 and len(provider.requests) == 1:
            yield ReasoningChunkEvent(chunk="PRIVATE_CHILD_REASONING_MARKER")
            yield ToolCallEvent(id="child-delegate", name="to_agent", arguments={"prompt": "Create grandchild"})
        elif provider.index == 2 and len(provider.requests) == 1:
            yield ReasoningChunkEvent(chunk=private_reasoning)
            yield ToolCallEvent(
                id="grandchild-write",
                name="write",
                arguments={"path": action_path, "content": private_argument},
            )
        else:
            yield TextDoneEvent(text="Ordinary scripted completion.")
            yield DoneEvent(final_text="Ordinary scripted completion.")
        del messages

    providers: list[FakeProvider] = []

    def provider_factory(provider_config: HarnessConfig, login_store: object | None = None) -> FakeProvider:
        del login_store
        provider = FakeProvider(provider_config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(harness_runtime, "HarnessProvider", provider_factory)
    executor = HarnessAgentExecutor(config, harness_config, store, registry)
    card = AgentCard(
        name="assistant",
        description="Nested real Harness fixture",
        supported_interfaces=[
            AgentInterface(url="https://a2a.example/rpc", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        version="1.0.0",
        skills=[AgentSkill(id="assistant", name="Assistant", description="Fixture", tags=["test"])],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    handler = OwnedRequestHandler(executor, store, SQLiteEventStream(store), card)
    alice = make_principal()
    alice_context = ServerCallContext(user=alice)
    other_owner = Principal(
        ISSUER,
        "concurrent-owner",
        "tenant-b",
        frozenset({"ngn:access", "ngn:invoke", "ngn:read", "ngn:cancel", "ngn:approve"}),
        time.time() + 3600,
    )
    other_context = ServerCallContext(user=other_owner)
    stream = handler.on_message_send_stream(request("nested-root", "Start the nested operation"), alice_context)
    public_events: list[Event] = []
    try:
        public_events = await collect_to_state(stream, TaskState.TASK_STATE_AUTH_REQUIRED)
        pending_status = next(
            event
            for event in public_events
            if isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_AUTH_REQUIRED
        )
        root_task_id = pending_status.task_id
        approval_id = pending_status.status.message.parts[0].text.rsplit("/", 1)[-1]
        record = await registry.inspect(approval_id, alice)
        assert record.task_id == root_task_id
        assert record.owner == alice.user_name
        assert record.details["tool"] == "write"
        assert record.details["depth"] == 2
        assert record.details["arguments"] == {"path": action_path, "content": private_argument}
        assert isinstance(record.details["description"], str) and record.details["description"]
        execution = executor._executions[root_task_id]
        assert isinstance(execution.runtime, HarnessRuntime)
        task_tree = execution.runtime.harness.tasks.list()
        assert sorted(info.depth for info in task_tree) == [1, 2]
        assert len(providers) == 3

        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(approval_id, other_owner)
        with pytest.raises(AuthorizationUnavailable):
            await registry.decide(approval_id, other_owner, record.nonce, True, record.digest)
        with pytest.raises(TaskNotFoundError):
            await handler.on_get_task(GetTaskRequest(id=root_task_id), other_context)

        other_events = await drain_terminal(
            handler.on_message_send_stream(request("other-root", "Separate owner work"), other_context)
        )
        assert any(
            isinstance(event, TaskStatusUpdateEvent) and event.status.state == TaskState.TASK_STATE_COMPLETED
            for event in other_events
        )
        with pytest.raises(AuthorizationUnavailable):
            await registry.inspect(approval_id, other_owner)

        if outcome == "deny":
            await registry.decide(approval_id, alice, record.nonce, False, record.digest)
            public_events.extend(await asyncio.wait_for(drain_terminal(stream), HANG_GUARD))
            final_state = TaskState.TASK_STATE_REJECTED
        else:
            canceled = await handler.on_cancel_task(CancelTaskRequest(id=root_task_id), alice_context)
            assert canceled is not None and canceled.status.state == TaskState.TASK_STATE_CANCELED
            public_events.append(canceled)
            await stream.aclose()
            final_state = TaskState.TASK_STATE_CANCELED

        final_task = await handler.on_get_task(GetTaskRequest(id=root_task_id), alice_context)
        assert final_task is not None and final_task.status.state == final_state
        assert not (workspace / action_path).exists()
        public_wire = "\n".join(str(event) for event in public_events) + str(final_task)
        for private in (
            private_reasoning,
            private_argument,
            "PRIVATE_ROOT_REASONING_MARKER",
            "PRIVATE_CHILD_REASONING_MARKER",
            "grandchild-write",
            "write",
        ):
            assert private not in public_wire
    finally:
        await stream.aclose()
        await executor.close()
        await registry.close()
