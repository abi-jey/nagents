"""Separate authenticated A2A ASGI service for framework Agents or ngn."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import TYPE_CHECKING
from typing import cast
from urllib.parse import urlsplit

import aiohttp
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import AgentCapabilities
from a2a.types import AgentCard
from a2a.types import AgentExtension
from a2a.types import AgentInterface
from a2a.types import AgentSkill
from a2a.types import OpenIdConnectSecurityScheme
from a2a.types import SecurityRequirement
from a2a.types import SecurityScheme
from a2a.types import StringList
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..diagnostics import error_frames
from ..executor import NagentsAgentExecutor
from ..store import SQLiteEventStream
from ..store import SQLiteTaskStore
from ..store import private_file
from .auth import AuthenticationError
from .auth import Authenticator
from .auth import Principal
from .authorization import AuthorizationRegistry
from .authorization import AuthorizationUnavailable
from .handlers import AuthenticatedContextBuilder
from .handlers import OwnedRequestHandler
from .oauth import OAuthCoordinator

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import ASGIApp
    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from nagents.harness.config import HarnessConfig

    from ..agent import AgentFactory
    from ..executor import TaskExecutor
    from .config import A2AConfig

logger = logging.getLogger(__name__)
ExecutorBuilder = Callable[[SQLiteTaskStore, AuthorizationRegistry, OAuthCoordinator], "TaskExecutor"]
RPC_SCOPES = {
    "SendMessage": "ngn:invoke",
    "SendStreamingMessage": "ngn:invoke",
    "GetTask": "ngn:read",
    "ListTasks": "ngn:read",
    "SubscribeToTask": "ngn:read",
    "CancelTask": "ngn:cancel",
    "GetExtendedAgentCard": "ngn:read",
    "CreateTaskPushNotificationConfig": "ngn:invoke",
    "GetTaskPushNotificationConfig": "ngn:read",
    "ListTaskPushNotificationConfigs": "ngn:read",
    "DeleteTaskPushNotificationConfig": "ngn:invoke",
}


def agent_card(config: A2AConfig) -> AgentCard:
    """Public minimal card: only the explicitly exported profile is disclosed."""
    from ..client.configuration import PUBLIC_CLIENT_EXTENSION

    extensions: list[AgentExtension] = []
    if config.browser_client_id and not config.browser_client_secret_env:
        public_client = AgentExtension(
            uri=PUBLIC_CLIENT_EXTENSION,
            required=False,
            description="Suggested public PKCE client. Register the caller's callback URI with its identity provider.",
        )
        public_client.params.update({"client_id": config.browser_client_id, "scopes": list(config.browser_scopes)})
        extensions.append(public_client)
    return AgentCard(
        name=config.name,
        description=config.description,
        version=version("nagents"),
        supported_interfaces=[
            AgentInterface(
                url=f"{config.public_url.rstrip('/')}/rpc", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True, push_notifications=False, extended_agent_card=False, extensions=extensions
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "image/png", "image/jpeg"],
        skills=[AgentSkill(id=config.agent, name=config.agent, description=config.description, tags=["nagents"])],
        security_schemes={
            "oidc": SecurityScheme(
                open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                    open_id_connect_url=f"{config.issuer.issuer.rstrip('/')}/.well-known/openid-configuration"
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"oidc": StringList(list=["ngn:access"])})],
    )


class RateLimit:
    """Bounded fixed-window admission, including unauthenticated requests."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.entries: dict[str, deque[float]] = {}

    def admit(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.entries) >= 1024:
            self.entries = {key: values for key, values in self.entries.items() if values and values[-1] > now - 60}
        if key not in self.entries:
            if len(self.entries) >= 1024:
                return False
            self.entries[key] = deque()
        values = self.entries[key]
        while values and values[0] <= now - 60:
            values.popleft()
        if len(values) >= self.maximum:
            return False
        values.append(now)
        return True


