"""A2A image parts retain their bytes and protocol identity."""

from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from a2a.helpers import new_text_message
from a2a.types import AgentCapabilities
from a2a.types import AgentCard
from a2a.types import AgentInterface
from a2a.types import AgentSkill
from a2a.types import Artifact
from a2a.types import Part
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import StreamResponse
from a2a.types import Task
from a2a.types import TaskArtifactUpdateEvent
from a2a.types import TaskState
from a2a.types import TaskStatus
from a2a.types import TaskStatusUpdateEvent

from nagents.a2a import RemoteAgent
from nagents.a2a.client.conversation import RemoteConversation
from nagents.a2a.client.transport import RemoteAgentError
from nagents.a2a.images import ImagePublisher
from nagents.a2a.images import current_image_publisher
from nagents.a2a.images import publish_image
from nagents.a2a.server.application import create_app
from nagents.a2a.server.application import create_harness_app
from nagents.a2a.server.config import A2AConfig
from nagents.a2a.server.config import IssuerConfig
from nagents.agent import Agent
from nagents.artifacts import ImageArtifact
from nagents.artifacts import ImageArtifactReference
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.agents.updates import format_batch
from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.remote.artifacts import image_references
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage
from nagents.media import MediaCapabilities
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from tests.a2a.test_a2a_auth import AUDIENCE
from tests.a2a.test_a2a_auth import IssuerState
from tests.a2a.test_a2a_auth import access_claims
from tests.a2a.test_a2a_auth import local_issuer
from tests.a2a.test_a2a_auth import make_token
from tests.a2a.test_a2a_auth import signing_key
from tests.a2a.test_a2a_client import serve_asgi
from tests.support.config import connection
from tests.support.providers import FakeProvider
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator

    from a2a.server.events import EventQueue

    from nagents.a2a.agent import AgentContext
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition


class RemoteStub:
    async def get_task(self, task_id: str) -> Task:
        raise AssertionError(task_id)

    async def subscribe(self, task_id: str) -> AsyncGenerator[StreamResponse, None]:
        del task_id
        if False:
            yield StreamResponse()


@pytest.fixture
def images() -> dict[str, bytes]:
    fixture_dir = Path(__file__).parents[1] / "fixtures" / "a2a-images"
    return {"red.png": (fixture_dir / "red.png").read_bytes(), "blue.jpg": (fixture_dir / "blue.jpg").read_bytes()}


def conversation() -> RemoteConversation:
    return RemoteConversation(RemoteStub())  # type: ignore[arg-type]


def event(artifact: Artifact, *, append: bool = False, last_chunk: bool = False) -> StreamResponse:
    return StreamResponse(
        artifact_update=TaskArtifactUpdateEvent(
            task_id="remote-task",
            context_id="remote-context",
            artifact=artifact,
            append=append,
            last_chunk=last_chunk,
        )
    )


def test_image_only_and_mixed_parts_keep_exact_raw_bytes_and_identity(images: dict[str, bytes]) -> None:
    remote = conversation()
    first = remote.accept(
        event(
            Artifact(
                artifact_id="thumbnail",
                parts=[
                    Part(raw=images["red.png"], media_type="image/png", filename="poster.png"),
                ],
                description="Series thumbnail",
            ),
            last_chunk=True,
        )
    )
    assert first.text == "Series thumbnail"
    assert len(first.images) == 1
    image = first.images[0]
    assert (image.artifact_id, image.artifact_part, image.data, image.media_type, image.filename) == (
        "thumbnail",
        0,
        images["red.png"],
        "image/png",
        "poster.png",
    )
    assert image.caption == "Series thumbnail"
    mixed = remote.accept(
        event(
            Artifact(
                artifact_id="mixed",
                parts=[
                    Part(text="Cover image"),
                    Part(raw=images["blue.jpg"], media_type="image/jpeg", filename="cover.jpg"),
                ],
            ),
            last_chunk=True,
        )
    )
    assert mixed.text == "Series thumbnail\n\nCover image"
    assert [(item.artifact_id, item.artifact_part, item.data, item.caption) for item in mixed.images] == [
        ("thumbnail", 0, images["red.png"], "Series thumbnail"),
        ("mixed", 1, images["blue.jpg"], "Cover image"),
    ]


