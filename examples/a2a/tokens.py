"""Example IdP clients; all endpoints and credentials come from the host."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import secrets
import time
import webbrowser
from base64 import urlsafe_b64encode
from contextlib import suppress
from urllib.parse import urlencode
from urllib.parse import urlsplit

import httpx
from aiohttp import web


def configured_url(name: str, *, loopback_http: bool) -> str:
    value = os.environ[name]
    parsed = urlsplit(value)
    local = parsed.hostname == "localhost"
    with suppress(ValueError):
        local = local or ipaddress.ip_address(parsed.hostname or "").is_loopback
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not (loopback_http and local and parsed.scheme == "http"))
    ):
        raise ValueError(f"{name} requires HTTPS or explicit development loopback HTTP")
    return value


class Tokens:
    """In-memory access-token cache; never emits token responses or ID tokens.

    Configure an audience/resource at the IdP for NGN_A2A_CLIENT_ID. The optional
    NGN_A2A_RESOURCE is the IdP's OAuth resource parameter, not a model argument.
    Browser login uses Authorization Code + PKCE, without a client secret.
    """

    def __init__(self, mode: str, *, loopback_http: bool = False) -> None:
        self.mode = mode
        self.loopback_http = loopback_http
        self.token = ""
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def __call__(self) -> str:
        if self.mode == "environment":
            return os.environ["NGN_A2A_ACCESS_TOKEN"]
        async with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            client_id = os.environ["NGN_A2A_CLIENT_ID"]
            scope = os.environ.get("NGN_A2A_SCOPES", "ngn:access ngn:invoke ngn:read ngn:cancel")
            resource = os.environ.get("NGN_A2A_RESOURCE", "")
            data = {"client_id": client_id, "scope": scope}
            if resource:
                data["resource"] = resource
            if self.mode == "client-credentials":
                data.update(grant_type="client_credentials", client_secret=os.environ["NGN_A2A_CLIENT_SECRET"])
            elif self.mode == "browser":
                data.update(await self.login(client_id, scope, resource))
            else:
                raise ValueError("Unknown example authentication mode")
            token_url = configured_url("NGN_A2A_TOKEN_URL", loopback_http=self.loopback_http)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as http:
                response = await http.post(token_url, data=data)
                if response.status_code != 200:
                    raise RuntimeError("IdP access-token exchange failed")
                result = response.json()
            token = result.get("access_token")
            expires = result.get("expires_in")
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(expires, (int, float))
                or isinstance(expires, bool)
                or not 30 <= expires <= 86400
            ):
                raise RuntimeError("IdP response requires a short-lived API access token")
            self.token = token
            self.expires = time.monotonic() + expires - 20
            return token

    async def login(self, client_id: str, scope: str, resource: str) -> dict[str, str]:
        authorization_url = configured_url("NGN_A2A_AUTHORIZATION_URL", loopback_http=self.loopback_http)
        port = int(os.environ.get("NGN_A2A_LOGIN_PORT", "8780"))
        if not 1024 <= port <= 65535:
            raise ValueError("NGN_A2A_LOGIN_PORT must be from 1024 to 65535")
        redirect = f"http://127.0.0.1:{port}/callback"
        verifier = secrets.token_urlsafe(48)
        challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        code: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def callback(request: web.Request) -> web.Response:
            supplied_state = request.query.get("state", "")
            supplied_code = request.query.get("code", "")
            if (
                request.host != f"127.0.0.1:{port}"
                or code.done()
                or not secrets.compare_digest(supplied_state, state)
                or not supplied_code
                or len(supplied_code) > 8192
            ):
                return web.Response(status=400, text="Invalid or already used login callback.")
            code.set_result(supplied_code)
            return web.Response(text="Login received. You can close this window.")

        app = web.Application(client_max_size=8192)
        app.router.add_get("/callback", callback)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", port).start()
            parameters = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            if resource:
                parameters["resource"] = resource
            url = f"{authorization_url}?{urlencode(parameters)}"
            print(f"Open this IdP login URL if your browser does not open: {url}")
            webbrowser.open(url)
            authorization_code = await asyncio.wait_for(code, timeout=300)
        finally:
            await runner.cleanup()
        return {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        }
