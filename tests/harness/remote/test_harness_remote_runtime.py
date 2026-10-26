"""Native harness delegation through the real SDK client to a local A2A peer."""

from __future__ import annotations

import asyncio
import base64
import os
from contextlib import aclosing
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import httpx
import pytest
from a2a.types import Artifact
from a2a.types import Part
from a2a.types import StreamResponse
from a2a.types import Task
from a2a.types import TaskArtifactUpdateEvent
from a2a.types import TaskState
from a2a.types import TaskStatus
from a2a.types import TaskStatusUpdateEvent

from nagents import _sqlite
from nagents.a2a.client import transport as a2a_client
from nagents.a2a.client.conversation import RemoteConversation
from nagents.a2a.server.application import create_app
from nagents.a2a.server.config import A2AConfig
from nagents.a2a.server.config import IssuerConfig
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.remote.artifacts import referenced_images
from nagents.harness.remote.profiles import RemoteProfile
from nagents.harness.remote.runtime import _load_state
from nagents.harness.remote.runtime import owned_remote_task
from nagents.harness.remote.runtime import run_remote
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import ImageContent
from nagents.types import Message
from tests.a2a.test_a2a_auth import AUDIENCE
from tests.a2a.test_a2a_auth import IssuerState
from tests.a2a.test_a2a_auth import access_claims
from tests.a2a.test_a2a_auth import local_issuer
from tests.a2a.test_a2a_auth import make_token
from tests.a2a.test_a2a_auth import signing_key
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from a2a.types import SendMessageRequest

    from nagents.a2a.agent.runtime import AgentContext
    from nagents.a2a.client.transport import RemoteAgent
    from nagents.types import GenerationConfig
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
                    name="to_agent",
                    arguments={"agent": "Remote agent", "prompt": "Return the deterministic peer answer."},
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
            delegate = schemas[1]["to_agent"]
            assert isinstance(delegate, dict)
            properties = delegate["properties"]
            assert isinstance(properties, dict)
            agent = properties["agent"]
            assert isinstance(agent, dict)
            assert "Remote agent" in agent["enum"]
            assert "peer" not in agent["enum"]
            assert observed == ["Return the deterministic peer answer."]
            delegated = next(info for info in harness.tasks.list() if info.profile == "peer")
            worker = harness.tasks._workers[delegated.session_id]
            await worker
            child_history = await harness.tasks.history(delegated.session_id)
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
async def test_remote_state_load_queues_behind_active_session_database_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    entered, release = asyncio.Event(), asyncio.Event()
    original_execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

    async def pause_active_message_write(
        connection: aiosqlite.Connection, operation: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        result = await original_execute(connection, operation, *args, **kwargs)
        statement = args[0] if args else None
        if (
            isinstance(statement, str)
            and statement.lstrip().startswith("INSERT INTO v2_messages")
            and not entered.is_set()
        ):
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(aiosqlite.Connection, "_execute", pause_active_message_write)
    active_write: asyncio.Task[int] | None = None
    state_load: asyncio.Task[tuple[str, str, str, int, int] | None] | None = None
    queued_write: asyncio.Task[int] | None = None
    try:
        await asyncio.wait_for(harness.initialize(), HANG_GUARD)
        active_write = asyncio.create_task(
            harness.agent.session.add_message(harness.session_id, Message(role="user", content="active write"))
        )
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        state_load = asyncio.create_task(_load_state(harness))
        queued_write = asyncio.create_task(
            harness.agent.session.add_message(harness.session_id, Message(role="user", content="queued write"))
        )
        async with asyncio.timeout(HANG_GUARD):
            while sum(gate.users for gates in _sqlite._gates.values() for gate in gates.values()) < 3:
                await asyncio.sleep(0)
        assert not state_load.done() and not queued_write.done()
        release.set()
        await asyncio.wait_for(active_write, HANG_GUARD)
        assert await asyncio.wait_for(state_load, HANG_GUARD) is None
        await asyncio.wait_for(queued_write, HANG_GUARD)
        assert [message.content for message in await harness.agent.session.get_history(harness.session_id)] == [
            "active write",
            "queued write",
        ]
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (active_write, state_load, queued_write) if task is not None), return_exceptions=True
        )
        await harness.close()