def test_appended_parts_and_repeated_snapshots_do_not_duplicate_or_publish_early(images: dict[str, bytes]) -> None:
    remote = conversation()
    remote.accept(event(Artifact(artifact_id="parts", parts=[Part(text="A caption")]), last_chunk=False))
    incomplete = remote.accept(
        event(
            Artifact(
                artifact_id="parts",
                parts=[
                    Part(raw=images["red.png"], media_type="image/png", filename="red.png"),
                ],
            ),
            append=True,
            last_chunk=False,
        )
    )
    assert incomplete.images == ()
    complete = remote.accept(
        event(
            Artifact(
                artifact_id="parts",
                parts=[
                    Part(raw=images["blue.jpg"], media_type="image/jpeg", filename="blue.jpg"),
                ],
            ),
            append=True,
            last_chunk=True,
        )
    )
    assert [(part.artifact_part, part.data) for part in complete.images] == [
        (1, images["red.png"]),
        (2, images["blue.jpg"]),
    ]
    snapshot = StreamResponse(
        task=Task(
            id="remote-task",
            context_id="remote-context",
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            artifacts=[
                Artifact(
                    artifact_id="parts",
                    parts=[
                        Part(text="A caption"),
                        Part(raw=images["red.png"], media_type="image/png", filename="red.png"),
                        Part(raw=images["blue.jpg"], media_type="image/jpeg", filename="blue.jpg"),
                    ],
                )
            ],
        )
    )
    projected = remote.accept(snapshot)
    assert [(part.artifact_part, part.data) for part in projected.images] == [
        (1, images["red.png"]),
        (2, images["blue.jpg"]),
    ]


def test_failed_terminal_retains_only_previously_complete_images(images: dict[str, bytes]) -> None:
    remote = conversation()
    remote.accept(
        event(
            Artifact(
                artifact_id="complete",
                parts=[
                    Part(raw=images["red.png"], media_type="image/png", filename="done.png"),
                ],
            ),
            last_chunk=True,
        )
    )
    remote.accept(
        event(
            Artifact(
                artifact_id="incomplete",
                parts=[
                    Part(raw=images["blue.jpg"], media_type="image/jpeg", filename="pending.jpg"),
                ],
            ),
            last_chunk=False,
        )
    )
    failed = StreamResponse(
        status_update=TaskStatusUpdateEvent(
            task_id="remote-task",
            context_id="remote-context",
            status=TaskStatus(state=TaskState.TASK_STATE_FAILED),
        )
    )
    update = remote.accept(failed)
    assert update.terminal
    assert [(image.artifact_id, image.data) for image in remote.images] == [("complete", images["red.png"])]


@pytest.mark.parametrize(
    "part,code",
    [
        (Part(url="https://127.0.0.1/private.png", media_type="image/png"), "A2A_ARTIFACT_URL"),
        (Part(raw=b"not an image", media_type="image/png"), "A2A_ARTIFACT_INVALID"),
        (Part(raw=b"raw", media_type="application/octet-stream"), "A2A_ARTIFACT_TYPE"),
    ],
)
def test_remote_image_errors_are_explicit(part: Part, code: str) -> None:
    with pytest.raises(RemoteAgentError) as raised:
        conversation().accept(event(Artifact(artifact_id="bad", parts=[part]), last_chunk=True))
    assert raised.value.code == code


