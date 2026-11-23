"""OIDC admission, including ID token validation and browser transports."""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from base64 import urlsafe_b64encode
from typing import TYPE_CHECKING
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nagents.harness.config import HarnessConfig
from nagents.web.http import oidc
from nagents.web.http.app import create_app
from nagents.web.http.security import LocalOnly
from tests.support.web import ControlledHarness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CONFIG = oidc.OidcConfig(
    "https://identity.example.test", "https://workspace.example.test", "example-client", "test-only"
)
ORIGIN = {"Origin": CONFIG.public_origin}
PROTOCOLS = ["ngn.events.v1", "ngn.token.process-token"]
EVENTS_URL = CONFIG.public_origin.replace("https:", "wss:") + "/api/events"
AUDIO_URL = CONFIG.public_origin.replace("https:", "wss:") + "/api/live/sessions/voice/audio"
AUDIO_PROTOCOLS = ["ngn.live.v2", "ngn.token.process-token"]


def app() -> FastAPI:
    server = FastAPI()
    server.add_middleware(
        LocalOnly, authority=urlsplit(CONFIG.public_origin).netloc, token="process-token", origin=CONFIG.public_origin
    )
    server.add_middleware(oidc.OidcLogin, client=oidc.OidcClient(CONFIG))

    @server.get("/")
    @server.get("/dashboard")
    @server.get("/assets/main.js")
    @server.get("/api/bootstrap")
    async def bootstrap() -> dict[str, str]:
        return {"token": "process-token"}

    @server.post("/api/messages")
    async def message() -> dict[str, bool]:
        return {"ok": True}

    @server.websocket("/api/events")
    async def events(socket: WebSocket) -> None:
        await socket.accept(subprotocol="ngn.events.v1")
        await socket.send_text("connected")
        await socket.receive_text()

    @server.websocket("/api/live/sessions/voice/audio")
    async def audio(socket: WebSocket) -> None:
        await socket.accept(subprotocol="ngn.live.v2")
        await socket.send_bytes(b"audio")
        await socket.receive_bytes()

    return server


class Identity:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.query: dict[str, list[str]] = {}
        self.claims: dict[str, object] = {}
        self.token_requests = 0
        self.signing_key = self.key
        self.available = True

    async def request(self, _http: object, method: str, url: str, **kwargs: object) -> dict[str, object]:
        if url.endswith("/.well-known/openid-configuration"):
            if not self.available:
                raise oidc.LoginError
            return {
                "issuer": CONFIG.issuer,
                "authorization_endpoint": CONFIG.issuer + "/authorize",
                "token_endpoint": CONFIG.issuer + "/token",
                "jwks_uri": CONFIG.issuer + "/keys",
            }
        if url == CONFIG.issuer + "/keys":
            key = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return {"keys": [{**key, "kid": "test-key"}]}
        assert url == CONFIG.issuer + "/token" and method == "POST"
        self.token_requests += 1
        data = kwargs["data"]
        assert isinstance(data, dict)
        assert data["redirect_uri"] == CONFIG.public_origin + oidc.CALLBACK
        challenge = urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).decode().rstrip("=")
        assert self.query["code_challenge"] == [challenge]
        assert "client_secret" not in data
        if data["code"] != "assigned-user":
            raise oidc.LoginError
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": CONFIG.issuer,
                "aud": CONFIG.client_id,
                "nonce": self.query["nonce"][0],
                "sub": "assigned-subject",
                "iat": now,
                "exp": now + 60,
                **self.claims,
            },
            self.signing_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        return {"id_token": token, "access_token": "do-not-retain"}

    def start(self, client: TestClient, path: str = "/") -> str:
        response = client.get(path)
        assert response.status_code == 303
        self.query = parse_qs(urlsplit(response.headers["location"]).query)
        assert self.query["scope"] == ["openid"]
        assert self.query["response_type"] == ["code"]
        assert self.query["code_challenge_method"] == ["S256"]
        assert "client_secret" not in self.query
        return self.query["state"][0]


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> Identity:
    provider = Identity()
    monkeypatch.setattr(oidc, "_json", provider.request)
    return provider


