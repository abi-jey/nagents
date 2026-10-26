"""OAuth callback URLs honor the browser Origin behind HTTPS termination."""

from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from nagents.harness.config import AgentProfile
from nagents.harness.remote_profiles import RemoteProfile
from nagents.web.settings import remote_agents
from tests.support.web import client_app
from tests.web.settings.test_web_remote_agents import AGENT_URL
from tests.web.settings.test_web_remote_agents import CLIENT_ID
from tests.web.settings.test_web_remote_agents import ISSUER
from tests.web.settings.test_web_remote_agents import config
from tests.web.settings.test_web_remote_agents import mock_issuer

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.requires_posix


def make_request(*, scheme: str = "http", host: str, origin: str = "", forwarded_proto: str = "") -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if origin:
        headers.append((b"origin", origin.encode("ascii")))
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/remote-agents/peer/login",
            "raw_path": b"/api/remote-agents/peer/login",
            "query_string": b"",
            "headers": headers,
            "server": (host.split(":", 1)[0], 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_callback_uses_https_origin_matching_same_host_even_when_asgi_scheme_is_http() -> None:
    request = make_request(host="ngn.example.test", origin="https://ngn.example.test")

    assert remote_agents.callback_url(request) == "https://ngn.example.test/remote-agent-login/callback"


def test_callback_rejects_a_hostile_origin() -> None:
    request = make_request(host="ngn.example.test", origin="https://attacker.example")

    with pytest.raises(HTTPException) as error:
        remote_agents.callback_url(request)

    assert error.value.status_code == 403


def test_callback_ignores_forwarded_proto_without_origin() -> None:
    request = make_request(host="ngn.example.test", forwarded_proto="https")

    assert remote_agents.callback_url(request) == "http://ngn.example.test/remote-agent-login/callback"


def test_callback_allows_loopback_origin() -> None:
    request = make_request(host="localhost:8765", origin="http://localhost:8765")

    assert remote_agents.callback_url(request) == "http://localhost:8765/remote-agent-login/callback"


def test_login_sets_secure_cookie_from_https_origin_behind_http_asgi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with client_app(
            tmp_path,
            host="0.0.0.0",
            base_url="http://ngn.example.test",
            config=config(
                tmp_path,
                profiles={
                    "peer": AgentProfile(
                        description="Remote peer",
                        a2a=RemoteProfile(
                            url=AGENT_URL,
                            issuer=ISSUER,
                            client_id=CLIENT_ID,
                            scopes=("openid", "profile"),
                        ),
                    )
                },
            ),
        ) as (_app, client, headers, _):
            mock_issuer(monkeypatch, calls=[])
            request_headers = {**headers, "Origin": "https://ngn.example.test"}
            begun = await client.post("/api/remote-agents/peer/login", headers=request_headers, json={})

            assert begun.status_code == 200, begun.text
            assert begun.json()["redirect_uri"] == "https://ngn.example.test/remote-agent-login/callback"
            cookies = SimpleCookie()
            cookies.load(begun.headers["set-cookie"])
            assert cookies[remote_agents.COOKIE]["secure"]
            assert cookies[remote_agents.COOKIE]["httponly"]

    asyncio.run(scenario())
