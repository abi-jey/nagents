"""Authenticated SDK client checks for the optional framework A2A client."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import socket
import ssl
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from typing import cast

import httpx
import pytest
import uvicorn
from a2a.helpers import new_text_message
from a2a.types import ListTasksRequest
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import TaskState
from aiohttp import web
from aiohttp.test_utils import TestServer
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette

from nagents.a2a import RemoteAgent
from nagents.a2a import RemoteAgentError
from nagents.a2a.server.application import agent_card
from nagents.a2a.server.application import create_app
from nagents.a2a.server.config import A2AConfig
from nagents.a2a.server.config import IssuerConfig
from nagents.agent import Agent
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from tests.a2a.test_a2a_auth import IssuerState
from tests.a2a.test_a2a_auth import access_claims
from tests.a2a.test_a2a_auth import local_issuer
from tests.a2a.test_a2a_auth import make_token
from tests.a2a.test_a2a_auth import signing_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import ASGIApp
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from nagents.a2a.agent.runtime import AgentContext
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


class AuthorizationCapture:
    """Capture bearer values in memory for exact origin-scoping assertions."""

    def __init__(self, app: ASGIApp, values: list[tuple[str, str]]) -> None:
        self.app = app
        self.values = values

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization")
            if authorization is not None:
                self.values.append((str(scope["path"]), authorization.decode("ascii")))
        await self.app(scope, receive, send)


@asynccontextmanager
async def serve_asgi(app: ASGIApp, sock: socket.socket | None = None) -> AsyncIterator[str]:
    if sock is None:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level=None, access_log=True, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("Local A2A test server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


def _uvicorn_logger_state() -> dict[str, tuple[int, tuple[int, ...], bool, bool, tuple[int, ...]]]:
    states: dict[str, tuple[int, tuple[int, ...], bool, bool, tuple[int, ...]]] = {}
    for name in ("", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        states[name] = (
            logger.level,
            tuple(id(handler) for handler in logger.handlers),
            logger.propagate,
            logger.disabled,
            tuple(id(filter_) for filter_ in logger.filters),
        )
    return states


@pytest.mark.asyncio
async def test_serve_asgi_preserves_global_logger_configuration() -> None:
    before = _uvicorn_logger_state()
    async with serve_asgi(Starlette()) as origin:
        assert origin.startswith("http://127.0.0.1:")
        assert _uvicorn_logger_state() == before
    assert _uvicorn_logger_state() == before


class RemoteProvider(Provider):
    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "fixture-only-key", "fixture-model")
        self.gate = gate

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
        prompt = messages[-1].content if messages else ""
        if prompt == "hold remote execution":
            yield TextChunkEvent(chunk="Remote work started.")
            await self.gate.wait()
        yield TextDoneEvent(text="Remote deterministic response.")
        yield DoneEvent(final_text="Remote deterministic response.")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")
async def test_remote_agent_uses_fresh_bearers_for_configured_origin_and_sdk_lifecycle(tmp_path: Path) -> None:
    key = signing_key("RS256", "remote-client-key")
    async with local_issuer(IssuerState([key.public_jwk])) as (issuer, _):
        gate = asyncio.Event()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config = A2AConfig(
            public_url=origin,
            agent="assistant",
            issuer=IssuerConfig(issuer=issuer, audience="nagents-a2a", algorithms=("RS256",)),
            data_dir=(tmp_path / "remote-service-state").resolve(),
            loopback_http=True,
        )
        seen_bearers: list[tuple[str, str]] = []
        agents: list[Agent] = []

        def factory(context: AgentContext) -> Agent:
            agent = Agent(
                RemoteProvider(gate),
                SessionManager(context.data_dir / "sessions.db"),
                system_prompt="Remote SDK integration fixture.",
                streaming=True,
                compaction_strategy=None,
                save_tool_outputs=False,
            )
            agents.append(agent)
            return agent

        app = create_app(config=config, agent_factory=factory)
        async with serve_asgi(AuthorizationCapture(app, seen_bearers), sock=sock) as served_origin:
            assert served_origin == origin
            # The public card advertises an attacker-controlled JSON-RPC
            # interface first. RemoteAgent must select only the operator's
            # exact configured origin and /rpc URL.
            app.state.a2a.card.supported_interfaces.insert(
                0,
                type(app.state.a2a.card.supported_interfaces[0])(
                    url=f"{origin}/attacker-rpc", protocol_binding="JSONRPC", protocol_version="1.0"
                ),
            )

            issued: list[str] = []

            async def tokens() -> str:
                claims = access_claims(
                    issuer,
                    scope="ngn:access ngn:invoke ngn:read ngn:cancel",
                    jti=f"remote-client-{len(issued)}",
                )
                token = make_token(key, claims)
                issued.append(token)
                return token

            async with RemoteAgent(origin, tokens, loopback_http=True, timeout=10) as remote:
                ordinary = new_text_message("complete remote request")
                ordinary.role = Role.ROLE_USER
                ordinary.message_id = "remote-client-complete-0001"
                completed_events = [event async for event in remote.send(SendMessageRequest(message=ordinary))]
                completed = next(event.task for event in reversed(completed_events) if event.HasField("task"))
                saved = await remote.get_task(completed.id)
                listed = await remote.list_tasks(ListTasksRequest())
                assert saved.id == completed.id
                assert saved.status.state == TaskState.TASK_STATE_COMPLETED
                assert any(task.id == completed.id for task in listed.tasks)
                assert len(agents) == 1

                held = new_text_message("hold remote execution")
                held.role = Role.ROLE_USER
                held.message_id = "remote-client-held-0001"
                send_stream = remote.send(SendMessageRequest(message=held))
                first = await asyncio.wait_for(anext(send_stream), timeout=5)
                task_id = first.task.id
                await send_stream.aclose()
                after_close = await remote.get_task(task_id)
                assert after_close.status.state not in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_CANCELED,
                }

                subscription = remote.subscribe(task_id)
                first_subscribed = await asyncio.wait_for(anext(subscription), timeout=5)
                assert first_subscribed.HasField("task") and first_subscribed.task.id == task_id
                cancelled = await remote.cancel(task_id)
                fetched_cancelled = await remote.get_task(task_id)
                assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
                assert fetched_cancelled.status.state == cancelled.status.state
                await subscription.aclose()
                assert len(agents) == 2

            assert len(issued) == len(seen_bearers)
            assert len(set(issued)) == len(issued)
            assert all(
                hmac.compare_digest(f"Bearer {token}", seen)
                for token, (_, seen) in zip(issued, seen_bearers, strict=True)
            )
            assert all(path != "/attacker-rpc" for path, _ in seen_bearers)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hostile-card", "redirect"])
@pytest.mark.skipif(os.name != "posix", reason="A2A private SQLite test storage requires POSIX")
async def test_remote_agent_never_follows_redirect_or_contacts_card_advertised_origin(
    tmp_path: Path, mode: str
) -> None:
    attacker_authorizations: list[str] = []
    attacker_app = web.Application()

    async def attacker(request: web.Request) -> web.Response:
        attacker_authorizations.append(request.headers.get("Authorization", ""))
        return web.json_response({"name": "attacker"})

    attacker_app.router.add_get("/{tail:.*}", attacker)
    async with TestServer(attacker_app, host="127.0.0.1") as attacker_server:
        attacker_url = str(attacker_server.make_url("/")).rstrip("/")
        settings = A2AConfig(
            public_url="http://127.0.0.1:42424",
            agent="assistant",
            issuer=IssuerConfig(issuer="https://issuer.example", audience="nagents-a2a"),
            data_dir=(tmp_path / f"{mode}-state").resolve(),
            loopback_http=True,
        )
        card = MessageToDict(agent_card(settings))
        interfaces = card["supportedInterfaces"]
        assert isinstance(interfaces, list)
        interfaces[0]["url"] = f"{attacker_url}/rpc"
        origin_authorizations: list[str] = []
        origin_app = web.Application()

        async def discovery(request: web.Request) -> web.StreamResponse:
            origin_authorizations.append(request.headers.get("Authorization", ""))
            if mode == "redirect":
                return web.Response(status=302, headers={"Location": f"{attacker_url}/.well-known/agent-card.json"})
            return web.json_response(card)

        origin_app.router.add_get("/.well-known/agent-card.json", discovery)
        async with TestServer(origin_app, host="127.0.0.1") as origin_server:
            origin = str(origin_server.make_url("/")).rstrip("/")
            token_value = "opaque-secret-that-must-not-leak"

            async def tokens() -> str:
                return token_value

            with pytest.raises(RemoteAgentError) as error:
                async with RemoteAgent(origin, tokens, loopback_http=True, timeout=5):
                    pass

            assert "opaque-secret-that-must-not-leak" not in str(error.value)
            assert len(origin_authorizations) == 1
            assert hmac.compare_digest(origin_authorizations[0], f"Bearer {token_value}")
            assert attacker_authorizations == []


def mock_card(origin: str, tmp_path: Path) -> dict[str, object]:
    config = A2AConfig(
        public_url=origin,
        agent="assistant",
        issuer=IssuerConfig(issuer="https://issuer.example", audience="remote-a2a"),
        data_dir=tmp_path / "unused-remote-state",
    )
    return cast("dict[str, object]", MessageToDict(agent_card(config)))


@pytest.mark.asyncio
async def test_remote_agent_accepts_explicit_tls_context_and_mock_transport(tmp_path: Path) -> None:
    origin = "https://remote.example"
    card = mock_card(origin, tmp_path)
    requests: list[tuple[str, str, str]] = []
    issued: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        requests.append((request.method, request.url.path, authorization))
        if request.method == "GET" and request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=card, request=request)
        raise AssertionError("opening the remote client must only resolve the card")

    async def tokens() -> str:
        token = f"remote-token-{len(issued)}"
        issued.append(token)
        return token

    transport = httpx.MockTransport(handle)
    async with RemoteAgent(
        origin,
        tokens,
        verify=ssl.create_default_context(),
        transport=transport,
    ) as remote:
        assert remote.card.name == "ngn"

    assert len(requests) == 1
    assert requests[0][:2] == ("GET", "/.well-known/agent-card.json")
    assert requests[0][2] == "Bearer remote-token-0"
    assert issued == ["remote-token-0"]


def test_remote_agent_rejects_disabled_tls_verification() -> None:
    async def tokens() -> str:
        return "unused-token"

    with pytest.raises(ValueError, match="verification"):
        RemoteAgent("https://remote.example", tokens, verify=False)


@pytest.mark.asyncio
async def test_mock_transport_send_failure_is_not_replayed_and_refreshes_bearer(tmp_path: Path) -> None:
    origin = "https://remote.example"
    card = mock_card(origin, tmp_path)
    requests: list[tuple[str, str, str]] = []
    issued: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("authorization", "")
        requests.append((request.method, request.url.path, authorization))
        if request.method == "GET" and request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=card, request=request)
        if request.method == "POST" and request.url.path == "/rpc":
            return httpx.Response(503, json={"error": "fixture unavailable"}, request=request)
        return httpx.Response(404, request=request)

    async def tokens() -> str:
        token = f"remote-token-{len(issued)}"
        issued.append(token)
        return token

    async with RemoteAgent(
        origin,
        tokens,
        verify=True,
        transport=httpx.MockTransport(handle),
    ) as remote:
        message = new_text_message("one stable request")
        message.role = Role.ROLE_USER
        message.message_id = "remote-mock-no-replay-0001"
        with pytest.raises(RemoteAgentError):
            _ = [event async for event in remote.send(SendMessageRequest(message=message))]

    card_requests = [item for item in requests if item[0] == "GET"]
    post_requests = [item for item in requests if item[0] == "POST"]
    assert len(card_requests) == 1
    assert len(post_requests) == 1
    assert len(issued) == 2
    assert card_requests[0][2] == "Bearer remote-token-0"
    assert post_requests[0][2] == "Bearer remote-token-1"