@pytest.fixture
def browser() -> Iterator[TestClient]:
    with TestClient(app(), base_url=CONFIG.public_origin, follow_redirects=False) as client:
        yield client


def test_oidc_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ISSUER", "PUBLIC_ORIGIN", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"NGN_WEB_OIDC_{name}", raising=False)
    assert oidc.OidcConfig.from_env() is None
    monkeypatch.setenv("NGN_WEB_OIDC_ISSUER", CONFIG.issuer)
    with pytest.raises(ValueError, match="all four"):
        oidc.OidcConfig.from_env()
    with pytest.raises(ValueError, match="canonical HTTPS origin"):
        oidc.OidcConfig(CONFIG.issuer, CONFIG.public_origin + "/", "client", "secret")
    with pytest.raises(ValueError, match="HTTPS"):
        oidc.OidcConfig("http://identity.example.test", CONFIG.public_origin, "client", "secret")


def test_oidc_boundaries_and_single_use_callback(browser: TestClient, identity: Identity) -> None:
    assert browser.get("/healthz", headers={"Host": "127.0.0.1"}).text == "ok"
    assert browser.get("/api/bootstrap", headers={"Host": "internal.example.test"}).status_code == 403
    for path in ("/api/bootstrap", "/assets/main.js"):
        assert browser.get(path).status_code == 401
    for url, protocols in ((EVENTS_URL, PROTOCOLS), (AUDIO_URL, AUDIO_PROTOCOLS)):
        with (
            pytest.raises(WebSocketDisconnect),
            browser.websocket_connect(url, headers=ORIGIN.copy(), subprotocols=protocols),
        ):
            pytest.fail("Accepted unauthenticated socket")

    state = identity.start(browser, "/dashboard")
    flow_cookie = browser.cookies[oidc.FLOW_COOKIE]
    with TestClient(app(), base_url=CONFIG.public_origin) as other:
        assert other.get(oidc.CALLBACK, params={"state": state, "code": "assigned-user"}).status_code == 403
    assert (
        browser.get(oidc.CALLBACK, params={"state": secrets.token_urlsafe(32), "code": "assigned-user"}).status_code
        == 403
    )
    assert identity.token_requests == 0
    callback = browser.get(oidc.CALLBACK, params={"state": state, "code": "assigned-user"})
    assert callback.status_code == 303 and callback.headers["location"] == "/dashboard"
    cookie = next(value for value in callback.headers.get_list("set-cookie") if value.startswith(oidc.SESSION_COOKIE))
    for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
        assert attribute in cookie
    replay = browser.get(
        oidc.CALLBACK,
        params={"state": state, "code": "assigned-user"},
        headers={"Cookie": f"{oidc.FLOW_COOKIE}={flow_cookie}"},
    )
    assert replay.status_code == 403 and identity.token_requests == 1
    assert browser.get("/api/bootstrap").json() == {"token": "process-token"}
    assert browser.post("/api/messages", headers=ORIGIN, json={}).status_code == 403
    assert browser.post("/api/messages", headers={"X-Ngn-Token": "process-token"}, json={}).status_code == 403
    assert browser.post("/api/messages", headers={**ORIGIN, "X-Ngn-Token": "process-token"}, json={}).status_code == 200
    assert browser.post(oidc.LOGOUT, headers={"Origin": "https://other.example.test"}).status_code == 403
    with browser.websocket_connect(AUDIO_URL, headers=ORIGIN.copy(), subprotocols=AUDIO_PROTOCOLS) as audio:
        assert audio.receive_bytes() == b"audio"
        audio.send_bytes(b"reply")
    with (
        pytest.raises(WebSocketDisconnect),
        browser.websocket_connect(EVENTS_URL, headers={"Origin": "https://other.example.test"}, subprotocols=PROTOCOLS),
    ):
        pytest.fail("Accepted a cross-origin socket")
    with browser.websocket_connect(EVENTS_URL, headers=ORIGIN.copy(), subprotocols=PROTOCOLS) as socket:
        assert socket.receive_text() == "connected"
        assert browser.post(oidc.LOGOUT, headers=ORIGIN).status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
        assert closed.value.code == 1008
    assert browser.get("/api/bootstrap").status_code == 401
    assert browser.get(oidc.SIGNED_OUT).status_code == 200


