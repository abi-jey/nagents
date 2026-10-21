"""Native harness delegation through the real SDK client to a local A2A peer."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import httpx
import pytest
from a2a.types import Task
from a2a.types import TaskState
from a2a.types import TaskStatus

from nagents.a2a import client as a2a_client
from nagents.a2a.application import create_app
from nagents.a2a.config import A2AConfig
from nagents.a2a.config import IssuerConfig
from nagents.a2a.conversation import RemoteConversation
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote_profiles import RemoteProfile
from nagents.harness.remote_runtime import owned_remote_task
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness
from tests.test_a2a_auth import AUDIENCE
from tests.test_a2a_auth import IssuerState
from tests.test_a2a_auth import access_claims
from tests.test_a2a_auth import local_issuer
from tests.test_a2a_auth import make_token
from tests.test_a2a_auth import signing_key

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from pathlib import Path

    from a2a.types import SendMessageRequest
    from a2a.types import StreamResponse

    from nagents.a2a.agent import AgentContext
    from nagents.a2a.client import RemoteAgent
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Harness and A2A SQLite fixtures require POSIX")


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    config_home.mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    config_home.chmod(0o700)
    data_home.chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


class DeterministicPeerProvider(Provider):
    def __init__(self, observed: list[str]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "fixture-only-key", "fixture-peer")
        self.observed = observed

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
        del tools, config, stream, verify_model
        self.observed.append(str(messages[-1].content))
        yield TextChunkEvent(chunk="Remote peer answer.")
        yield TextDoneEvent(text="Remote peer answer.")
        yield DoneEvent(final_text="Remote peer answer.")


@asynccontextmanager
async def peer_application(tmp_path: Path) -> AsyncIterator[tuple[object, str, list[str]]]:
    key = signing_key("RS256", "harness-peer")
    async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
        token = make_token(
            key,
            access_claims(
                issuer,
                aud=AUDIENCE,
                scope="ngn:access ngn:invoke ngn:read ngn:cancel",
                jti="harness-peer-token",
            ),
        )
        observed: list[str] = []
        config = A2AConfig(
            public_url="https://peer.example",
            agent="assistant",
            issuer=IssuerConfig(issuer=issuer, audience=AUDIENCE),
            data_dir=(tmp_path / "peer-state").resolve(),
            loopback_http=True,
        )

        def factory(context: AgentContext) -> Agent:
            return Agent(
                DeterministicPeerProvider(observed),
                SessionManager(context.data_dir / "sessions.db"),
                system_prompt="Deterministic A2A peer fixture.",
                streaming=True,
                compaction_strategy=None,
                save_tool_outputs=False,
            )

        app = create_app(config=config, agent_factory=factory)
        async with app.router.lifespan_context(app):
            yield app, token, observed


@pytest.mark.asyncio
async def test_configure_then_delegate_uses_remote_sdk_and_preserves_remote_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with peer_application(tmp_path) as (peer_app, token, observed):
        monkeypatch.setenv("NGN_TEST_REMOTE_TOKEN", token)
        actual_remote_agent = a2a_client.RemoteAgent

        def with_asgi_transport(
            url: str,
            tokens: object = None,
            **kwargs: object,
        ) -> a2a_client.RemoteAgent:
            kwargs["transport"] = httpx.ASGITransport(app=peer_app)  # type: ignore[arg-type]
            return actual_remote_agent(url, tokens, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(a2a_client, "RemoteAgent", with_asgi_transport)
        schemas: list[dict[str, object]] = []
        original_generate = FakeProvider.generate
        round_number = 0

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            nonlocal round_number
            round_number += 1
            if round_number == 1:
                yield ToolCallEvent(
                    id="configure-remote",
                    name="remote_agent_configure",
                    arguments={
                        "name": "peer",
                        "url": "https://peer.example",
                        "description": "Deterministic remote peer",
                        "token_env": "NGN_TEST_REMOTE_TOKEN",
                    },
                )
            elif round_number == 2:
                yield ToolCallEvent(
                    id="delegate-remote",
                    name="delegate",
                    arguments={"agent": "peer", "prompt": "Return the deterministic peer answer."},
                )
            else:
                yield TextDoneEvent(text="Remote work was delegated.")

        async def capture_generate(
            self: FakeProvider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            schemas.append({tool.name: dict(tool.parameters) for tool in tools or []})
            async for event in original_generate(self, messages, tools, config, stream, verify_model):
                yield event

        monkeypatch.setattr(FakeProvider, "generate", capture_generate)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def approve(request: object) -> bool:
            del request
            return True

        harness.approval_handler = approve
        try:
            await collect(harness, "Configure and use the peer in this conversation.")

            assert round_number >= 3
            assert len(schemas) >= 3
            delegate = schemas[1]["delegate"]
            assert isinstance(delegate, dict)
            properties = delegate["properties"]
            assert isinstance(properties, dict)
            agent = properties["agent"]
            assert isinstance(agent, dict)
            assert "peer" in agent["enum"]
            assert observed == ["Return the deterministic peer answer."]
            delegated = next(info for info in harness.tasks.list() if info.profile == "peer")
            worker = harness.tasks._workers[delegated.id]
            await worker
            child_history = await harness.tasks.history(delegated.id)
            assert delegated.status == "completed"
            assert any("Remote peer answer." in str(message.content) for message in child_history)
            assert sum(len(provider.requests) for provider in providers) == round_number
        finally:
            await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_mode", ["aclose", "cancelled"])
async def test_owned_remote_task_cancels_on_generator_close_and_not_after_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_mode: str
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    task_id = "remote-running-task"
    message_id = "accepted-message"

    class CancellationPeer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def cancel(self, identifier: str) -> Task:
            self.calls.append(identifier)
            return Task(
                id=identifier,
                context_id="remote-context",
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )

    remote_impl = CancellationPeer()
    remote = cast("RemoteAgent", remote_impl)
    conversation = RemoteConversation(remote, task_id=task_id, context_id="remote-context")
    conversation.state = TaskState.TASK_STATE_WORKING
    database = harness.agent.session.db_path
    try:
        await asyncio.wait_for(harness.initialize(), 10)
        async with aiosqlite.connect(database) as db:
            await db.execute(
                "CREATE TABLE ngn_remote_sessions (session_id TEXT PRIMARY KEY, binding TEXT NOT NULL, "
                "context_id TEXT NOT NULL, task_id TEXT NOT NULL, message_id TEXT NOT NULL, "
                "state INTEGER NOT NULL, pending INTEGER NOT NULL)"
            )
            await db.execute(
                "INSERT INTO ngn_remote_sessions VALUES(?,?,?,?,?,?,1)",
                (
                    harness.session_id,
                    "peer-binding",
                    "remote-context",
                    task_id,
                    message_id,
                    TaskState.TASK_STATE_WORKING,
                ),
            )
            await db.commit()

        async def chunk_stream() -> AsyncGenerator[str, None]:
            async with owned_remote_task(remote, conversation, harness, message_id):
                yield "accepted remote chunk"

        if close_mode == "aclose":
            stream = chunk_stream()
            assert await asyncio.wait_for(anext(stream), 5) == "accepted remote chunk"
            await asyncio.wait_for(stream.aclose(), 12)
        else:
            started = asyncio.Event()

            async def block_in_owned_task() -> None:
                async with owned_remote_task(remote, conversation, harness, message_id):
                    started.set()
                    await asyncio.wait_for(asyncio.Event().wait(), 10)

            running = asyncio.create_task(block_in_owned_task())
            await asyncio.wait_for(started.wait(), 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 12)

        assert remote_impl.calls == [task_id]
        async with (
            aiosqlite.connect(database) as db,
            db.execute(
                "SELECT task_id,state,pending FROM ngn_remote_sessions WHERE session_id=?",
                (harness.session_id,),
            ) as cursor,
        ):
            active_row = await cursor.fetchone()
        assert active_row == (task_id, TaskState.TASK_STATE_CANCELED, 0)

        conversation.state = TaskState.TASK_STATE_CANCELED

        async def terminal_stream() -> AsyncGenerator[str, None]:
            async with owned_remote_task(remote, conversation, harness, message_id):
                yield "already terminal"

        stream = terminal_stream()
        assert await asyncio.wait_for(anext(stream), 5) == "already terminal"
        await asyncio.wait_for(stream.aclose(), 5)
        assert remote_impl.calls == [task_id]
        async with (
            aiosqlite.connect(database) as db,
            db.execute(
                "SELECT task_id,state,pending FROM ngn_remote_sessions WHERE session_id=?",
                (harness.session_id,),
            ) as cursor,
        ):
            terminal_row = await cursor.fetchone()
        assert terminal_row == (task_id, TaskState.TASK_STATE_CANCELED, 0)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_set_agent_runs_remote_without_local_provider_and_records_matching_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with peer_application(tmp_path) as (peer_app, token, observed):
        monkeypatch.setenv("NGN_TEST_REMOTE_TOKEN", token)
        actual_remote_agent = a2a_client.RemoteAgent

        def with_asgi_transport(url: str, tokens: object = None, **kwargs: object) -> a2a_client.RemoteAgent:
            kwargs["transport"] = httpx.ASGITransport(app=peer_app)  # type: ignore[arg-type]
            return actual_remote_agent(url, tokens, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(a2a_client, "RemoteAgent", with_asgi_transport)

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider, messages
            yield TextDoneEvent(text="Local provider must not run")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        store = harness._agent_catalog.remote_store
        store.save(
            {
                "peer": AgentProfile(
                    description="Configured peer",
                    a2a=RemoteProfile(url="https://peer.example", token_env="NGN_TEST_REMOTE_TOKEN"),
                )
            },
            expected=store.load().revision,
        )
        assert harness.refresh_agent_definitions()
        try:
            await harness.set_agent("peer")
            events = await collect(harness, "Run as the peer.")
            completed = [event for event in events if isinstance(event, TextDoneEvent)]
            metadata = [event for event in events if isinstance(event, ResponseMetadataEvent)]

            assert len(completed) == 1
            assert completed[0].text == "Remote peer answer."
            assert len(metadata) == 1
            assert metadata[0].history_id
            assert metadata[0].response_id == completed[0].extra["response_id"]
            assert observed == ["Run as the peer."]
            assert sum(len(provider.requests) for provider in providers) == 0
        finally:
            await harness.close()


@pytest.mark.asyncio
async def test_completed_remote_requests_in_one_local_session_reuse_context_with_new_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with peer_application(tmp_path) as (peer_app, token, observed):
        monkeypatch.setenv("NGN_TEST_REMOTE_TOKEN", token)
        actual_remote_agent = a2a_client.RemoteAgent
        sent_messages: list[SendMessageRequest] = []
        response_tasks: set[tuple[str, str]] = set()

        class RecordingRemote:
            def __init__(self, url: str, tokens: object = None, **kwargs: object) -> None:
                kwargs["transport"] = httpx.ASGITransport(app=peer_app)  # type: ignore[arg-type]
                self.remote = actual_remote_agent(url, tokens, **kwargs)  # type: ignore[arg-type]
                self.card: object = None

            async def __aenter__(self) -> RecordingRemote:
                await self.remote.__aenter__()
                self.card = self.remote.card
                return self

            async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
                await self.remote.__aexit__(exc_type, exc, traceback)  # type: ignore[arg-type]

            async def send(self, request: SendMessageRequest) -> AsyncGenerator[StreamResponse, None]:
                sent_messages.append(request)
                async for event in self.remote.send(request):
                    kind = event.WhichOneof("payload")
                    if kind == "task":
                        response_tasks.add((event.task.id, event.task.context_id))
                    elif kind == "status_update":
                        response_tasks.add((event.status_update.task_id, event.status_update.context_id))
                    elif kind == "artifact_update":
                        response_tasks.add((event.artifact_update.task_id, event.artifact_update.context_id))
                    yield event

        monkeypatch.setattr(a2a_client, "RemoteAgent", RecordingRemote)

        async def unused_local_provider(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider, messages
            yield TextDoneEvent(text="Local provider must not run")

        harness, providers = setup_harness(tmp_path, monkeypatch, unused_local_provider)
        store = harness._agent_catalog.remote_store
        profile = AgentProfile(
            description="Configured peer",
            a2a=RemoteProfile(url="https://peer.example", token_env="NGN_TEST_REMOTE_TOKEN"),
        )
        store.save({"peer": profile}, expected=store.load().revision)
        assert harness.refresh_agent_definitions()
        try:
            await harness.set_agent("peer")
            first = await collect(harness, "First request in this local session.")
            second = await collect(harness, "Second request in this local session.")

            assert len([event for event in first if isinstance(event, TextDoneEvent)]) == 1
            assert len([event for event in second if isinstance(event, TextDoneEvent)]) == 1
            assert observed == ["First request in this local session.", "Second request in this local session."]
            assert len(sent_messages) == 2
            first_message, second_message = (request.message for request in sent_messages)
            assert not first_message.context_id and not first_message.task_id
            assert second_message.context_id
            assert second_message.context_id in {context for _, context in response_tasks}
            assert not second_message.task_id
            assert len(response_tasks) == 2
            task_ids = {task for task, _ in response_tasks}
            context_ids = {context for _, context in response_tasks}
            assert len(task_ids) == 2
            assert len(context_ids) == 1
            assert sum(len(provider.requests) for provider in providers) == 0
        finally:
            await harness.close()