class A2AService:
    def __init__(self, config: A2AConfig, builder: ExecutorBuilder) -> None:
        self.config = config
        self.builder = builder
        self.store = SQLiteTaskStore(config)
        self.authorizations = AuthorizationRegistry(config, self.store)
        self.card = agent_card(config)
        self.address_limit = RateLimit(config.requests_per_minute * 2)
        self.owner_limit = RateLimit(config.requests_per_minute)
        self.http: aiohttp.ClientSession
        self.authenticator: Authenticator
        self.oauth: OAuthCoordinator
        self.executor: TaskExecutor
        self.handler: OwnedRequestHandler
        self._lock_descriptor = -1

    async def start(self) -> None:
        # One process owns the execution ledger. More workers would have live
        # approval Futures in different processes and unsafe restart recovery.
        if os.name != "posix":
            raise ValueError("Private A2A serving currently requires POSIX ownership controls")
        import fcntl

        lock_path = self.config.data_dir.resolve() / "server.lock"
        private_file(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise ValueError("This A2A data directory is already owned by another server process") from None
        self._lock_descriptor = descriptor
        try:
            await self.store.initialize()
            await self.store.reconcile_restart()
            await self.authorizations.initialize()
            self.http = aiohttp.ClientSession()
            try:
                self.authenticator = Authenticator(self.config, self.http)
                await self.authenticator.initialize()
                self.oauth = OAuthCoordinator(self.config, self.authenticator, self.authorizations)
                await self.oauth.initialize()
                self.executor = self.builder(self.store, self.authorizations, self.oauth)
                self.handler = OwnedRequestHandler(self.executor, self.store, SQLiteEventStream(self.store), self.card)
            except BaseException:
                await self.http.close()
                raise
        except BaseException:
            os.close(self._lock_descriptor)
            self._lock_descriptor = -1
            raise

    async def close(self) -> None:
        try:
            try:
                await self.executor.close()
            finally:
                try:
                    await self.handler.aclose()
                finally:
                    await self.authorizations.close()
                    self.oauth.close()
                    await self.http.close()
        finally:
            if self._lock_descriptor >= 0:
                os.close(self._lock_descriptor)
                self._lock_descriptor = -1

    async def rpc(self, request: Request) -> Response:
        # SDK owns protocol parsing, version negotiation, errors and SSE framing.
        route = create_jsonrpc_routes(self.handler, "/rpc", AuthenticatedContextBuilder(), enable_v0_3_compat=False)[0]
        return cast("Response", await route.endpoint(request))


class SecurityBoundary:
    def __init__(self, app: ASGIApp, *, service: A2AService) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        config = self.service.config
        request_id = uuid.uuid4().hex
        scope["ngn.request_id"] = request_id
        started = time.monotonic()
        status = 500
        response_started = False
        operation = "http"

        async def traced_send(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = int(message["status"])
                response_started = True
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                )
                if request.url.path != "/.well-known/agent-card.json":
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            authority = urlsplit(config.public_url).netloc
            if request.headers.get("host", "").lower() != authority.lower():
                await JSONResponse({"error": "invalid_host"}, status_code=400)(scope, receive, traced_send)
                return
            public = urlsplit(config.public_url)
            if scope.get("scheme") != "https" and public.scheme != "http":
                raise AuthenticationError("https_required", 400)
            if len(scope.get("query_string", b"")) > 16384 or len(scope.get("path", "")) > 2048:
                raise AuthenticationError("request_too_large", 414)
            origin = request.headers.get("origin")
            if origin and origin != config.public_url.rstrip("/"):
                raise AuthenticationError("invalid_origin", 403)
            client = scope.get("client")
            address = str(client[0]) if client else "unknown"
            if public.scheme == "http":
                try:
                    local = ipaddress.ip_address(address).is_loopback
                except ValueError:
                    local = False
                if not local:
                    raise AuthenticationError("loopback_required", 403)
            if not self.service.address_limit.admit(address):
                await JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})(
                    scope, receive, traced_send
                )
                return
            if request.url.path == "/rpc":
                headers = request.headers.getlist("authorization")
                if len(headers) != 1:
                    raise AuthenticationError()
                scheme, _, token = headers[0].partition(" ")
                if scheme.lower() != "bearer":
                    raise AuthenticationError()
                user = await self.service.authenticator.authenticate(token)
                user.require("ngn:access")
                scope["ngn.a2a.principal"] = user
                if not self.service.owner_limit.admit(user.user_name):
                    await JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})(
                        scope, receive, traced_send
                    )
                    return
            if request.method in {"POST", "PUT", "PATCH"}:
                content_length = request.headers.get("content-length", "")
                if content_length and (not content_length.isdigit() or int(content_length) > config.max_request_bytes):
                    await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, traced_send)
                    return
                raw = bytearray()
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > config.max_request_bytes:
                            await JSONResponse({"error": "request_too_large"}, status_code=413)(
                                scope, receive, traced_send
                            )
                            return
                if request.url.path == "/rpc":
                    try:
                        payload: object = json.loads(raw)
                    except (ValueError, UnicodeError):
                        payload = None  # SDK returns the standard parse error.
                    method = payload.get("method") if isinstance(payload, dict) else None
                    if isinstance(method, str) and method in RPC_SCOPES:
                        operation = method
                        user = scope["ngn.a2a.principal"]
                        if not isinstance(user, Principal):
                            raise AuthenticationError()
                        user.require(RPC_SCOPES[method])
                original_receive = receive
                delivered = False

                async def replay() -> Message:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(raw), "more_body": False}
                    return await original_receive()

                receive = replay
            await self.app(scope, receive, traced_send)
        except AuthenticationError as exc:
            challenge_headers = (
                {"WWW-Authenticate": f'Bearer realm="ngn-a2a", error="{exc.code}"'} if exc.status in {401, 403} else {}
            )
            await JSONResponse(
                {"error": exc.code, "request_id": request_id}, status_code=exc.status, headers=challenge_headers
            )(scope, receive, traced_send)
        except AuthorizationUnavailable:
            await JSONResponse({"error": "authorization_unavailable", "request_id": request_id}, status_code=404)(
                scope, receive, traced_send
            )
        except Exception as exc:
            logger.warning(
                "a2a.http.failed request=%s error_type=%s frames=%s", request_id, type(exc).__name__, error_frames(exc)
            )
            if response_started:
                raise RuntimeError(f"A2A response interrupted; request={request_id}") from None
            await JSONResponse({"error": "request_failed", "request_id": request_id}, status_code=500)(
                scope, receive, traced_send
            )
        finally:
            logger.info(
                "a2a.http request=%s operation=%s status=%d duration_ms=%d",
                request_id,
                operation,
                status,
                (time.monotonic() - started) * 1000,
            )