@pytest.mark.parametrize(
    ("code", "claims"),
    [
        ("unassigned-user", {}),
        ("assigned-user", {"aud": "another-client"}),
        ("assigned-user", {"aud": ["example-client", "another-client"]}),
        ("assigned-user", {"azp": "another-client"}),
        ("assigned-user", {"nonce": "wrong-flow"}),
        ("assigned-user", {"iss": "https://other.example.test"}),
        ("assigned-user", {"sub": ""}),
        ("assigned-user", {"exp": 1}),
    ],
)
def test_oidc_refuses_denied_or_invalid_login(
    browser: TestClient,
    identity: Identity,
    code: str,
    claims: dict[str, object],
) -> None:
    identity.claims = claims
    state = identity.start(browser)
    response = browser.get(oidc.CALLBACK, params={"state": state, "code": code})
    assert response.status_code == 403
    assert oidc.SESSION_COOKIE not in browser.cookies
    assert browser.get("/api/bootstrap").status_code == 401


def test_oidc_refuses_forged_signature_and_unavailable_discovery(browser: TestClient, identity: Identity) -> None:
    identity.available = False
    assert browser.get("/").status_code == 503
    assert browser.get("/api/bootstrap").status_code == 401
    with TestClient(app(), base_url=CONFIG.public_origin, follow_redirects=False) as other:
        identity.available = True
        identity.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        state = identity.start(other)
        assert other.get(oidc.CALLBACK, params={"state": state, "code": "assigned-user"}).status_code == 403


def test_session_expires_without_socket_traffic(
    browser: TestClient, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oidc, "SESSION_SECONDS", 1)
    state = identity.start(browser)
    assert browser.get(oidc.CALLBACK, params={"state": state, "code": "assigned-user"}).status_code == 303
    cookie = browser.cookies[oidc.SESSION_COOKIE]
    with browser.websocket_connect(
        EVENTS_URL, headers={**ORIGIN, "Cookie": f"{oidc.SESSION_COOKIE}={cookie}"}, subprotocols=PROTOCOLS
    ) as socket:
        assert socket.receive_text() == "connected"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
        assert closed.value.code == 1008
    assert browser.get("/api/bootstrap", headers={"Cookie": f"{oidc.SESSION_COOKIE}={cookie}"}).status_code == 401


def test_workspace_serve_opts_into_oidc(tmp_path: Path, identity: Identity, caplog: pytest.LogCaptureFixture) -> None:
    assets = tmp_path / "static"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<html>Test workspace</html>")
    config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
    server = create_app(config, host="0.0.0.0", assets=assets, harness_factory=ControlledHarness, oidc=CONFIG)
    with TestClient(server, base_url=CONFIG.public_origin, follow_redirects=False) as client:
        caplog.set_level(logging.INFO, logger="uvicorn.error")
        rejected = client.get("/api/bootstrap")
        assert rejected.status_code == 401 and "strict-transport-security" in rejected.headers
        assert client.get("/api/bootstrap").status_code == 401
        assert client.get(oidc.CALLBACK, params={"code": "private-marker"}).status_code == 403
        state = identity.start(client)
        assert client.get(oidc.CALLBACK, params={"state": state, "code": "assigned-user"}).status_code == 303
        assert "Test workspace" in client.get("/").text
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.json()["oidc_login"] is True and "strict-transport-security" in bootstrap.headers
        assert client.post(oidc.LOGOUT, headers=ORIGIN).status_code == 204
        events = [record.getMessage() for record in caplog.records if record.getMessage().startswith("web_auth ")]
        assert events == [
            f"web_auth event={event} suppressed=0"
            for event in ("http_unauthenticated", "login_denied", "login_succeeded", "logout")
        ]