def test_remote_image_count_limit_is_reported(images: dict[str, bytes]) -> None:
    remote = conversation()
    for index in range(3):
        remote.accept(
            event(
                Artifact(
                    artifact_id=f"image-{index}",
                    parts=[
                        Part(raw=images["red.png"], media_type="image/png", filename=f"{index}.png"),
                    ],
                ),
                last_chunk=True,
            )
        )
    with pytest.raises(RemoteAgentError) as raised:
        remote.accept(
            event(
                Artifact(
                    artifact_id="image-4",
                    parts=[
                        Part(raw=images["blue.jpg"], media_type="image/jpeg", filename="4.jpg"),
                    ],
                ),
                last_chunk=True,
            )
        )
    assert raised.value.code == "A2A_ARTIFACT_LIMIT"


def test_remote_image_byte_limit_is_reported() -> None:
    oversized = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(8 * 1024 * 1024 - 25) + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    with pytest.raises(RemoteAgentError) as raised:
        conversation().accept(
            event(
                Artifact(
                    artifact_id="oversized",
                    parts=[
                        Part(raw=oversized, media_type="image/png", filename="huge.png"),
                    ],
                ),
                last_chunk=True,
            )
        )
    assert raised.value.code == "A2A_ARTIFACT_LIMIT"


class PublishingProvider(Provider):
    def __init__(self, image_bytes: bytes) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "fixture-key", "fixture-model")
        self.image_bytes = image_bytes
        self.observed: list[str] = []

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
    ) -> AsyncGenerator[Event, None]:
        del messages, tools, config, stream, verify_model
        identifier = await publish_image(self.image_bytes, media_type="image/png", filename="published.png")
        self.observed.append(identifier)
        yield TextDoneEvent(text="The image is ready.")
        yield DoneEvent(final_text="The image is ready.")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")
async def test_independent_sdk_client_receives_raw_image_from_nagents_server(
    tmp_path: Path, images: dict[str, bytes]
) -> None:
    key = signing_key("RS256", "image-server")
    async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
        server_socket = socket.socket()
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
        server_socket.setblocking(False)
        origin = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
        config = A2AConfig(
            public_url=origin,
            agent="assistant",
            issuer=IssuerConfig(issuer=issuer, audience=AUDIENCE),
            data_dir=(tmp_path / "image-server-state").resolve(),
            loopback_http=True,
        )
        provider = PublishingProvider(images["red.png"])

        def factory(context: AgentContext) -> Agent:
            return Agent(
                provider,
                SessionManager(context.data_dir / "sessions.db"),
                system_prompt="A2A image export test.",
                streaming=True,
                compaction_strategy=None,
                save_tool_outputs=False,
            )

        app = create_app(config=config, agent_factory=factory)
        async with serve_asgi(app, sock=server_socket):

            async def token() -> str:
                return make_token(
                    key,
                    access_claims(
                        issuer,
                        aud=AUDIENCE,
                        scope="ngn:access ngn:invoke ngn:read ngn:cancel",
                        jti="a2a-image-client",
                    ),
                )

            async with RemoteAgent(origin, token, loopback_http=True, timeout=10) as client:
                message = new_text_message("publish the fixture image")
                message.role = Role.ROLE_USER
                message.message_id = "a2a-image-client-0001"
                events = [item async for item in client.send(SendMessageRequest(message=message))]
                task = next(item.task for item in reversed(events) if item.HasField("task"))
                saved = await client.get_task(task.id)
                raw_parts = [
                    part
                    for artifact in saved.artifacts
                    for part in artifact.parts
                    if part.WhichOneof("content") == "raw"
                ]
                assert len(raw_parts) == 1
                assert raw_parts[0].raw == images["red.png"]
                assert raw_parts[0].media_type == "image/png"
                assert raw_parts[0].filename == "published.png"
                assert provider.observed and provider.observed[0] not in " ".join(
                    part.text
                    for artifact in saved.artifacts
                    for part in artifact.parts
                    if part.WhichOneof("content") == "text"
                )
                assert any(
                    part.WhichOneof("content") == "text" for artifact in saved.artifacts for part in artifact.parts
                )


