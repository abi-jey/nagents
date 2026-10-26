"""Out-of-band PKCE flows and private, task-scoped downstream credentials."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import time
from base64 import b64encode
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from urllib.parse import quote_plus
from urllib.parse import urlencode

import aiohttp
import httpx

from nagents.harness.private_store import ProtectedFileStore
from nagents.harness.private_store import ProtectedStoreError

from .auth import AuthenticationError
from .auth import Principal
from .authorization import AuthorizationUnavailable
from .config import endpoint
from .config import mapping

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from .auth import Authenticator
    from .authorization import AuthorizationRecord
    from .authorization import AuthorizationRegistry
    from .authorization import AuthorizationSession
    from .config import A2AConfig
    from .config import ServiceConfig


class AccessGrant:
    """Opaque host-tool credential; not a model result or a serializable record.

    ``auth()`` binds HTTPX authentication to the configured API origin. Host
    tools return public service results, never this object or its credentials.
    """

    __slots__ = ("_api_origin", "_token", "expires_at", "service")

    def __init__(self, service: str, token: str, expires_at: float, api_origin: str) -> None:
        self.service = service
        self._token = token
        self.expires_at = expires_at
        self._api_origin = api_origin

    def __repr__(self) -> str:
        return "AccessGrant(<redacted>)"

    def auth(self) -> httpx.Auth:
        return _GrantAuth(self)


class _GrantAuth(httpx.Auth):
    def __init__(self, grant: AccessGrant) -> None:
        self.grant = grant
        target = httpx.URL(grant._api_origin)
        self.origin = target.scheme, target.host, target.port

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        origin = request.url.scheme, request.url.host, request.url.port
        if origin != self.origin or self.grant.expires_at <= time.time():
            raise AuthorizationUnavailable()
        request.headers["Authorization"] = f"Bearer {self.grant._token}"
        yield request


class CredentialStore(ProtectedFileStore):
    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            filename=path.name,
            subject="A2A service",
            error=ProtectedStoreError,
            malformed="A2A service credentials are unavailable.",
            max_bytes=65536,
        )

    def save(self, token: str, expires_at: float, fingerprint: str) -> None:
        self._write_bytes(
            json.dumps({"access_token": token, "expires_at": expires_at, "binding": fingerprint}).encode(),
            prefix=".a2a-credential-",
        )

    def load(self, service: str, fingerprint: str, api_origin: str) -> AccessGrant | None:
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
            if raw is None:
                return None
            data = mapping(json.loads(raw), "saved credentials")
            expires = data.get("expires_at")
            token = data.get("access_token")
            if (
                data.get("binding") != fingerprint
                or isinstance(expires, bool)
                or not isinstance(expires, (int, float))
                or not math.isfinite(expires)
                or expires <= time.time()
                or not isinstance(token, str)
                or not token
            ):
                return None
            return AccessGrant(service, token, float(expires), api_origin)
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError, OSError):
            raise ProtectedStoreError("A2A service credentials are unavailable.") from None


@dataclass(frozen=True)
class OAuthClient:
    authorization_endpoint: str
    token_endpoint: str
    id: str
    secret_env: str = ""
    scopes: tuple[str, ...] = ()
    resource: str = ""


@dataclass(frozen=True)
class Flow:
    verifier: str = field(repr=False)
    browser_hash: str
    authorization_id: str
    service: str
    user: Principal | None
    expires_at: float
    client: OAuthClient


@dataclass(frozen=True)
class FlowResult:
    authorization_id: str
    user: Principal
    access_token: str = field(default="", repr=False)


class OAuthCoordinator:
    def __init__(self, config: A2AConfig, authenticator: Authenticator, registry: AuthorizationRegistry) -> None:
        self.config = config
        self.authenticator = authenticator
        self.registry = registry
        self._services: dict[str, OAuthClient] = {}
        self._flows: dict[str, Flow] = {}

    async def initialize(self) -> None:
        for name, service in self.config.services.items():
            metadata = await self.authenticator.document(
                f"{service.issuer.rstrip('/')}/.well-known/openid-configuration"
            )
            if metadata.get("issuer") != service.issuer:
                raise ValueError("Downstream discovery issuer does not match its configured issuer")
            values: list[str] = []
            for key in ("authorization_endpoint", "token_endpoint"):
                value = metadata.get(key)
                if not isinstance(value, str):
                    raise ValueError("Downstream discovery is missing OAuth endpoints")
                values.append(endpoint(value, loopback_http=self.config.loopback_http))
            methods = metadata.get("code_challenge_methods_supported", [])
            if not isinstance(methods, list) or "S256" not in methods:
                raise ValueError("Downstream authorization requires PKCE S256 support")
            self._services[name] = OAuthClient(
                values[0], values[1], service.client_id, service.client_secret_env, service.scopes, service.resource
            )

    def _browser_client(self) -> OAuthClient:
        if not self.config.browser_client_id:
            raise AuthenticationError("browser_login_not_configured", 503)
        methods = self.authenticator.metadata.get("code_challenge_methods_supported", [])
        if not isinstance(methods, list) or "S256" not in methods:
            raise AuthenticationError("pkce_not_supported", 503)
        return OAuthClient(
            self.authenticator.metadata_url("authorization_endpoint"),
            self.authenticator.metadata_url("token_endpoint"),
            self.config.browser_client_id,
            self.config.browser_client_secret_env,
            self.config.browser_scopes,
        )

    @property
    def callback(self) -> str:
        return f"{self.config.public_url.rstrip('/')}/oauth/callback"

    def requires_user(self, state: str) -> bool:
        flow = self._flows.get(state)
        return flow is not None and bool(flow.service)

    def begin(self, authorization_id: str, browser: str, *, service: str = "", user: Principal | None = None) -> str:
        # Callers authorize/inspect the pending task before selecting a service.
        # Login contains only an opaque target, never an arbitrary redirect URL.
        now = time.time()
        self._flows = {key: flow for key, flow in self._flows.items() if flow.expires_at > now}
        if len(self._flows) >= 128 or not browser or len(browser) > 128:
            raise AuthorizationUnavailable()
        client = self._services.get(service) if service else self._browser_client()
        if client is None:
            raise AuthorizationUnavailable()
        if service and user is None:
            raise AuthorizationUnavailable()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        self._flows[state] = Flow(
            verifier,
            hashlib.sha256(browser.encode()).hexdigest(),
            authorization_id,
            service,
            user,
            now + min(300, self.config.approval_timeout),
            client,
        )
        challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = {
            "client_id": client.id,
            "response_type": "code",
            "redirect_uri": self.callback,
            "scope": " ".join(client.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if client.resource:
            query["resource"] = client.resource
        return f"{client.authorization_endpoint}?{urlencode(query)}"

    async def _exchange(self, flow: Flow, code: str) -> dict[str, object]:
        if not code or len(code) > 8192 or any(ord(c) < 33 for c in code):
            raise AuthorizationUnavailable()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.callback,
            "code_verifier": flow.verifier,
        }
        headers: dict[str, str] = {}
        if flow.client.secret_env:
            secret = os.environ.get(flow.client.secret_env, "")
            if not secret:
                raise AuthenticationError("authorization_service_unavailable", 503)
            raw = f"{quote_plus(flow.client.id)}:{quote_plus(secret)}".encode()
            headers["Authorization"] = f"Basic {b64encode(raw).decode()}"
        else:
            data["client_id"] = flow.client.id
        try:
            async with self.authenticator.http.post(
                flow.client.token_endpoint,
                data=data,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                return await self.authenticator._read(response)
        except (aiohttp.ClientError, TimeoutError):
            raise AuthenticationError("authorization_service_unavailable", 503) from None

    def _credential_store(self, record: AuthorizationRecord, service: str) -> CredentialStore:
        key = hashlib.sha256(f"{record.owner}:{record.task_id}:{service}".encode()).hexdigest()
        return CredentialStore(self.config.data_dir / "credentials" / f"{key}.json")

    @staticmethod
    def _fingerprint(service: ServiceConfig) -> str:
        raw = json.dumps(
            [service.issuer, service.client_id, service.scopes, service.resource, service.api_origin]
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    async def finish(self, state: str, code: str, browser: str, user: Principal | None) -> FlowResult:
        flow = self._flows.get(state)
        if (
            flow is None
            or flow.expires_at <= time.time()
            or not hmac.compare_digest(flow.browser_hash, hashlib.sha256(browser.encode()).hexdigest())
        ):
            raise AuthorizationUnavailable()
        del self._flows[state]  # Single use even when the exchange fails.
        if flow.service:
            if user is None or flow.user is None or user.user_name != flow.user.user_name:
                raise AuthorizationUnavailable()
            record = await self.registry.inspect(flow.authorization_id, user)
            if record.kind != "oauth" or record.details.get("service") != flow.service:
                raise AuthorizationUnavailable()
        tokens = await self._exchange(flow, code)
        access = tokens.get("access_token")
        if (
            not isinstance(access, str)
            or not 1 <= len(access) <= 32768
            or any(ord(c) < 33 or ord(c) > 126 for c in access)
            or str(tokens.get("token_type", "")).lower() != "bearer"
        ):
            raise AuthenticationError()
        if not flow.service:
            authenticated = await self.authenticator.authenticate(access)
            authenticated.require("ngn:access")
            authenticated.require("ngn:approve")
            await self.registry.inspect(flow.authorization_id, authenticated)
            return FlowResult(flow.authorization_id, authenticated, access)
        if user is None:
            raise AuthorizationUnavailable()
        # Recheck after network I/O: cancellation, expiry and another decision
        # must invalidate this consent before credentials can release a tool.
        record = await self.registry.inspect(flow.authorization_id, user)
        expiry = tokens.get("expires_in")
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not math.isfinite(expiry)
            or not 1 <= expiry <= 86400
        ):
            raise AuthenticationError()
        granted = tokens.get("scope")
        if granted is not None and (
            not isinstance(granted, str) or not set(flow.client.scopes) <= set(granted.split())
        ):
            raise AuthenticationError("insufficient_scope", 403)
        store = self._credential_store(record, flow.service)
        store.save(access, time.time() + expiry, self._fingerprint(self.config.services[flow.service]))
        try:
            await self.registry.decide(record.id, user, record.nonce, True, record.digest)
        except BaseException:
            store._unlink()
            raise
        return FlowResult(record.id, user)

    async def decline(self, state: str, browser: str, user: Principal | None) -> None:
        flow = self._flows.get(state)
        if (
            flow is None
            or flow.expires_at <= time.time()
            or not hmac.compare_digest(flow.browser_hash, hashlib.sha256(browser.encode()).hexdigest())
        ):
            raise AuthorizationUnavailable()
        del self._flows[state]
        if flow.service:
            if user is None or flow.user is None or user.user_name != flow.user.user_name:
                raise AuthorizationUnavailable()
            record = await self.registry.inspect(flow.authorization_id, user)
            await self.registry.decide(record.id, user, record.nonce, False, record.digest)

    async def credentials(self, session: AuthorizationSession, service: str) -> AccessGrant:
        from a2a.types import TaskState

        configured = self.config.services.get(service)
        if configured is None or service not in self._services:
            raise AuthorizationUnavailable()
        record = await self.registry.create(
            session.task_id,
            session.user,
            "oauth",
            {
                "service": service,
                "issuer": configured.issuer,
                "scopes": list(configured.scopes),
                "resource": configured.resource,
                "api_origin": configured.api_origin,
            },
        )
        try:
            url = f"{self.config.public_url.rstrip('/')}/authorize/{record.id}"
            await session.report(
                TaskState.TASK_STATE_AUTH_REQUIRED,
                f"Additional service authorization required. Review the requested access at {url}",
            )
            await self.registry.wait(record)
            grant = self._credential_store(record, service).load(
                service, self._fingerprint(configured), configured.api_origin
            )
            if grant is None:
                raise AuthorizationUnavailable()
            await session.report(TaskState.TASK_STATE_WORKING, "Service authorization complete; resuming work.")
            return grant
        except Exception as exc:
            session.failure = exc
            raise

    def close(self) -> None:
        self._flows.clear()


async def request_credentials(service: str) -> AccessGrant:
    """Obtain a task-scoped downstream grant from a trusted ngn tool.

    Incoming A2A bearer tokens are never forwarded. The operation remains
    subject to HarnessExecutor approval independently of this service grant.
    """
    from .authorization import authorization_session

    return await authorization_session().request_credentials(service)
