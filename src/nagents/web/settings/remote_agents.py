"""Workspace A2A connections and server-owned outbound OAuth PKCE sign-in."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from starlette.requests import Request  # noqa: TC002 -- FastAPI resolves route annotations at runtime.
from starlette.responses import JSONResponse
from starlette.responses import RedirectResponse

from nagents.harness.config import AgentProfile
from nagents.harness.remote.credentials import RemoteCredentials
from nagents.harness.remote.profiles import RemoteProfile
from nagents.harness.remote.profiles import remote_endpoint

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from ..execution.service import WebState

logger = logging.getLogger(__name__)
CALLBACK = "/remote-agent-login/callback"
COOKIE = "ngn_remote_login"


def callback_url(request: Request) -> str:
    """Use the authenticated browser's same-host Origin, never proxy headers.

    An HTTPS ingress terminates TLS before ngn. Its internal HTTP scheme must
    not become the registered OAuth callback or downgrade the browser cookie.
    """
    origin = request.headers.get("origin", "")
    if origin:
        try:
            remote_endpoint(origin, loopback_http=True)
            parsed = urlsplit(origin)
            if parsed.netloc.lower() != request.headers.get("host", "").lower() or parsed.path not in {"", "/"}:
                raise ValueError
        except ValueError:
            raise HTTPException(403, "The sign-in Origin must match this ngn server") from None
        return origin.rstrip("/") + CALLBACK
    return str(request.base_url).rstrip("/") + CALLBACK


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(max_length=2048)
    rpc_url: str = Field(default="", max_length=2048)
    token_env: str = Field(default="", max_length=128)
    ca_file: str = Field(default="", max_length=2048)
    issuer: str = Field(default="", max_length=2048)
    client_id: str = Field(default="", max_length=2048)
    scopes: list[str] = Field(default_factory=lambda: ["openid"], max_length=32)
    resource: str = Field(default="", max_length=2048)
    loopback_http: bool = False
    timeout: int = Field(default=120, ge=1, le=3600)
    metadata_url: str = Field(default="", max_length=2048)


class RemoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    description: str = Field(default="", max_length=8192)
    user_invocable: bool = True
    subagent_available: bool = True
    connection: ConnectionInput


class TrustInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    trusted: bool


@dataclass(frozen=True)
class LoginFlow:
    agent: str
    profile: RemoteProfile
    cookie_hash: str
    token_endpoint: str
    redirect_uri: str
    expires_at: float
    verifier: str = field(repr=False)


def register(app: FastAPI, get: Callable[[], WebState]) -> None:
    flows: dict[str, LoginFlow] = {}
    login_errors: dict[str, str] = {}

    def require_network() -> None:
        if get().harness.config.demo:
            raise HTTPException(403, "Remote connections are unavailable in offline demo mode")

    def configured(name: str) -> RemoteProfile:
        harness = get().harness
        harness.refresh_agent_definitions()
        if name not in harness.config.profile_names or not harness.config.profile(name).a2a.configured:
            raise HTTPException(404, "Remote agent not found")
        return harness.config.profile(name).a2a

    async def snapshot() -> dict[str, object]:
        harness = get().harness
        harness.refresh_agent_definitions()
        catalog = harness._agent_catalog.remote_store.load()
        get().remote_metadata_refresh.schedule(harness)
        agents = []
        for name, profile in catalog.agents.items():
            advertised = catalog.advertised.get(name)
            authentication = await RemoteCredentials(profile.a2a).status()
            signed_in = authentication["state"] in {"signed_in", "token_ready", "not_required"}
            agents.append(
                {
                    "name": name,
                    "description": profile.description,
                    "remote_name": advertised.name if advertised is not None else "",
                    "remote_description": advertised.description if advertised is not None else "",
                    "metadata_observed_at": advertised.observed_at if advertised is not None else 0.0,
                    "metadata_status": get().remote_metadata_refresh.status(
                        profile.a2a.fingerprint, advertised.observed_at if advertised is not None else 0.0
                    ),
                    "connection": profile.a2a.as_dict(),
                    "signed_in": signed_in,
                    "authentication": authentication,
                    "trust_remote_tools": profile.a2a.fingerprint in catalog.trusted_connections,
                    "login_error": login_errors.get(name, ""),
                    "user_invocable": profile.user_invocable,
                    "subagent_available": profile.subagent_available,
                }
            )
        return {
            "revision": catalog.revision,
            "agents": agents,
            "source_path": str(harness._agent_catalog.remote_store.path),
            "applies": "next_model_round",
        }

    @app.get("/api/remote-agents")
    async def read() -> dict[str, object]:
        return await snapshot()

    @app.post("/api/remote-agents/discover")
    async def discover(body: ConnectionInput) -> dict[str, object]:
        require_network()
        try:
            from nagents.a2a.client.discovery import discover_remote

            discovery = await discover_remote(RemoteProfile.parse(body.model_dump()))
            return {
                "connection": discovery.connection.as_dict(),
                "name": discovery.name,
                "description": discovery.description,
                "authentication": discovery.authentication,
                "client_id_advertised": discovery.client_id_advertised,
            }
        except Exception as error:
            logger.warning("A2A public discovery failed error_type=%s", type(error).__name__)
            raise HTTPException(
                502, "Could not discover this remote agent. Check its URL, CA bundle and public Agent Card."
            ) from None

    @app.put("/api/remote-agents/{name}")
    async def save(name: str, body: RemoteInput) -> dict[str, object]:
        state = get()
        async with state.queued_mutation():
            if state.harness.config.demo or state.harness.mode != "build":
                raise HTTPException(403, "Remote connection editing requires build mode")
            store = state.harness._agent_catalog.remote_store
            current = store.load()
            if name in state.harness.config.profiles and not state.harness.config.profiles[name].a2a.configured:
                raise HTTPException(409, "This name belongs to a local agent")
            try:
                profile = AgentProfile(
                    description=body.description,
                    a2a=RemoteProfile.parse(body.connection.model_dump()),
                    user_invocable=body.user_invocable,
                    subagent_available=body.subagent_available,
                )
                store.save({**current.agents, name: profile}, expected=body.revision)
            except ValueError as error:
                raise HTTPException(409 if "changed" in str(error) else 422, str(error)) from None
            state.harness.refresh_agent_definitions()
            state.changed()
        return await snapshot()

    @app.put("/api/remote-agents/{name}/trust")
    async def set_tool_trust(name: str, body: TrustInput) -> dict[str, object]:
        state = get()
        async with state.queued_mutation():
            if state.harness.config.demo or state.harness.mode != "build":
                raise HTTPException(403, "Remote tool trust requires build mode")
            store = state.harness._agent_catalog.remote_store
            current = store.load()
            if name not in current.agents:
                raise HTTPException(404, "Remote agent not found")
            try:
                store.save(
                    current.agents,
                    expected=body.revision,
                    trust_updates=((name, body.trusted),),
                )
            except ValueError as error:
                raise HTTPException(409 if "changed" in str(error) else 422, str(error)) from None
            state.harness.refresh_agent_definitions()
            state.changed()
        return await snapshot()

    @app.post("/api/remote-agents/{name}/login")
    async def login(name: str, request: Request) -> JSONResponse:
        require_network()
        profile = configured(name)
        login_errors.pop(name, None)
        if not profile.issuer:
            raise HTTPException(422, "This connection does not use browser sign-in")
        if not profile.client_id:
            raise HTTPException(
                422,
                "The remote host has not advertised a public sign-in client. Ask its administrator to configure a public PKCE client and register this ngn server's callback URL with the identity provider: "
                + callback_url(request),
            )
        credentials = RemoteCredentials(profile)
        try:
            async with httpx.AsyncClient(
                verify=credentials.tls(), trust_env=False, follow_redirects=False, timeout=10
            ) as http:
                response = await http.get(
                    profile.metadata_url or f"{profile.issuer.rstrip('/')}/.well-known/openid-configuration"
                )
                response.raise_for_status()
                if len(response.content) > 131072:
                    raise ValueError
                metadata = response.json()
                if metadata.get("issuer") != profile.issuer or "S256" not in metadata.get(
                    "code_challenge_methods_supported", []
                ):
                    raise ValueError
                authorization = remote_endpoint(
                    metadata.get("authorization_endpoint"), loopback_http=profile.loopback_http
                )
                token_endpoint = remote_endpoint(metadata.get("token_endpoint"), loopback_http=profile.loopback_http)
        except Exception as error:
            logger.warning("A2A sign-in discovery failed agent=%s error_type=%s", name, type(error).__name__)
            raise HTTPException(
                502, "Could not discover the configured sign-in service. Check its issuer and CA bundle."
            ) from None
        now = time.time()
        for key in list(flows):
            if flows[key].expires_at <= now:
                del flows[key]
        if len(flows) >= 128:
            raise HTTPException(429, "Too many sign-in attempts. Wait before trying again.")
        cookie = request.cookies.get(COOKIE) or secrets.token_urlsafe(32)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        redirect_uri = callback_url(request)
        flows[state] = LoginFlow(
            name,
            profile,
            hashlib.sha256(cookie.encode()).hexdigest(),
            token_endpoint,
            redirect_uri,
            now + 300,
            verifier,
        )
        query = {
            "client_id": profile.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(profile.scopes),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        }
        if profile.resource:
            query["resource"] = profile.resource
        result = JSONResponse(
            {"authorization_url": f"{authorization}?{urlencode(query)}", "redirect_uri": redirect_uri}
        )
        result.set_cookie(
            COOKIE,
            cookie,
            max_age=300,
            httponly=True,
            secure=urlsplit(redirect_uri).scheme == "https",
            samesite="lax",
            path=CALLBACK,
        )
        return result

    @app.post("/api/remote-agents/{name}/probe")
    async def probe(name: str) -> dict[str, object]:
        require_network()
        profile = configured(name)
        try:
            from nagents.a2a.client.transport import RemoteAgent

            credentials = RemoteCredentials(profile)
            async with RemoteAgent.from_config(
                profile, credentials.token if profile.issuer or profile.token_env else None
            ) as remote:
                await asyncio.to_thread(
                    get().harness._agent_catalog.remote_store.update_metadata,
                    profile.fingerprint,
                    remote.card.name[:200],
                    remote.card.description[:2000],
                )
                return {
                    "connected": True,
                    "name": remote.card.name[:200],
                    "description": remote.card.description[:2000],
                    "message": "Connected. No task was sent.",
                }
        except Exception as error:
            logger.warning("A2A connection probe failed agent=%s error_type=%s", name, type(error).__name__)
            raise HTTPException(
                502, "Connection check failed. Check sign-in, the server URL and its CA bundle."
            ) from None

    @app.get(CALLBACK)
    async def callback(request: Request) -> RedirectResponse:
        require_network()
        # Middleware extracts and removes the query before access logging.
        query = request.scope.get("ngn.remote_callback", {})
        key, code = query.get("state", ""), query.get("code", "")
        flow = flows.get(key)
        if (
            flow is None
            or flow.expires_at <= time.time()
            or not hmac.compare_digest(
                flow.cookie_hash, hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest()
            )
        ):
            raise HTTPException(403, "This remote sign-in expired or belongs to another browser")
        del flows[key]
        if query.get("error") or not code:
            if query.get("error") == "access_denied":
                message = "Sign-in was declined. The remote agent was not connected."
            elif query.get("error") == "invalid_scope":
                message = "The provider rejected the scopes advertised by the remote host. Its administrator needs to correct the host/provider configuration."
            else:
                message = (
                    "The provider rejected the sign-in client or callback. Register this exact callback URL in the identity provider for the host's public client: "
                    + flow.redirect_uri
                )
            login_errors[flow.agent] = message
            raise HTTPException(422, message)
        if configured(flow.agent) != flow.profile:
            raise HTTPException(409, "The remote connection changed during sign-in. Start again.")
        credentials = RemoteCredentials(flow.profile)
        try:
            async with httpx.AsyncClient(
                verify=credentials.tls(), trust_env=False, follow_redirects=False, timeout=10
            ) as http:
                response = await http.post(
                    flow.token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": flow.redirect_uri,
                        "code_verifier": flow.verifier,
                        "client_id": flow.profile.client_id,
                        **({"resource": flow.profile.resource} if flow.profile.resource else {}),
                    },
                )
                response.raise_for_status()
                if len(response.content) > 131072:
                    raise ValueError
                tokens = response.json()
            expires = tokens.get("expires_in")
            if (
                type(expires) not in {int, float}
                or not math.isfinite(expires)
                or not 1 <= expires <= 86400
                or str(tokens.get("token_type", "")).lower() != "bearer"
            ):
                raise ValueError
            if configured(flow.agent) != flow.profile:
                raise ValueError
            granted_scopes = tokens.get("scope")
            if granted_scopes is not None and (
                not isinstance(granted_scopes, str) or not set(flow.profile.scopes) <= set(granted_scopes.split())
            ):
                raise ValueError
            refresh_token = tokens.get("refresh_token", "")
            if refresh_token is None:
                refresh_token = ""
            if not isinstance(refresh_token, str) or (
                refresh_token
                and (len(refresh_token) > 16384 or any(ord(char) < 33 or ord(char) > 126 for char in refresh_token))
            ):
                raise ValueError
            await credentials.save_login(
                tokens.get("access_token", ""),
                time.time() + expires,
                refresh_token=refresh_token,
                token_endpoint=flow.token_endpoint if refresh_token else "",
            )
        except Exception as error:
            logger.warning("A2A sign-in exchange failed agent=%s error_type=%s", flow.agent, type(error).__name__)
            message = (
                "The provider could not complete sign-in. Check the host's public client registration and register this exact callback URL in the provider, then try again: "
                + flow.redirect_uri
            )
            login_errors[flow.agent] = message
            raise HTTPException(502, message) from None
        login_errors.pop(flow.agent, None)
        get().changed()
        result = RedirectResponse("/", status_code=303)
        result.delete_cookie(COOKIE, path=CALLBACK)
        return result