@pytest.mark.asyncio
async def test_ngn_remote_conversation_reads_independent_sdk_server_artifacts(
    images: dict[str, bytes],
) -> None:
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.agent_execution import RequestContext
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import create_agent_card_routes
    from a2a.server.routes import create_jsonrpc_routes
    from a2a.server.tasks import TaskUpdater
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI

    class ImageExecutor(AgentExecutor):
        async def execute(self, context: object, event_queue: EventQueue) -> None:
            assert isinstance(context, RequestContext)
            assert context.task_id is not None and context.context_id is not None
            updater = TaskUpdater(event_queue, context.task_id, context.context_id)
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )
            await updater.start_work()
            await updater.add_artifact(
                [
                    Part(text="A mixed artifact"),
                    Part(raw=images["red.png"], media_type="image/png", filename="red.png"),
                ],
                artifact_id="mixed",
                last_chunk=True,
            )
            await updater.add_artifact(
                [Part(raw=images["blue.jpg"], media_type="image/jpeg", filename="blue.jpg")],
                artifact_id="image-only",
                last_chunk=False,
            )
            await updater.add_artifact(
                [Part(text="A JPEG artifact")],
                artifact_id="image-only",
                append=True,
                last_chunk=True,
            )
            await updater.complete()

        async def cancel(self, context: object, event_queue: EventQueue) -> None:
            assert isinstance(context, RequestContext)
            assert context.task_id is not None and context.context_id is not None
            await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    origin = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
    card = AgentCard(
        name="independent-image-server",
        supported_interfaces=[AgentInterface(url=f"{origin}/rpc", protocol_binding="JSONRPC", protocol_version="1.0")],
        version="1.0.0",
        description="Independent A2A SDK fixture server",
        skills=[AgentSkill(id="images", name="Images", description="Returns fixture images", tags=["test"])],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "image/png", "image/jpeg"],
    )
    handler = DefaultRequestHandler(ImageExecutor(), InMemoryTaskStore(), card)
    app = FastAPI()
    app.router.routes.extend(create_agent_card_routes(card))
    app.router.routes.extend(create_jsonrpc_routes(handler, rpc_url="/rpc"))

    async with serve_asgi(app, sock=server_socket) as served_origin:
        assert served_origin == origin
        async with RemoteAgent(served_origin, loopback_http=True, timeout=10) as remote:
            conversation = RemoteConversation(remote)
            updates = [item async for item in conversation.send("return images", message_id="sdk-image-test")]
            assert updates
            assert conversation.state == TaskState.TASK_STATE_COMPLETED
            assert [
                (image.artifact_id, image.artifact_part, image.data, image.media_type, image.filename, image.caption)
                for image in conversation.images
            ] == [
                ("mixed", 1, images["red.png"], "image/png", "red.png", "A mixed artifact"),
                ("image-only", 0, images["blue.jpg"], "image/jpeg", "blue.jpg", "A JPEG artifact"),
            ]
            snapshot = await remote.get_task(conversation.task_id)
            conversation.accept(StreamResponse(task=snapshot))
            conversation.accept(StreamResponse(task=snapshot))
            assert [(image.artifact_id, image.artifact_part, image.data) for image in conversation.images] == [
                ("mixed", 1, images["red.png"]),
                ("image-only", 0, images["blue.jpg"]),
            ]