def application(config: A2AConfig, builder: ExecutorBuilder) -> Starlette:
    from starlette.middleware import Middleware

    from .views import AuthorizationViews

    service = A2AService(config, builder)
    views = AuthorizationViews(service)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await service.start()
        app.state.a2a = service
        try:
            yield
        finally:
            views.close()
            await service.close()

    routes = [
        *create_agent_card_routes(service.card),
        Route("/rpc", service.rpc, methods=["POST"]),
        *views.routes(),
    ]
    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(SecurityBoundary, service=service)])


def create_app(
    *, config: A2AConfig, agent_factory: AgentFactory, read_only_tools: frozenset[str] = frozenset()
) -> Starlette:
    """Serve a native nagents Agent factory without ngn or a browser UI.

    Only this host factory selects providers, instructions, tools and storage.
    All tools require task-bound human approval unless explicitly declared
    read-only by the host. Existing custom ToolExecutor checks remain in force.
    """

    def build(store: SQLiteTaskStore, registry: AuthorizationRegistry, oauth: OAuthCoordinator) -> TaskExecutor:
        return NagentsAgentExecutor(
            config,
            agent_factory,
            store,
            registry,
            read_only_tools=read_only_tools,
            credential_handler=oauth.credentials,
        )

    return application(config, build)


def create_harness_app(*, config: A2AConfig, harness_config: HarnessConfig) -> Starlette:
    """Serve one explicit ngn profile using the same framework A2A boundary."""
    from ..harness import HarnessAgentExecutor

    def build(store: SQLiteTaskStore, registry: AuthorizationRegistry, oauth: OAuthCoordinator) -> TaskExecutor:
        return HarnessAgentExecutor(config, harness_config, store, registry, credential_handler=oauth.credentials)

    return application(config, build)
