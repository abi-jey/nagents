"""Web-owned remote A2A connection persistence and OAuth PKCE boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING
from typing import cast
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlsplit

import httpx
import pytest
from httpx import ASGITransport
from httpx import AsyncClient as _AsyncClient

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.remote_credentials import RemoteCredentials
from nagents.harness.remote_profiles import RemoteProfile
from nagents.harness.remote_store import RemoteAgentMetadata
from nagents.web import remote_agents
from nagents.web.security import LocalOnly
from tests.support.providers import FakeProvider
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    import ssl
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from starlette.types import Message
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from nagents.a2a.client import AccessTokenProvider
    from nagents.a2a.client import RemoteAgent
    from nagents.a2a.remote_config import RemoteAgentConfig
    from nagents.events import Event
    from nagents.types import Message as HarnessMessage

pytestmark = pytest.mark.requires_posix

URL = "http://127.0.0.1:8765"
ISSUER = "https://issuer.example.test"
AGENT_URL = "https://agent.example.test"
CLIENT_ID = "web-remote-example"
AUTH_CODE = "fixture-authorization-code-not-a-credential"
ACCESS_TOKEN = "fixture-access-token-not-a-credential"
REFRESH_TOKEN = "fixture-refresh-token-not-a-credential"
ID_TOKEN = "fixture-id-token-not-a-credential"


@pytest.fixture(autouse=True)
def private_xdg_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "user-config"
    data_home = tmp_path / "user-data"
    config_home.mkdir(mode=0o700)
    config_home.chmod(0o700)
    (config_home / "ngn").mkdir(mode=0o700)
    (config_home / "ngn" / "workspaces").mkdir(mode=0o700)
    data_home.mkdir(mode=0o700)
    data_home.chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


def config(
    tmp_path: Path,
    *,
    profiles: dict[str, AgentProfile] | None = None,
    agent: str = "assistant",
    demo: bool = False,
) -> HarnessConfig:
    return HarnessConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        profiles=profiles or {},
        agent=agent,
        demo=demo,
    )


def connection(*, client_id: str = CLIENT_ID, url: str = AGENT_URL) -> dict[str, object]:
    return {
        "url": url,
        "issuer": ISSUER,
        "client_id": client_id,
        "scopes": ["openid", "profile"],
        "resource": "https://agent.example.test/resource",
        "timeout": 45,
    }


def input_body(revision: str, *, client_id: str = CLIENT_ID, url: str = AGENT_URL) -> dict[str, object]:
    return {
        "revision": revision,
        "description": "Remote review agent",
        "user_invocable": True,
        "subagent_available": True,
        "connection": connection(client_id=client_id, url=url),
    }


def mock_issuer(monkeypatch: pytest.MonkeyPatch, *, calls: list[httpx.Request], token_error: str = "") -> None:
    metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "code_challenge_methods_supported": ["S256"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=metadata, request=request)
        if request.method == "POST" and request.url.path == "/token":
            if token_error:
                return httpx.Response(
                    400,
                    json={"error": token_error, "error_description": "private token endpoint detail"},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "access_token": ACCESS_TOKEN,
                    "refresh_token": REFRESH_TOKEN,
                    "id_token": ID_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(handler)

    class MockIssuerClient(httpx.AsyncClient):
        def __init__(
            self,
            *,
            verify: ssl.SSLContext | str | bool = True,
            trust_env: bool = True,
            follow_redirects: bool = False,
            timeout: float | httpx.Timeout | None = None,
        ) -> None:
            super().__init__(
                verify=verify,
                trust_env=trust_env,
                follow_redirects=follow_redirects,
                timeout=timeout,
                transport=transport,
            )

    monkeypatch.setattr("nagents.web.remote_agents.httpx.AsyncClient", MockIssuerClient)


def install_sdk_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    from nagents.a2a.client import RemoteAgent

    transport = httpx.MockTransport(handler)
    original_factory = RemoteAgent.from_config

    def with_mock_transport(
        cls: type[RemoteAgent],
        profile: RemoteAgentConfig,
        tokens: AccessTokenProvider | None = None,
    ) -> RemoteAgent:
        del cls
        return original_factory(profile, tokens, transport=transport)

    monkeypatch.setattr(RemoteAgent, "from_config", classmethod(with_mock_transport))


def card_for(url: str, data_dir: Path) -> dict[str, object]:
    pytest.importorskip("a2a")
    from google.protobuf.json_format import MessageToDict

    from nagents.a2a.application import agent_card
    from nagents.a2a.config import A2AConfig
    from nagents.a2a.config import IssuerConfig

    config = A2AConfig(
        public_url=url,
        agent="remote-fixture",
        name="Remote fixture",
        description="A deterministic card used by the web probe test.",
        issuer=IssuerConfig(issuer=ISSUER, audience=CLIENT_ID),
        data_dir=data_dir.resolve(),
    )
    return cast("dict[str, object]", MessageToDict(agent_card(config)))


def no_test_tokens(value: object) -> None:
    serialized = json.dumps(value, default=str)
    for secret in (AUTH_CODE, ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN):
        assert secret not in serialized


def test_connection_save_read_revision_conflict_and_no_public_credentials(tmp_path: Path) -> None:
    async def scenario() -> None:
        local_profiles = {"local-helper": AgentProfile(description="Local agent")}
        async with client_app(tmp_path, config=config(tmp_path, profiles=local_profiles)) as (
            _,
            client,
            headers,
            harnesses,
        ):
            before = (await client.get("/api/remote-agents", headers=headers)).json()
            assert before["revision"] == "0" * 64
            assert before["agents"] == []

            saved_response = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(before["revision"]),
            )
            assert saved_response.status_code == 200, saved_response.text
            saved = saved_response.json()
            assert saved["revision"] != before["revision"]
            assert len(saved["agents"]) == 1
            peer = saved["agents"][0]
            assert peer["name"] == "peer-review"
            assert peer["connection"]["issuer"] == ISSUER
            assert peer["connection"]["client_id"] == CLIENT_ID
            assert peer["signed_in"] is False
            assert peer["authentication"] == {
                "state": "sign_in_required",
                "expires_at": 0.0,
                "error_code": "A2A_AUTH_REQUIRED",
                "refreshable": False,
            }
            assert peer["trust_remote_tools"] is False
            no_test_tokens(saved_response.json())

            persisted = harnesses[0]._agent_catalog.remote_store.path
            document = json.loads(persisted.read_text(encoding="utf-8"))
            assert document["version"] == 1
            assert document["agents"]["peer-review"]["connection"]["client_id"] == CLIENT_ID
            no_test_tokens(document)

            stale = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(before["revision"], url="https://changed.example.test"),
            )
            assert stale.status_code == 409
            collision = await client.put(
                "/api/remote-agents/local-helper",
                headers=headers,
                json=input_body(saved["revision"]),
            )
            assert collision.status_code == 409

            read_response = await client.get("/api/remote-agents", headers=headers)
            assert read_response.status_code == 200
            assert read_response.json()["agents"][0]["name"] == "peer-review"
            no_test_tokens(read_response.json())
            dom = await client.get("/", headers=headers)
            assert dom.status_code == 200
            no_test_tokens(dom.text)

    asyncio.run(scenario())


def test_oauth_pkce_cookie_wrong_browser_replay_and_private_token_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        mock_calls: list[httpx.Request] = []
        async with client_app(tmp_path, config=config(tmp_path)) as (app, client, headers, _harnesses):
            mock_issuer(monkeypatch, calls=mock_calls)
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            saved = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(initial["revision"]),
            )
            assert saved.status_code == 200

            begun = await client.post("/api/remote-agents/peer-review/login", headers=headers, json={})
            assert begun.status_code == 200, begun.text
            payload = begun.json()
            authorization = urlsplit(payload["authorization_url"])
            parameters = parse_qs(authorization.query)
            state = parameters["state"][0]
            challenge = parameters["code_challenge"][0]
            redirect_uri = payload["redirect_uri"]
            assert parameters["response_type"] == ["code"]
            assert parameters["code_challenge_method"] == ["S256"]
            assert len(state) >= 40
            assert len(challenge) == 43
            assert parameters["client_id"] == [CLIENT_ID]
            assert parameters["scope"] == ["openid profile"]
            assert parameters["resource"] == ["https://agent.example.test/resource"]
            assert parameters["redirect_uri"] == [redirect_uri]
            no_test_tokens(payload)
            assert "code_verifier" not in json.dumps(payload)

            cookies = SimpleCookie()
            cookies.load(begun.headers["set-cookie"])
            morsel = cookies[remote_agents.COOKIE]
            assert morsel["httponly"]
            assert morsel["path"] == remote_agents.CALLBACK
            assert morsel["max-age"] == "300"
            assert morsel["samesite"].lower() == "lax"
            cookie_value = client.cookies.get(remote_agents.COOKIE)
            assert cookie_value and len(cookie_value) >= 40

            # A different browser cannot use the state/cookie pair and does not consume it.
            async with _AsyncClient(transport=ASGITransport(app=app), base_url=URL) as other_browser:
                wrong_browser = await other_browser.get(
                    remote_agents.CALLBACK,
                    params={"state": state, "code": AUTH_CODE},
                )
            assert wrong_browser.status_code == 403

            callback = await client.get(
                remote_agents.CALLBACK,
                params={"state": state, "code": AUTH_CODE},
            )
            assert callback.status_code == 303
            assert len(mock_calls) == 2
            token_request = next(request for request in mock_calls if request.url.path == "/token")
            form = parse_qs(token_request.content.decode("ascii"))
            verifier = form["code_verifier"][0]
            calculated = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert calculated == challenge
            assert form["code"] == [AUTH_CODE]
            assert form["redirect_uri"] == [redirect_uri]
            assert form["client_id"] == [CLIENT_ID]

            # Replay with the original cookie still fails because the state is one-use.
            async with _AsyncClient(transport=ASGITransport(app=app), base_url=URL) as replay_browser:
                replay = await replay_browser.get(
                    remote_agents.CALLBACK,
                    params={"state": state, "code": AUTH_CODE},
                    headers={"Cookie": f"{remote_agents.COOKIE}={cookie_value}"},
                )
            assert replay.status_code == 403

            profile = RemoteProfile.parse(connection())
            credential = RemoteCredentials(profile)
            assert credential.path.is_file()
            assert credential.path.stat().st_mode & 0o777 == 0o600
            assert credential.path.parent.stat().st_mode & 0o777 == 0o700
            private_contents = json.loads(credential.path.read_text(encoding="utf-8"))
            assert private_contents["access_token"] == ACCESS_TOKEN
            assert private_contents["refresh_token"] == REFRESH_TOKEN
            assert private_contents["token_endpoint"] == f"{ISSUER}/token"
            assert ID_TOKEN not in credential.path.read_text(encoding="utf-8")

            read_response = await client.get("/api/remote-agents", headers=headers)
            assert read_response.status_code == 200
            assert read_response.json()["agents"][0]["signed_in"] is True
            assert read_response.json()["agents"][0]["authentication"]["state"] == "signed_in"
            assert read_response.json()["agents"][0]["authentication"]["refreshable"] is True
            no_test_tokens(read_response.json())
            dom = await client.get("/", headers=headers)
            assert dom.status_code == 200
            no_test_tokens(dom.text)
            assert AUTH_CODE not in caplog.text
            assert ACCESS_TOKEN not in caplog.text
            assert REFRESH_TOKEN not in caplog.text
            assert ID_TOKEN not in caplog.text

    asyncio.run(scenario())


def test_oauth_callback_rejects_connection_changed_during_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        mock_calls: list[httpx.Request] = []
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            mock_issuer(monkeypatch, calls=mock_calls)
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            first = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(initial["revision"]),
            )
            assert first.status_code == 200
            begun = await client.post("/api/remote-agents/peer-review/login", headers=headers, json={})
            assert begun.status_code == 200
            parameters = parse_qs(urlsplit(begun.json()["authorization_url"]).query)
            state = parameters["state"][0]

            changed = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(first.json()["revision"], client_id="changed-client"),
            )
            assert changed.status_code == 200
            callback = await client.get(
                remote_agents.CALLBACK,
                params={"state": state, "code": AUTH_CODE},
            )
            assert callback.status_code == 409
            assert not any(request.url.path == "/token" for request in mock_calls)

    asyncio.run(scenario())


def test_issuer_only_login_explains_public_client_setup_and_exact_callback(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            body = input_body(initial["revision"], client_id="")
            saved = await client.put("/api/remote-agents/peer-review", headers=headers, json=body)
            assert saved.status_code == 200

            result = await client.post("/api/remote-agents/peer-review/login", headers=headers, json={})

            exact_callback = URL + remote_agents.CALLBACK
            assert result.status_code == 422
            assert "public sign-in client" in result.json()["detail"]
            assert exact_callback in result.json()["detail"]
            assert CLIENT_ID not in result.text
            assert AUTH_CODE not in result.text
            assert "secret" not in result.text.lower()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_error",
    ["invalid_client", "unauthorized_client", "redirect_uri_mismatch", "invalid_redirect_uri"],
)
def test_callback_registration_errors_give_safe_exact_callback_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    provider_error: str,
) -> None:
    async def scenario() -> None:
        mock_calls: list[httpx.Request] = []
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            mock_issuer(monkeypatch, calls=mock_calls)
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            saved = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(initial["revision"]),
            )
            assert saved.status_code == 200
            begun = await client.post("/api/remote-agents/peer-review/login", headers=headers, json={})
            assert begun.status_code == 200
            state = parse_qs(urlsplit(begun.json()["authorization_url"]).query)["state"][0]
            exact_callback = begun.json()["redirect_uri"]

            response = await client.get(
                remote_agents.CALLBACK,
                params={"state": state, "error": provider_error, "error_description": "private provider detail"},
            )

            detail = response.json()["detail"]
            assert response.status_code == 422
            assert "Register this exact callback URL" in detail
            assert exact_callback in detail
            assert provider_error not in detail
            assert "private provider detail" not in detail
            assert AUTH_CODE not in response.text
            assert "private provider detail" not in caplog.text
            snapshot = await client.get("/api/remote-agents", headers=headers)
            assert snapshot.status_code == 200
            assert snapshot.json()["agents"][0]["login_error"] == detail
            assert provider_error not in snapshot.text

    asyncio.run(scenario())


def test_access_denied_is_distinct_and_does_not_leak_provider_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        mock_calls: list[httpx.Request] = []
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            mock_issuer(monkeypatch, calls=mock_calls)
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            await client.put("/api/remote-agents/peer-review", headers=headers, json=input_body(initial["revision"]))
            begun = await client.post("/api/remote-agents/peer-review/login", headers=headers, json={})
            state = parse_qs(urlsplit(begun.json()["authorization_url"]).query)["state"][0]

            response = await client.get(
                remote_agents.CALLBACK,
                params={"state": state, "error": "access_denied", "error_description": "private denial text"},
            )

            assert response.status_code == 422
            assert response.json()["detail"] == "Sign-in was declined. The remote agent was not connected."
            assert "private denial text" not in response.text
            assert "private denial text" not in caplog.text

    asyncio.run(scenario())


def test_token_endpoint_rejection_is_safe_and_gives_callback_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        mock_calls: list[httpx.Request] = []
        provider_error = "invalid_client"
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            mock_issuer(monkeypatch, calls=mock_calls, token_error=provider_error)
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            saved = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(initial["revision"]),
            )
            assert saved.status_code == 200
            begun = await client.post("/api/remote-agents/peer-review/login", headers=headers, json={})
            parameters = parse_qs(urlsplit(begun.json()["authorization_url"]).query)
            state = parameters["state"][0]
            response = await client.get(
                remote_agents.CALLBACK,
                params={"state": state, "code": AUTH_CODE},
            )

            callback_url = begun.json()["redirect_uri"]
            assert response.status_code == 502
            assert callback_url in response.json()["detail"]
            assert provider_error not in response.text
            assert "private token endpoint detail" not in response.text
            assert AUTH_CODE not in response.text
            assert provider_error not in caplog.text
            assert "private token endpoint detail" not in caplog.text
            assert AUTH_CODE not in caplog.text
            token_request = next(request for request in mock_calls if request.url.path == "/token")
            assert AUTH_CODE.encode() in token_request.content

    asyncio.run(scenario())


def test_callback_query_is_removed_before_downstream_and_unrelated_queries_stay_rejected(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        observed: dict[str, object] = {}

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            del receive
            observed["query_string"] = scope["query_string"]
            observed["callback"] = scope.get("ngn.remote_callback")
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        state = "s" * 43
        raw_query = urlencode({"state": state, "code": AUTH_CODE}).encode("ascii")
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": remote_agents.CALLBACK,
            "raw_path": remote_agents.CALLBACK.encode("ascii"),
            "query_string": raw_query,
            "headers": [(b"host", b"127.0.0.1:8765"), (b"sec-fetch-site", b"cross-site")],
            "server": ("127.0.0.1", 8765),
            "client": ("127.0.0.1", 10000),
            "root_path": "",
        }
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        middleware = LocalOnly(downstream, authority="127.0.0.1:8765", token="test-local-token")
        await middleware(scope, receive, send)
        assert observed["query_string"] == b""
        assert observed["callback"] == {"state": state, "code": AUTH_CODE}
        assert sent[0]["status"] == 204

        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            rejected = await client.get("/api/remote-agents?unrelated=1", headers=headers)
            assert rejected.status_code == 400
            assert "unrelated" not in rejected.text

    asyncio.run(scenario())


def test_probe_uses_sdk_card_discovery_only_and_returns_safe_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        pytest.importorskip("a2a")
        card = card_for(AGENT_URL, tmp_path / "peer-state")
        requests: list[tuple[str, str]] = []
        fail_remote_reads = False

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            status = 502 if fail_remote_reads else 200
            if status == 200:
                return httpx.Response(status, json=card, request=request)
            return httpx.Response(status, text="UPSTREAM_PRIVATE_ERROR_BODY", request=request)

        install_sdk_transport(monkeypatch, handle)

        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, harnesses):
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            body = input_body(initial["revision"])
            body["connection"] = {"url": AGENT_URL, "timeout": 45}
            saved = await client.put("/api/remote-agents/peer-review", headers=headers, json=body)
            assert saved.status_code == 200

            connected = await client.post("/api/remote-agents/peer-review/probe", headers=headers, json={})
            assert connected.status_code == 200, connected.text
            assert connected.json()["connected"] is True
            assert connected.json()["name"] == "Remote fixture"
            assert "No task was sent" in connected.json()["message"]
            assert harnesses[0]._agent_catalog.remote_store.load().advertised["peer-review"].name == "Remote fixture"
            refreshed = (await client.get("/api/remote-agents", headers=headers)).json()["agents"][0]
            assert refreshed["name"] == "peer-review"
            assert refreshed["description"] == "Remote review agent"
            assert refreshed["remote_name"] == "Remote fixture"
            assert refreshed["remote_description"] == "A deterministic card used by the web probe test."

            fail_remote_reads = True
            failed = await client.post("/api/remote-agents/peer-review/probe", headers=headers, json={})
            assert failed.status_code == 502
            assert failed.json()["detail"] == (
                "Connection check failed. Check sign-in, the server URL and its CA bundle."
            )
            assert "UPSTREAM_PRIVATE_ERROR_BODY" not in failed.text
            assert "UPSTREAM_PRIVATE_ERROR_BODY" not in caplog.text
            assert len(requests) >= 2
            assert all(request == ("GET", "/.well-known/agent-card.json") for request in requests)

    asyncio.run(scenario())


def test_task_catalog_poll_refreshes_stale_agent_card_metadata_without_touching_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        pytest.importorskip("a2a")
        card = card_for(AGENT_URL, tmp_path / "peer-state")
        card["name"] = "Updated remote display name"
        card["description"] = "Updated remote card purpose"
        requests: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(200, json=card, request=request)

        install_sdk_transport(monkeypatch, handle)

        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            store = harness._agent_catalog.remote_store
            profile = AgentProfile(description="Local editorial description", a2a=RemoteProfile(url=AGENT_URL))
            second_profile = AgentProfile(description="Second local note", a2a=profile.a2a)
            initial = store.save(
                {"peer-alias": profile, "peer-alias-2": second_profile},
                expected=store.load().revision,
                trust_updates=(("peer-alias", True),),
            )
            metadata = store.metadata_store.load()
            store.metadata_store.save(
                {
                    "peer-alias": RemoteAgentMetadata(profile.a2a.fingerprint, "Old card name", "Old card text", 0),
                    "peer-alias-2": RemoteAgentMetadata(profile.a2a.fingerprint, "Old card name", "Old card text", 0),
                },
                expected=metadata.revision,
            )

            first = (await client.get("/api/sessions/" + harness.session_id + "/tasks", headers=headers)).json()
            first_profile = next(item for item in first["profiles"] if item["id"] == "peer-alias")
            assert first_profile["remote_name"] == "Old card name"
            assert first_profile["description"] == "Local editorial description"

            deadline = time.monotonic() + 3
            refreshed = store.load()
            while refreshed.advertised.get("peer-alias", RemoteAgentMetadata("0" * 64, "", "", 0)).name != (
                "Updated remote display name"
            ):
                if time.monotonic() >= deadline:
                    raise AssertionError("stale AgentCard metadata did not refresh from the task-index poll")
                await asyncio.sleep(0.01)
                refreshed = store.load()

            second = (await client.get("/api/sessions/" + harness.session_id + "/tasks", headers=headers)).json()
            second_profile = next(item for item in second["profiles"] if item["id"] == "peer-alias")
            assert second_profile["remote_name"] == "Updated remote display name"
            assert second_profile["remote_description"] == "Updated remote card purpose"
            assert second_profile["description"] == "Local editorial description"
            assert second["profile_revision"] == first["profile_revision"]
            assert refreshed.advertised["peer-alias-2"].name == "Updated remote display name"
            assert refreshed.advertised["peer-alias-2"].description == "Updated remote card purpose"
            assert refreshed.revision == initial.revision
            assert refreshed.trusted_connections == initial.trusted_connections
            assert refreshed.agents["peer-alias"].a2a.fingerprint == profile.a2a.fingerprint
            assert requests == ["/.well-known/agent-card.json"]

    asyncio.run(scenario())


def test_card_refresh_has_total_deadline_independent_of_profile_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        pytest.importorskip("a2a")
        from nagents.a2a.client import RemoteAgent
        from nagents.harness.remote_store import RemoteAgentStore
        from nagents.web.remote_metadata import RemoteMetadataRefresh

        class HangingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                del request
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        original_factory = RemoteAgent.from_config

        def with_hanging_transport(
            cls: type[RemoteAgent], profile: RemoteProfile, tokens: AccessTokenProvider | None = None
        ) -> RemoteAgent:
            del cls
            return original_factory(profile, tokens, transport=HangingTransport())

        monkeypatch.setattr(RemoteAgent, "from_config", classmethod(with_hanging_transport))
        monkeypatch.setattr("nagents.web.remote_metadata.REMOTE_CARD_TIMEOUT_SECONDS", 0.01)
        profile = AgentProfile(a2a=RemoteProfile(url=AGENT_URL, timeout=3600))
        store = RemoteAgentStore(tmp_path / "settings" / "connections" / "a2a.json")
        manager = RemoteMetadataRefresh()
        with pytest.raises(TimeoutError):
            await manager._refresh(store, "peer-alias", profile)
        assert not store.path.exists()
        await manager.close()

    asyncio.run(scenario())


def test_metadata_constructor_runs_off_loop_and_is_joined_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        import threading

        pytest.importorskip("a2a")
        from nagents.a2a.client import RemoteAgent
        from nagents.harness.remote_store import RemoteAgentStore
        from nagents.web.remote_metadata import RemoteMetadataRefresh

        started = threading.Event()
        release = threading.Event()
        fallback_released = threading.Event()
        original_factory = RemoteAgent.from_config

        class HangingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                del request
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        def slow_factory(
            cls: type[RemoteAgent], profile: RemoteProfile, tokens: AccessTokenProvider | None = None
        ) -> RemoteAgent:
            del cls
            started.set()
            release.wait()
            return original_factory(profile, tokens, transport=HangingTransport())

        monkeypatch.setattr(RemoteAgent, "from_config", classmethod(slow_factory))
        profile = AgentProfile(a2a=RemoteProfile(url=AGENT_URL))
        manager = RemoteMetadataRefresh()
        task = asyncio.create_task(manager._refresh(RemoteAgentStore(tmp_path / "a2a.json"), "peer", profile))
        manager._tasks[profile.a2a.fingerprint] = task

        def force_release() -> None:
            fallback_released.set()
            release.set()

        safety_release = threading.Timer(2, force_release)
        safety_release.start()
        closing: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert not fallback_released.is_set(), "constructor blocked the event loop until the safety release"
            closing = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            assert not closing.done(), "shutdown returned while the owned constructor thread was still running"
        finally:
            release.set()
            safety_release.cancel()
            if closing is None:
                await manager.close()
            else:
                await closing


@pytest.mark.parametrize("restricted_mode", ["demo", "reviewer"])
def test_restricted_metadata_schedule_never_connects_to_remote_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restricted_mode: str
) -> None:
    async def scenario() -> None:
        from nagents.web.remote_metadata import RemoteMetadataRefresh

        called: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            called.append(request.url.path)
            return httpx.Response(500, request=request)

        install_sdk_transport(monkeypatch, handle)
        async with client_app(tmp_path, config=config(tmp_path)) as (_, _, _, harnesses):
            harness = harnesses[0]
            if restricted_mode == "demo":
                harness.config.demo = True
            else:
                harness.config.read_only = True
            store = harness._agent_catalog.remote_store
            profile = AgentProfile(a2a=RemoteProfile(url=AGENT_URL))
            store.save({"peer-alias": profile}, expected=store.load().revision)
            manager = RemoteMetadataRefresh()
            manager.schedule(harness)
            await asyncio.sleep(0)
            assert not manager._tasks
            assert not called
            await manager.close()

    asyncio.run(scenario())


def test_remote_configure_approval_keeps_the_model_call_id_and_one_history_pair(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]

            async def script(provider: FakeProvider, messages: list[HarnessMessage]) -> AsyncIterator[Event]:
                del messages
                if len(provider.requests) == 1:
                    yield ToolCallEvent(
                        id="stable-call",
                        name="remote_agent_configure",
                        arguments={
                            "name": "peer-review",
                            "url": "https://peer.example.test",
                            "description": "Remote review agent",
                            "issuer": ISSUER,
                            "client_id": CLIENT_ID,
                        },
                    )
                else:
                    yield TextDoneEvent(text="Remote agent connection saved.")

            harness.agent.provider = FakeProvider(harness.config, 0, script)
            harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
            stream = LiveStream(app, headers, harness.session_id, "Configure the remote peer.")
            approval = await stream.event("approval")
            assert approval["tool"] == "remote_agent_configure"

            decided = await client.post(
                "/api/approval",
                headers=headers,
                json={
                    "run_id": approval["run_id"],
                    "approval_id": approval["approval_id"],
                    "call_id": approval["id"],
                    "decision": "allow",
                },
            )
            assert decided.status_code == 200
            events = await stream.run_to_completion()
            await stream.disconnect()

            assert approval["id"] == "stable-call"
            results = [event for event in events if event.get("event") == "tool_result"]
            assert len(results) == 1
            assert results[0]["id"] == "stable-call"
            assert results[0].get("error") in {None, ""}
            assert any(event.get("event") == "run_finished" for event in events)

            detail = await client.get(f"/api/sessions/{harness.session_id}", headers=headers)
            assert detail.status_code == 200
            history = detail.json()["history"]
            assert [row["role"] for row in history] == ["user", "assistant", "tool", "assistant"]
            assistant_calls = [
                call
                for row in history
                if row["role"] == "assistant"
                for call in row["tool_calls"]
                if call.get("name") == "remote_agent_configure"
            ]
            tool_results = [row for row in history if row["role"] == "tool"]
            assert len(assistant_calls) == 1
            assert assistant_calls[0]["id"] == "stable-call"
            assert len(tool_results) == 1
            assert tool_results[0]["tool_call_id"] == "stable-call"
            assert len(history) == 4

    asyncio.run(scenario())


def test_root_remote_prompt_is_persisted_when_credentials_are_missing(tmp_path: Path) -> None:
    async def scenario() -> None:
        profile = RemoteProfile(url=AGENT_URL, issuer=ISSUER, client_id=CLIENT_ID)
        configuration = config(
            tmp_path,
            profiles={"peer": AgentProfile(description="Configured peer", a2a=profile)},
            agent="peer",
        )
        prompt = "Ask the peer a question while its sign-in is missing."
        async with client_app(tmp_path, config=configuration) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
            stream = LiveStream(app, headers, harness.session_id, prompt)
            events = await stream.run_to_completion()
            await stream.disconnect()

            errors = [event for event in events if event.get("event") == "error"]
            assert len(errors) == 1
            assert errors[0].get("recoverable") is False
            assert "sign-in is missing or expired" in str(errors[0].get("message", ""))
            response = await client.get(f"/api/sessions/{harness.session_id}", headers=headers)
            assert response.status_code == 200
            history = response.json()["history"]
            assert len(history) == 1
            assert history[0]["role"] == "user"
            assert history[0]["content"] == prompt
            assert history[0]["message_id"] == stream.message_id
            assert history[0]["ingress_id"].startswith("inbox-")
            assert history[0]["source_verified"] is True

    asyncio.run(scenario())


def test_demo_remote_endpoints_fail_closed_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        profile = RemoteProfile(url=AGENT_URL, issuer=ISSUER, client_id=CLIENT_ID)
        configuration = config(
            tmp_path,
            profiles={"peer-review": AgentProfile(description="Remote peer", a2a=profile)},
            demo=True,
        )
        async with client_app(tmp_path, config=configuration) as (_, client, headers, _):

            def forbidden_network(*args: object, **kwargs: object) -> None:
                raise AssertionError("demo remote endpoint attempted network access")

            monkeypatch.setattr("httpx.AsyncClient", forbidden_network)
            responses = [
                await client.post("/api/remote-agents/discover", headers=headers, json=connection()),
                await client.post("/api/remote-agents/peer-review/login", headers=headers, json={}),
                await client.post("/api/remote-agents/peer-review/probe", headers=headers, json={}),
                await client.get(
                    remote_agents.CALLBACK,
                    headers=headers,
                    params={"state": "s" * 43, "code": "demo-code"},
                ),
            ]

            assert [response.status_code for response in responses] == [403, 403, 403, 403]
            assert all("offline demo mode" in response.json()["detail"] for response in responses)
            trust = await client.put(
                "/api/remote-agents/peer-review/trust",
                headers=headers,
                json={"revision": "0" * 64, "trusted": True},
            )
            assert trust.status_code == 403

    asyncio.run(scenario())


def test_workspace_remote_tool_trust_is_revisioned_and_removed_when_binding_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, harnesses):
            initial = (await client.get("/api/remote-agents", headers=headers)).json()
            saved = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(initial["revision"]),
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["agents"][0]["trust_remote_tools"] is False

            trusted = await client.put(
                "/api/remote-agents/peer-review/trust",
                headers=headers,
                json={"revision": saved.json()["revision"], "trusted": True},
            )
            assert trusted.status_code == 200, trusted.text
            assert trusted.json()["agents"][0]["trust_remote_tools"] is True
            document = json.loads(harnesses[0]._agent_catalog.remote_store.path.read_text(encoding="utf-8"))
            assert document["agents"]["peer-review"]["trust_remote_tools"] is True
            no_test_tokens(trusted.json())

            stale = await client.put(
                "/api/remote-agents/peer-review/trust",
                headers=headers,
                json={"revision": saved.json()["revision"], "trusted": False},
            )
            assert stale.status_code == 409
            after_stale = (await client.get("/api/remote-agents", headers=headers)).json()
            assert after_stale["agents"][0]["trust_remote_tools"] is True

            changed = await client.put(
                "/api/remote-agents/peer-review",
                headers=headers,
                json=input_body(trusted.json()["revision"], url="https://changed.example.test"),
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["agents"][0]["trust_remote_tools"] is False

    asyncio.run(scenario())


def test_remote_tool_trust_cannot_be_enabled_in_reviewer_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, harnesses):
            harnesses[0].config.read_only = True
            response = await client.put(
                "/api/remote-agents/peer-review/trust",
                headers=headers,
                json={"revision": "0" * 64, "trusted": True},
            )
            assert response.status_code == 403
            assert "build mode" in response.json()["detail"]

    asyncio.run(scenario())