@pytest.mark.asyncio
async def test_image_publisher_respects_modes_budget_and_execution_lifetime(images: dict[str, bytes]) -> None:
    queued: list[ImageArtifact] = []

    async def sink(image: ImageArtifact) -> None:
        queued.append(image)

    consumed = 0

    def consume(size: int) -> None:
        nonlocal consumed
        if consumed + size > len(images["blue.jpg"]) - 1:
            raise ValueError("public output budget exceeded")
        consumed += size

    publisher = ImagePublisher(sink, consume, lambda: ("image/jpeg",))
    token = current_image_publisher.set(publisher)
    try:
        with pytest.raises(ValueError, match="did not accept"):
            await publish_image(images["red.png"], media_type="image/png", filename="red.png")
        assert queued == []
        publisher.modes = lambda: ("image/png", "image/jpeg")
        with pytest.raises(ValueError, match="budget"):
            await publish_image(images["blue.jpg"], media_type="image/jpeg", filename="blue.jpg")
        assert queued == []
        assert publisher.count == publisher.bytes == consumed == 0
        identifier = await publish_image(images["red.png"], media_type="image/png", filename="red.png")
        assert len(queued) == 1
        assert queued[0].artifact_id == identifier
        publisher.active = False
        with pytest.raises(RuntimeError, match="no longer active"):
            await publish_image(images["red.png"], media_type="image/png", filename="red.png")
    finally:
        current_image_publisher.reset(token)


@pytest.mark.asyncio
async def test_image_publisher_rejects_over_budget_batch_before_sending_first_image(
    images: dict[str, bytes],
) -> None:
    queued: list[ImageArtifact] = []

    async def sink(image: ImageArtifact) -> None:
        queued.append(image)

    def consume(size: int) -> None:
        raise ValueError("shared output budget exceeded")

    publisher = ImagePublisher(sink, consume, lambda: ("image/png", "image/jpeg"))
    batch = (
        ImageArtifact("batch-one", 0, images["red.png"], "image/png", "one.png"),
        ImageArtifact("batch-two", 0, images["blue.jpg"], "image/jpeg", "two.jpg"),
    )
    token = current_image_publisher.set(publisher)
    try:
        with pytest.raises(ValueError, match="shared output budget"):
            await publisher.publish_batch(batch)
        assert queued == []
        assert publisher.count == publisher.bytes == 0
        assert publisher.identifiers == set()
    finally:
        current_image_publisher.reset(token)


@pytest.mark.asyncio
async def test_image_publisher_rejects_caption_without_text_mode_before_budget_or_queue(
    images: dict[str, bytes],
) -> None:
    queued: list[ImageArtifact] = []
    consumed = 0
    consume_calls = 0

    async def sink(image: ImageArtifact) -> None:
        queued.append(image)

    def consume(size: int) -> None:
        nonlocal consumed, consume_calls
        consumed += size
        consume_calls += 1

    publisher = ImagePublisher(sink, consume, lambda: ("image/png",))
    token = current_image_publisher.set(publisher)
    try:
        with pytest.raises(ValueError, match="did not accept text captions"):
            await publish_image(
                images["red.png"],
                media_type="image/png",
                filename="captioned.png",
                caption="An image-specific caption",
            )
        assert queued == []
        assert publisher.count == publisher.bytes == consumed == consume_calls == 0
        assert publisher.identifiers == set()
    finally:
        current_image_publisher.reset(token)


