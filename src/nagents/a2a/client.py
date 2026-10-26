"""Host-configured framework client for a remote machine's A2A agent."""

from __future__ import annotations

import re
import ssl
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast
from urllib.parse import urlsplit

import httpx
from a2a.client import A2ACardResolver
from a2a.client import ClientConfig
from a2a.client import ClientFactory
from a2a.client.errors import A2AClientTimeoutError
from a2a.client.errors import AgentCardResolutionError
from a2a.types import CancelTaskRequest
from a2a.types import GetTaskRequest
from a2a.types import ListTasksRequest
from a2a.types import SubscribeToTaskRequest

from .errors import RemoteAgentError as RemoteAgentError
from .server.config import endpoint

if TYPE_CHECKING:
    from types import TracebackType

    from a2a.client import Client
    from a2a.types import AgentCard
    from a2a.types import ListTasksResponse
    from a2a.types import SendMessageRequest
    from a2a.types import StreamResponse
    from a2a.types import Task

    from .remote_config import RemoteAgentConfig

AccessTokenProvider = Callable[[], Awaitable[str]]


def remote_failure_code(error: BaseException) -> str:
    """Inspect typed causes/statuses; never classify or expose exception text."""
    causes: list[BaseException] = []
    seen: set[int] = set()
    current = error
    while id(current) not in seen and len(causes) < 16:
        seen.add(id(current))
        causes.append(current)
        if isinstance(current, RemoteAgentError):
            return current.code
        status = (
            current.response.status_code
            if isinstance(current, httpx.HTTPStatusError)
            else current.status_code
            if isinstance(current, AgentCardResolutionError)
            else 0
        )
        if status == 401:
            return "A2A_AUTH_REJECTED"
        if status == 403:
            return "A2A_FORBIDDEN"
        if status == 429:
            return "A2A_RATE_LIMIT"
        if isinstance(status, int) and status >= 500:
            return "A2A_SERVER_ERROR"
        cause = current.__cause__ or current.__context__
        if cause is None:
            break
        current = cause
    if any(isinstance(cause, ssl.SSLError) for cause in causes):
        return "A2A_TLS"
    if any(isinstance(cause, httpx.TimeoutException | A2AClientTimeoutError) for cause in causes):
        return "A2A_TIMEOUT"
    if any(isinstance(cause, httpx.ProtocolError | httpx.DecodingError) for cause in causes):
        return "A2A_PROTOCOL"
    if any(isinstance(cause, httpx.RequestError | ConnectionError) for cause in causes):
        return "A2A_CONNECTION"
    return "A2A_PROTOCOL"