@pytest.mark.asyncio
async def test_binding_error_consumer_can_write_session_database_while_stream_is_suspended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    store = harness._agent_catalog.remote_store
    store.save(
        {
            "peer": AgentProfile(
                description="Configured peer",
                a2a=RemoteProfile(url="https://peer.example"),
            )
        },
        expected=store.load().revision,
    )
    assert harness.refresh_agent_definitions()
    try:
        await harness.set_agent("peer")
        await asyncio.wait_for(harness.initialize(), HANG_GUARD)
        await _load_state(harness)
        async with aiosqlite.connect(harness.agent.session.db_path) as db:
            await db.execute(
                "INSERT INTO ngn_remote_sessions VALUES(?,?,?,?,?,?,?)",
                (harness.session_id, "stale-binding", "", "", "previous-message", 0, 0),
            )
            await db.commit()

        async with aclosing(run_remote(harness, "This should stop at the binding check.")) as stream:
            event = await asyncio.wait_for(anext(stream), HANG_GUARD)
            assert isinstance(event, ErrorEvent)
            assert event.code == "A2A_BINDING_CHANGED"
            history_id = await asyncio.wait_for(
                harness.agent.session.add_message(
                    harness.session_id, Message(role="assistant", content="consumer database write")
                ),
                HANG_GUARD,
            )
            assert history_id > 0
            history = await harness.agent.session.get_history(harness.session_id)
            assert history[-1].content == "consumer database write"
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_image", [True, False], ids=["complete", "incomplete"])
async def test_local_cancel_retains_only_complete_remote_child_images_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete_image: bool
) -> None:
    image_data = (Path(__file__).parents[2] / "fixtures" / "a2a-images" / "red.png").read_bytes()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="unused")

    class BlockingImagePeer:
        def __init__(self) -> None:
            self.card = type("Card", (), {"name": "Image peer", "description": ""})()
            self.started = asyncio.Event()
            self.sent: list[object] = []
            self.canceled: list[str] = []

        async def __aenter__(self) -> BlockingImagePeer:
            return self

        async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
            del exc_type, exc, traceback

        async def send(self, request: object) -> AsyncGenerator[StreamResponse, None]:
            self.sent.append(request)
            yield StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="peer-task-image-stop",
                    context_id="peer-context-image-stop",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
            yield StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    task_id="peer-task-image-stop",
                    context_id="peer-context-image-stop",
                    artifact=Artifact(
                        artifact_id="stopped-image",
                        parts=[
                            Part(text="A distinct image caption"),
                            Part(raw=image_data, media_type="image/png", filename="stopped.png"),
                        ],
                        description="A distinct image caption",
                    ),
                    last_chunk=complete_image,
                )
            )
            self.started.set()
            await asyncio.Event().wait()

        async def cancel(self, task_id: str) -> Task:
            self.canceled.append(task_id)
            return Task(
                id=task_id,
                context_id="peer-context-image-stop",
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    peer = BlockingImagePeer()
    monkeypatch.setattr(a2a_client, "RemoteAgent", lambda *args, **kwargs: peer)
    store = harness._agent_catalog.remote_store
    store.save(
        {
            "peer": AgentProfile(
                description="Configured image peer",
                a2a=RemoteProfile(url="https://peer.example"),
            )
        },
        expected=store.load().revision,
    )
    assert harness.refresh_agent_definitions()

    async def approve(request: object) -> bool:
        del request
        return True

    harness.approval_handler = approve
    try:
        await asyncio.wait_for(harness.initialize(), HANG_GUARD)
        harness.tasks.begin(harness.session_id)
        accepted = await asyncio.wait_for(harness.tasks.to_agent("Return a captioned image.", agent="peer"), HANG_GUARD)
        await asyncio.wait_for(peer.started.wait(), HANG_GUARD)
        worker = harness.tasks._workers[accepted["session_id"]]
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, HANG_GUARD)

        info = harness.tasks._lookup(accepted["session_id"])
        assert info.status == "cancelled"
        assert len(peer.sent) == 1
        assert peer.canceled == ["peer-task-image-stop"]
        history = await harness.tasks.history(info.session_id)
        assistant_images = [
            part
            for message in history
            if message.role == "assistant" and isinstance(message.content, list)
            for part in message.content
            if isinstance(part, ImageContent)
        ]
        if complete_image:
            assert len(assistant_images) == 1
            assert assistant_images[0].caption == "A distinct image caption"
            assert len(info.artifacts) == 1
            assert info.artifacts[0].caption == "A distinct image caption"
            owned = await referenced_images(
                harness.agent.session.db_path,
                harness.session_id,
                info._storage_id,
                info.artifacts,
            )
            assert len(owned) == 1
            assert owned[0].base64_data == base64.b64encode(image_data).decode("ascii")
            assert owned[0].caption == "A distinct image caption"
        else:
            assert assistant_images == []
            assert info.artifacts == ()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cancel_during_committed_remote_image_insert_does_not_duplicate_history_or_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_data = (Path(__file__).parents[2] / "fixtures" / "a2a-images" / "red.png").read_bytes()

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        del provider, messages
        yield TextDoneEvent(text="unused")

    class CompletingImagePeer:
        def __init__(self) -> None:
            self.card = type("Card", (), {"name": "Image peer", "description": ""})()
            self.image_sent = asyncio.Event()
            self.complete = asyncio.Event()
            self.sent: list[object] = []

        async def __aenter__(self) -> CompletingImagePeer:
            return self

        async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
            del exc_type, exc, traceback

        async def send(self, request: object) -> AsyncGenerator[StreamResponse, None]:
            self.sent.append(request)
            yield StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="peer-task-image-complete",
                    context_id="peer-context-image-complete",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            )
            yield StreamResponse(
                artifact_update=TaskArtifactUpdateEvent(
                    task_id="peer-task-image-complete",
                    context_id="peer-context-image-complete",
                    artifact=Artifact(
                        artifact_id="completed-image",
                        parts=[
                            Part(text="A committed image caption"),
                            Part(raw=image_data, media_type="image/png", filename="committed.png"),
                        ],
                        description="A committed image caption",
                    ),
                    last_chunk=True,
                )
            )
            self.image_sent.set()
            await self.complete.wait()
            yield StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="peer-task-image-complete",
                    context_id="peer-context-image-complete",
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                )
            )

        async def cancel(self, task_id: str) -> Task:
            raise AssertionError(f"A completed remote task must not be canceled: {task_id}")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    peer = CompletingImagePeer()
    monkeypatch.setattr(a2a_client, "RemoteAgent", lambda *args, **kwargs: peer)
    store = harness._agent_catalog.remote_store
    store.save(
        {
            "peer": AgentProfile(
                description="Configured image peer",
                a2a=RemoteProfile(url="https://peer.example"),
            )
        },
        expected=store.load().revision,
    )
    assert harness.refresh_agent_definitions()

    async def approve(request: object) -> bool:
        del request
        return True

    harness.approval_handler = approve
    entered_insert, release_insert = asyncio.Event(), asyncio.Event()
    original_execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

    async def pause_actual_insert(
        connection: aiosqlite.Connection, operation: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        result = await original_execute(connection, operation, *args, **kwargs)
        statement = args[0] if args else None
        if (
            isinstance(statement, str)
            and statement.lstrip().startswith("INSERT INTO v2_messages")
            and not entered_insert.is_set()
        ):
            entered_insert.set()
            await release_insert.wait()
        return result

    worker: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(harness.initialize(), HANG_GUARD)
        harness.tasks.begin(harness.session_id)
        accepted = await asyncio.wait_for(harness.tasks.to_agent("Complete this image task.", agent="peer"), HANG_GUARD)
        await asyncio.wait_for(peer.image_sent.wait(), HANG_GUARD)
        monkeypatch.setattr(aiosqlite.Connection, "_execute", pause_actual_insert)
        peer.complete.set()
        worker = harness.tasks._workers[accepted["session_id"]]
        await asyncio.wait_for(entered_insert.wait(), HANG_GUARD)
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        release_insert.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, HANG_GUARD)

        info = harness.tasks._lookup(accepted["session_id"])
        assert info.status == "cancelled"
        assert len(peer.sent) == 1
        child_history = await harness.tasks.history(info.session_id)
        assistant_messages = [message for message in child_history if message.role == "assistant"]
        assistant_images = [
            part
            for message in assistant_messages
            if isinstance(message.content, list)
            for part in message.content
            if isinstance(part, ImageContent)
        ]
        assert len(assistant_messages) == 1
        assert len(assistant_images) == 1
        assert len(info.artifacts) == 1
        assert info.artifacts[0].caption == "A committed image caption"
        owned = await referenced_images(
            harness.agent.session.db_path,
            harness.session_id,
            info._storage_id,
            info.artifacts,
        )
        assert len(owned) == 1
        assert owned[0].base64_data == base64.b64encode(image_data).decode("ascii")
        assert owned[0].caption == "A committed image caption"
    finally:
        release_insert.set()
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
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