@pytest.mark.asyncio
async def test_image_publisher_is_unavailable_outside_active_a2a_execution(images: dict[str, bytes]) -> None:
    with pytest.raises(RuntimeError, match="active A2A server execution"):
        await publish_image(images["red.png"], media_type="image/png", filename="red.png")


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")
async def test_harness_a2a_channel_send_requires_approval_and_exports_only_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, images: dict[str, bytes], approved: bool
) -> None:
    from nagents.harness import runtime as harness_runtime

    workspace = tmp_path / "harness-workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "thumbnail.png").write_bytes(images["red.png"])
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    config_home.mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only-key")
    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    origin = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
    key = signing_key("RS256", "harness-image-export")
    async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
        config = A2AConfig(
            public_url=origin,
            agent="assistant",
            issuer=IssuerConfig(issuer=issuer, audience=AUDIENCE),
            data_dir=(tmp_path / "a2a-harness-state").resolve(),
            loopback_http=True,
        )
        provider_profiles = connection(name="fixture", auth="api-key")
        registry_store = ScopedProviderRegistryStore(tmp_path).global_store
        saved_registry = registry_store.load()
        registry_store.save(
            ProviderRegistry(active="fixture", providers=provider_profiles), expected=saved_registry.revision
        )
        harness_config = HarnessConfig(
            workspace=workspace,
            data_dir=tmp_path / "harness-state",
            provider="fixture",
            providers=provider_profiles,
            model="fixture-model",
        )

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                yield ToolCallEvent(id="list", name="channel_list", arguments={})
            elif len(provider.requests) == 2:
                yield ToolCallEvent(
                    id="export",
                    name="channel_send",
                    arguments={
                        "channel": "builtin.a2a",
                        "destination": "current",
                        "text": "",
                        "attachments": ["thumbnail.png"],
                    },
                )
            else:
                yield TextDoneEvent(text="Image export finished.")
                yield DoneEvent(final_text="Image export finished.")
            del messages

        providers: list[FakeProvider] = []

        def provider_factory(provider_config: HarnessConfig, login_store: object | None = None) -> FakeProvider:
            del login_store
            provider = FakeProvider(provider_config, len(providers), script)
            providers.append(provider)
            return provider

        monkeypatch.setattr(harness_runtime, "HarnessProvider", provider_factory)
        monkeypatch.setattr(
            harness_runtime, "build_provider", lambda profile, provider_config, auth: provider_factory(provider_config)
        )
        app = create_harness_app(config=config, harness_config=harness_config)
        async with serve_asgi(app, sock=server_socket):

            async def token() -> str:
                return make_token(
                    key,
                    access_claims(
                        issuer,
                        aud=AUDIENCE,
                        scope="ngn:access ngn:invoke ngn:read ngn:cancel ngn:approve",
                        jti=f"harness-image-{approved}",
                    ),
                )

            async with RemoteAgent(origin, token, loopback_http=True, timeout=10) as client:
                owner = await app.state.a2a.authenticator.authenticate(await token())
                message = new_text_message("Export the requested image through A2A")
                message.role = Role.ROLE_USER
                message.message_id = f"harness-image-client-{approved}"
                updates = []
                task_id = ""
                auth_updates = []
                reviewed_tools = []
                async for update in client.send(SendMessageRequest(message=message)):
                    updates.append(update)
                    if update.HasField("task"):
                        task_id = update.task.id
                    if (
                        update.WhichOneof("payload") == "status_update"
                        and update.status_update.status.state == TaskState.TASK_STATE_AUTH_REQUIRED
                    ):
                        auth_updates.append(update)
                        approval_id = update.status_update.status.message.parts[0].text.rsplit("/", 1)[-1]
                        record = await app.state.a2a.authorizations.inspect(approval_id, owner)
                        reviewed_tools.append(record.details["tool"])
                        if record.details["tool"] == "channel_send":
                            assert record.details["arguments"]["attachments"] == ["thumbnail.png"]
                        await app.state.a2a.authorizations.decide(
                            approval_id,
                            owner,
                            record.nonce,
                            approved or record.details["tool"] != "channel_send",
                            record.digest,
                        )
                assert task_id
                assert auth_updates, "channel_send must retain the ordinary task approval requirement"
                assert "channel_send" in reviewed_tools
                assert providers and any(
                    "channel_list" in schema and "channel_send" in schema
                    for provider in providers
                    for schema in provider.schemas
                )
                assert base64.b64encode(images["red.png"]).decode("ascii") not in repr(
                    [request for provider in providers for request in provider.requests]
                )
                saved = await client.get_task(task_id)
                raw_parts = [
                    part
                    for artifact in saved.artifacts
                    for part in artifact.parts
                    if part.WhichOneof("content") == "raw"
                ]
                if approved:
                    assert len(raw_parts) == 1
                    assert raw_parts[0].raw == images["red.png"]
                    assert raw_parts[0].filename == "thumbnail.png"
                    assert raw_parts[0].media_type == "image/png"
                else:
                    assert raw_parts == []
                    assert saved.status.state == TaskState.TASK_STATE_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_image", [True, False])