class ResourceBearer(httpx.Auth):
    def __init__(self, tokens: AccessTokenProvider, origin: str) -> None:
        self.tokens = tokens
        parsed = httpx.URL(origin)
        self.origin = parsed.scheme, parsed.host, parsed.port

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        origin = request.url.scheme, request.url.host, request.url.port
        if origin != self.origin:
            raise RemoteAgentError("Remote credentials cannot be sent to another origin")
        try:
            token = await self.tokens()
        except Exception as error:
            raise RemoteAgentError(
                "Remote access credentials are unavailable",
                code=error.code if isinstance(error, RemoteAgentError) else "A2A_AUTH_REQUIRED",
            ) from None
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,16384}", token):
            raise RemoteAgentError("Remote access credentials are unavailable", code="A2A_AUTH_REQUIRED")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class RemoteAgent:
    """Call a fixed remote A2A endpoint from a native Agent tool or local ngn.

    The host selects the server, intended OAuth resource and token provider.
    Model arguments never select an endpoint or credentials. Discovery cannot
    redirect credentials to a URL advertised by an untrusted Agent Card.
    Message IDs/payloads are supplied by the caller and never silently retried.
    Closing a local stream does not cancel remote work; cancel explicitly.
    """

    def __init__(
        self,
        url: str,
        tokens: AccessTokenProvider | None = None,
        *,
        rpc_url: str = "",
        loopback_http: bool = False,
        timeout: float = 120.0,
        verify: ssl.SSLContext | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = endpoint(url, loopback_http=loopback_http).rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.path:
            raise ValueError("Remote A2A url must be the configured service origin")
        self.rpc_url = endpoint(rpc_url or f"{self.url}/rpc", loopback_http=loopback_http)
        remote = urlsplit(self.rpc_url)
        if (remote.scheme, remote.netloc) != (parsed.scheme, parsed.netloc):
            raise ValueError("Remote A2A RPC endpoint must use the configured origin")
        if not 1 <= timeout <= 3600:
            raise ValueError("Remote request timeout must be from 1 to 3600 seconds")
        if verify is not True and not isinstance(verify, ssl.SSLContext):
            raise ValueError("Remote A2A TLS verification cannot be disabled")
        self.tokens = tokens
        self.timeout = timeout
        self.verify = verify
        self.transport = transport
        self._connected = False
        self.http: httpx.AsyncClient
        self.client: Client
        self.card: AgentCard

    async def __aenter__(self) -> RemoteAgent:
        if self._connected:
            raise RemoteAgentError("Remote agent is already connected")
        self.http = httpx.AsyncClient(
            auth=ResourceBearer(self.tokens, self.url) if self.tokens is not None else None,
            verify=self.verify,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(self.timeout),
            headers={"A2A-Version": "1.0"},
        )
        try:
            self.card = await A2ACardResolver(self.http, self.url).get_agent_card()
            matches = [
                interface
                for interface in self.card.supported_interfaces
                if interface.protocol_binding == "JSONRPC"
                and re.fullmatch(r"1\.0(?:\.\d+)?", interface.protocol_version)
                and httpx.URL(interface.url) == httpx.URL(self.rpc_url)
                and not interface.tenant
            ]
            if len(matches) != 1:
                raise RemoteAgentError("Remote discovery does not advertise the configured A2A 1.0 JSON-RPC endpoint")
            # Give the SDK only the explicitly trusted interface. Even another
            # valid advertised binding cannot alter the host's destination.
            card = type(self.card)()
            card.CopyFrom(self.card)
            del card.supported_interfaces[:]
            card.supported_interfaces.add().CopyFrom(matches[0])
            self.client = ClientFactory(
                ClientConfig(
                    httpx_client=self.http,
                    supported_protocol_bindings=["JSONRPC"],
                    streaming=True,
                    accepted_output_modes=["text/plain", "image/png", "image/jpeg"],
                )
            ).create(card)
            self._connected = True
            return self
        except BaseException as exc:
            await self.http.aclose()
            if isinstance(exc, Exception):
                raise RemoteAgentError(
                    "Remote discovery failed; check the configured server and access token",
                    code=remote_failure_code(exc),
                ) from None
            raise

    def _ready(self) -> Client:
        if not self._connected:
            raise RemoteAgentError("Use RemoteAgent as an async context manager before calling it")
        return self.client

    @classmethod
    def from_config(
        cls,
        config: RemoteAgentConfig,
        tokens: AccessTokenProvider | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> RemoteAgent:
        """Build from parsed JSON; the embedding supplies its credential owner."""
        if not config.configured:
            raise ValueError("A remote agent connection requires a URL")
        if (config.issuer or config.token_env) and tokens is None:
            raise ValueError("The configured remote authentication requires a token provider")
        context = ssl.create_default_context()
        if config.ca_file:
            context.load_verify_locations(cafile=config.ca_file)
        return cls(
            config.url,
            tokens,
            rpc_url=config.rpc_url,
            loopback_http=config.loopback_http,
            timeout=config.timeout,
            verify=context,
            transport=transport,
        )

    async def send(self, request: SendMessageRequest) -> AsyncGenerator[StreamResponse, None]:
        """Send one caller-owned stable message without automatic replay."""
        try:
            events = cast("AsyncGenerator[StreamResponse, None]", self._ready().send_message(request))
            async with aclosing(events):
                async for event in events:
                    yield event
        except Exception as error:
            raise RemoteAgentError(
                "Remote send failed; retain this message ID and inspect the task before retrying",
                code=remote_failure_code(error),
            ) from None

    async def subscribe(self, task_id: str) -> AsyncGenerator[StreamResponse, None]:
        try:
            events = cast(
                "AsyncGenerator[StreamResponse, None]", self._ready().subscribe(SubscribeToTaskRequest(id=task_id))
            )
            async with aclosing(events):
                async for event in events:
                    yield event
        except Exception as error:
            raise RemoteAgentError(
                "Remote subscription ended; inspect the task before reconnecting", code=remote_failure_code(error)
            ) from None

    async def get_task(self, task_id: str) -> Task:
        try:
            return await self._ready().get_task(GetTaskRequest(id=task_id))
        except Exception as error:
            raise RemoteAgentError("Remote task read failed", code=remote_failure_code(error)) from None

    async def list_tasks(self, request: ListTasksRequest) -> ListTasksResponse:
        try:
            return await self._ready().list_tasks(request)
        except Exception as error:
            raise RemoteAgentError("Remote task listing failed", code=remote_failure_code(error)) from None

    async def cancel(self, task_id: str) -> Task:
        try:
            return await self._ready().cancel_task(CancelTaskRequest(id=task_id))
        except Exception as error:
            raise RemoteAgentError(
                "Remote task cancellation failed; inspect its state", code=remote_failure_code(error)
            ) from None

    async def close(self) -> None:
        if self._connected:
            self._connected = False
            try:
                await self.client.close()
            finally:
                await self.http.aclose()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.close()
