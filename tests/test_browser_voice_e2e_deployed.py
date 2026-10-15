"""The optional deployment test bridge must not become an open proxy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiohttp
import pytest
from examples.live.browser_e2e import deployed
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.parametrize(
    "origin",
    [
        "http://ngn.example",
        "https://user:secret@ngn.example",
        "https://ngn.example/api",
        "https://ngn.example?token=x",
        "https://ngn.example#fragment",
        "https://ngn.example\\evil",
        "https://ngn.example:bad",
    ],
)
def test_bridge_requires_one_plain_https_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        deployed.deployed_origin(origin)


@pytest.mark.parametrize(
    "path",
    ["//evil.example", "/api/../other", "/api/%2e%2e/other", "/api/%5cevil", "/other", "https://evil.example/api"],
)
def test_bridge_rejects_non_ngn_and_ambiguous_paths(path: str) -> None:
    assert not deployed.allowed_path(path)


def test_bridge_checks_exact_loopback_host_origin_and_fetch_metadata() -> None:
    headers = {"host": "127.0.0.1:8880", "origin": "http://127.0.0.1:8880", "sec-fetch-site": "same-origin"}
    assert deployed.same_origin(headers, 8880, websocket=True)
    for name, bad_value in (
        ("host", "evil.example:8880"),
        ("origin", "https://evil.example"),
        ("sec-fetch-site", "cross-site"),
    ):
        assert not deployed.same_origin({**headers, name: bad_value}, 8880)
    del headers["origin"]
    assert deployed.same_origin(headers, 8880)
    assert not deployed.same_origin(headers, 8880, websocket=True)


@pytest.mark.asyncio
async def test_websocket_redirect_trace_aborts_before_following_location() -> None:
    with pytest.raises(deployed.RedirectRefused):
        await deployed.refuse_redirect(object(), object(), object())


class Response:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.headers = {
            "Content-Type": "application/json",
            "Set-Cookie": "private=x",
            "Location": "https://evil.example",
        }
        self.content = self
        self.closed = False

    async def iter_any(self) -> AsyncIterator[bytes]:
        yield b'{"ok":'
        yield b"true}"

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def request(self, method: str, url: str, **kwargs: object) -> Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.response


def test_bridge_streams_only_fixed_origin_and_explicit_ngn_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = Session(Response())
    options: dict[str, object] = {}

    def make_session(**kwargs: object) -> Session:
        options.update(kwargs)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", make_session)
    app = deployed.create_bridge("https://ngn.example/", tmp_path, port=8880)
    with TestClient(app, base_url="http://127.0.0.1:8880") as client:
        response = client.post(
            "/api/live/sessions",
            json={"context_mode": "none"},
            headers={
                "origin": "http://127.0.0.1:8880",
                "X-Ngn-Token": "test-token",
                "Cookie": "do-not-forward=x",
                "Authorization": "do-not-forward",
            },
        )
    assert response.json() == {"ok": True}
    assert "set-cookie" not in response.headers
    assert "location" not in response.headers
    request = session.requests[0]
    assert request["url"] == "https://ngn.example/api/live/sessions"
    assert request["allow_redirects"] is False
    assert request["headers"] == {
        "accept": "*/*",
        "content-type": "application/json",
        "x-ngn-token": "test-token",
        "Origin": "https://ngn.example",
        "Accept-Encoding": "identity",
    }
    assert options["trust_env"] is False
    assert session.response.closed


def test_bridge_refuses_redirects_and_untrusted_callers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = Session(Response(302))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: session)
    with TestClient(
        deployed.create_bridge("https://ngn.example", tmp_path, port=8880), base_url="http://127.0.0.1:8880"
    ) as client:
        rejected = client.get("/api/live", headers={"origin": "https://evil.example"})
        assert rejected.status_code == 403
        assert not session.requests
        response = client.get("/api/live")
        assert response.status_code == 502
        assert "location" not in response.headers
        assert len(session.requests) == 1
        assert session.response.closed