@pytest.mark.skipif(os.name != "posix", reason="Harness image notification tests require POSIX")
async def test_parent_notification_attaches_only_owned_child_images_with_explicit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, images: dict[str, bytes], supports_image: bool
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncGenerator[Event, None]:
        del provider, messages
        yield TextDoneEvent(text="Child image result")
        yield DoneEvent(final_text="Child image result")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    await harness.initialize()
    harness.tasks.begin(harness.session_id)

    class VisionProvider(FakeProvider):
        @property
        def supported_media_formats(self) -> MediaCapabilities:
            return MediaCapabilities(image_formats=frozenset({"png"}))

    class TextOnlyProvider(FakeProvider):
        @property
        def supported_media_formats(self) -> MediaCapabilities:
            return MediaCapabilities()

    try:
        if supports_image:
            harness.agent.provider = VisionProvider(harness.config, 99, script)
        else:
            harness.agent.provider = TextOnlyProvider(harness.config, 99, script)
        accepted = await harness.tasks.to_agent("Return the thumbnail")
        task_id = str(accepted["session_id"])
        await harness.tasks._workers[task_id]
        info = harness.tasks._lookup(task_id)
        image = ImageContent(
            base64.b64encode(images["red.png"]).decode("ascii"),
            "image/png",
            filename="child.png",
            artifact_id="child-image",
            artifact_part=0,
        )
        history_id = await harness.agent.session.add_message(
            info._storage_id, Message(role="assistant", content=[image])
        )
        references = image_references([image], harness.tasks.root._session_id, history_id)
        info.artifacts = references
        completed = next(event for event in harness.tasks._ready if isinstance(event, TaskCompleted))
        completed.artifacts = references

        original_parent = info.parent_session_id
        info.parent_session_id = "grandchild-parent"
        with pytest.raises(RuntimeError, match="immediate parent"):
            await harness.tasks.notification()
        assert len(harness.tasks._ready) == 1
        info.parent_session_id = original_parent

        notification = await harness.tasks.notification()
        assert notification is not None
        if supports_image:
            assert isinstance(notification, list)
            assert isinstance(notification[0], TextContent)
            assert isinstance(notification[1], ImageContent)
            assert notification[1].base64_data == image.base64_data
            envelope = json.loads(notification[0].text.split("\n", 1)[1])
            metadata = envelope["sessions"][0]["artifacts"][0]
            assert metadata["model_input"] == "attached"
            assert metadata["filename"] == "child.png"
        else:
            assert isinstance(notification, str)
            envelope = json.loads(notification.split("\n", 1)[1])
            metadata = envelope["sessions"][0]["artifacts"][0]
            assert metadata["model_input"] == "unsupported"
    finally:
        await harness.tasks.end()
        await harness.close()


def test_parent_update_json_preserves_attachment_status_and_two_stage_prefix() -> None:
    reference = ImageArtifactReference(
        artifact_id="child-image",
        artifact_part=0,
        filename="child.png",
        media_type="image/png",
        byte_length=512,
        path="/api/sessions/root/artifacts/4/0",
    )
    updates = (
        TaskCompleted(session_id="child-1", title="Image child", result="Image ready", artifacts=(reference,)),
        TaskMessage(session_id="child-2", title="Second child", prompt="Follow-up note"),
    )
    first = format_batch(updates, image_inputs={reference.path: "unsupported"}, maximum=1)
    second = format_batch(updates[first.count :])
    first_json = first.text.split("\n", 1)[1]
    envelope = json.loads(first_json)
    assert first.count == 1 and second.count == 1
    assert envelope["sessions"][0]["artifacts"][0]["model_input"] == "unsupported"
    assert first.text == first.text.rstrip()
    assert json.loads(second.text.split("\n", 1)[1])["human_messages"][0]["session_id"] == "child-2"
