"""HTTP admission and credential selection for GPT-Live with a ChatGPT login."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.provider.openai import CodexConfigError
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import _load_config
from nagents.web.live_auth import LOGIN_VOICES
from nagents.web.live_settings import LIVE_VOICES
from nagents.web.service import Run
from tests.providers.test_openai_local import write_config
from tests.providers.test_openai_local import write_oauth_auth
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Never

    import httpx

    from nagents.agent import Agent
    from nagents.web.live_login import LoginVoiceConfig

LOGIN_SECRET = "synthetic-private-chatgpt-access-token"
KEY_SECRET = "synthetic-private-api-key"
ACCOUNT = "synthetic-private-account-id"
SESSION = "live-login-fixture"
OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


@dataclass
class LoginState:
    available: bool = True
    reads: int = 0


@pytest.fixture
def login(monkeypatch: pytest.MonkeyPatch) -> LoginState:
    state = LoginState()

    def logged_in(_auth: OpenAIAuth) -> bool:
        return state.available

    async def credentials(_auth: OpenAIAuth) -> CodexCredentials:
        assert state.available, "Readiness must reject missing login before requesting credentials"
        state.reads += 1
        return CodexCredentials(LOGIN_SECRET, ACCOUNT)

    def no_codex(*, model: str) -> Never:
        raise CodexConfigError("No fixture Codex configuration")

    monkeypatch.setattr(OpenAIAuth, "logged_in", logged_in)
    monkeypatch.setattr(OpenAIAuth, "credentials", credentials)
    monkeypatch.setattr("nagents.web.live_auth._load_config", no_codex)
    monkeypatch.delenv("TEST_LOGIN_VOICE_KEY", raising=False)
    return state


class RoutedLiveService:
    def __init__(
        self,
        factory: Callable[[str], Agent],
        *,
        login_factory: Callable[[str], LoginVoiceConfig | None],
    ) -> None:
        self.login_factory = login_factory
        self.active_session_id = ""
        self.browser_calls: list[tuple[str, str]] = []
        self.relay_calls: list[str] = []
        self.configs: list[LoginVoiceConfig] = []

    async def create(self, offer: str, voice: str = "") -> dict[str, object]:
        config = self.login_factory(voice)
        assert config is not None
        self.configs.append(config)
        credentials = await config.credentials()
        assert credentials.access_token == LOGIN_SECRET and credentials.account_id == ACCOUNT
        self.browser_calls.append((offer, voice))
        self.active_session_id = SESSION
        return {"session_id": SESSION, "model": config.model, "voice": config.voice, "sdp": OFFER}

    async def create_stream(self, voice: str = "") -> dict[str, object]:
        self.relay_calls.append(voice)
        self.active_session_id = SESSION
        return {"session_id": SESSION, "model": "gpt-live-1", "voice": voice or "marin"}

    async def close(self, session_id: str) -> dict[str, object]:
        self.active_session_id = ""
        return {"session_id": session_id, "status": "closed"}

    async def shutdown(self) -> None:
        self.active_session_id = ""


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> list[RoutedLiveService]:
    instances: list[RoutedLiveService] = []

    def service(
        factory: Callable[[str], Agent],
        *,
        login_factory: Callable[[str], LoginVoiceConfig | None],
        caption_factory: object = None,
    ) -> RoutedLiveService:
        del caption_factory
        instance = RoutedLiveService(factory, login_factory=login_factory)
        instances.append(instance)
        return instance

    monkeypatch.setattr("nagents.web.app.LiveService", service)
    return instances


def configuration(path: Path, auth: str = "chatgpt") -> HarnessConfig:
    providers = ScopedProviderRegistryStore(path)
    registry = ProviderRegistry(
        active="chat",
        providers={
            "chat": ProviderProfile(kind="anthropic", auth="api-key"),
            "voice": ProviderProfile(kind="openai", auth=auth, api_key_env="TEST_LOGIN_VOICE_KEY"),
        },
    )
    providers.save_scope(registry, expected="0" * 64, scope="global")
    return HarnessConfig(
        workspace=path,
        data_dir=path / "data",
        provider="anthropic",
        provider_id="chat",
        auth="api-key",
        model="chat-model",
    )


async def configure(client: httpx.AsyncClient, headers: dict[str, str], **preferences: str | bool) -> str:
    before = (await client.get("/api/live/settings?scope=workspace", headers=headers)).json()
    result = await client.post(
        "/api/live/settings",
        headers=headers,
        json={
            "scope": "workspace",
            "revision": before["revision"],
            "overrides": {"enabled": True, "connection_id": "voice", **preferences},
        },
    )
    assert result.status_code == 200, result.text
    return str(result.json()["revision"])


@pytest.mark.parametrize("auth", ["chatgpt", "auto"])
@pytest.mark.parametrize("key_available", [False, True])
def test_login_readiness_uses_saved_auth_without_api_keys_or_secret_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: LoginState, auth: str, key_available: bool
) -> None:
    if key_available:
        monkeypatch.setenv("TEST_LOGIN_VOICE_KEY", KEY_SECRET)

    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, auth)) as (app, client, headers, _):
            await configure(client, headers)
            capability = await client.get("/api/live", headers=headers)
            settings = await client.get("/api/live/settings", headers=headers)
            assert capability.json()["available"] and capability.json()["key_configured"]
            assert capability.json()["voice_auth"] == settings.json()["voice_auth"] == "chatgpt"
            assert capability.json()["transport"] == "webrtc"
            assert capability.json()["model"] == settings.json()["values"]["model"] == "gpt-live-1-codex"
            assert settings.json()["global_preferences"]["model"] == "gpt-live-1"
            assert capability.json()["voices"] == settings.json()["voices"] == list(LOGIN_VOICES)
            assert capability.json()["voice"] == "cove"
            assert capability.json()["assistant"]["provider"] == "anthropic"
            assert login.reads == 0, "Discovery must not refresh or expose bearer credentials"
            connection = await app.state.live_settings.connection()
            assert connection.api_key.get_secret_value() == ""
            for response in (capability, settings):
                assert response.headers["cache-control"] == "no-store"
                for secret in (LOGIN_SECRET, KEY_SECRET, ACCOUNT):
                    assert secret not in response.text

    asyncio.run(check())


def test_missing_chatgpt_login_does_not_fall_back_to_an_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: LoginState, services: list[RoutedLiveService]
) -> None:
    login.available = False
    monkeypatch.setenv("TEST_LOGIN_VOICE_KEY", KEY_SECRET)

    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            revision = await configure(client, headers)
            capability = (await client.get("/api/live", headers=headers)).json()
            assert not capability["available"] and not capability["key_configured"]
            assert "Sign in" in capability["reason"] and "API key" not in capability["reason"]
            result = await client.post(
                "/api/live/sessions",
                headers=headers,
                json={"revision": revision, "session_id": harnesses[0].session_id, "sdp": OFFER},
            )
            assert result.status_code == 503 and not services[0].browser_calls and not services[0].relay_calls
            assert login.reads == 0

    asyncio.run(check())


def test_login_sdp_route_admits_the_selected_assistant_and_selected_voice(
    tmp_path: Path, login: LoginState, services: list[RoutedLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            revision = await configure(client, headers, voice="ember")
            body = {"revision": revision, "session_id": harnesses[0].session_id}
            missing = await client.post("/api/live/sessions", headers=headers, json=body)
            assert missing.status_code == 422 and "WebRTC offer" in missing.text
            wrong_voice = await client.post(
                "/api/live/sessions", headers=headers, json={**body, "sdp": OFFER, "voice": "marin"}
            )
            assert wrong_voice.status_code == 422
            wrong_chat = await client.post(
                "/api/live/sessions", headers=headers, json={**body, "sdp": OFFER, "session_id": "ngn-other"}
            )
            assert wrong_chat.status_code == 409
            assert not services[0].browser_calls and not services[0].relay_calls
            result = await client.post("/api/live/sessions", headers=headers, json={**body, "sdp": OFFER})
            assert result.status_code == 201, result.text
            assert result.json() == {"session_id": SESSION, "voice": "ember", "model": "gpt-live-1-codex", "sdp": OFFER}
            assert services[0].browser_calls == [(OFFER, "")] and not services[0].relay_calls
            assert "selected chat" in services[0].configs[0].instructions
            assert login.reads == 1
            connection = await app.state.live_settings.connection()
            assert connection.profile_name == "voice" and harnesses[0].config.provider_id == "chat"
            assert all(secret not in result.text for secret in (LOGIN_SECRET, KEY_SECRET, ACCOUNT))
            await client.post(f"/api/live/sessions/{SESSION}/close", headers=headers, json={})

    asyncio.run(check())


def test_chatgpt_hosted_backend_is_rejected_before_provisioning(
    tmp_path: Path, login: LoginState, services: list[RoutedLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            revision = await configure(client, headers, backend_mode="hosted")
            capability = (await client.get("/api/live", headers=headers)).json()
            assert not capability["available"] and "Main assistant" in capability["reason"]
            result = await client.post(
                "/api/live/sessions",
                headers=headers,
                json={"revision": revision, "session_id": harnesses[0].session_id, "sdp": OFFER},
            )
            assert result.status_code == 503 and "main assistant" in result.text
            assert not services[0].browser_calls and not services[0].relay_calls and login.reads == 0

    asyncio.run(check())


def test_explicit_api_key_voice_keeps_relay_transport_even_with_saved_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: LoginState, services: list[RoutedLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, "api-key")) as (_, client, headers, harnesses):
            revision = await configure(client, headers)
            missing = (await client.get("/api/live", headers=headers)).json()
            assert not missing["available"] and not missing["key_configured"]
            assert missing["voice_auth"] == "api-key" and "TEST_LOGIN_VOICE_KEY" in missing["reason"]
            monkeypatch.setenv("TEST_LOGIN_VOICE_KEY", KEY_SECRET)
            capability = (await client.get("/api/live", headers=headers)).json()
            assert capability["available"] and capability["voice_auth"] == "api-key"
            assert capability["transport"] == "websocket" and capability["voices"] == list(LIVE_VOICES)
            body = {"revision": revision, "session_id": harnesses[0].session_id, "voice": "cedar"}
            invalid = await client.post("/api/live/sessions", headers=headers, json={**body, "sdp": OFFER})
            assert invalid.status_code == 422 and "server audio relay" in invalid.text
            result = await client.post("/api/live/sessions", headers=headers, json=body)
            assert result.status_code == 201 and services[0].relay_calls == ["cedar"]
            assert not services[0].browser_calls and login.reads == 0

    asyncio.run(check())


def test_login_voice_can_connect_during_assistant_work_without_unlocking_settings(
    tmp_path: Path, login: LoginState, services: list[RoutedLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            revision = await configure(client, headers)
            settings = (await client.get("/api/settings", headers=headers)).json()
            release = asyncio.Event()

            async def working() -> None:
                await release.wait()

            state = app.state.web
            active = Run(harnesses[0].session_id, server_owned=True)
            active.task = asyncio.create_task(working())
            state.active = active
            try:
                response = await client.post(
                    "/api/live/sessions",
                    headers=headers,
                    json={"revision": revision, "session_id": active.session_id, "sdp": OFFER},
                )
                assert response.status_code == 201, response.text
                assert state.active is active and not active.task.done() and not active.task.cancelling()
                changed = await client.post(
                    "/api/settings",
                    headers=headers,
                    json={"revision": settings["revision"], "values": settings["values"]},
                )
                assert changed.status_code == 409
                assert services[0].browser_calls == [(OFFER, "")] and login.reads == 1
                assert state.active is active and not active.task.done()
            finally:
                release.set()
                await active.task
                state.finish(active)

    asyncio.run(check())


@pytest.mark.parametrize("auth", ["auto", "codex"])
@pytest.mark.parametrize("invalid", ["malformed", "expired", "workspace"])
def test_invalid_codex_login_never_falls_back_to_an_environment_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: LoginState,
    services: list[RoutedLiveService],
    auth: str,
    invalid: str,
) -> None:
    login.available = False
    codex_home = tmp_path / "codex"
    write_config(codex_home, 'forced_chatgpt_workspace_id = "required-workspace"\n' if invalid == "workspace" else "")
    if invalid == "malformed":
        (codex_home / "auth.json").write_text("invalid JSON")
    else:
        write_oauth_auth(codex_home, expires=0 if invalid == "expired" else None)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", KEY_SECRET)
    monkeypatch.setenv("TEST_LOGIN_VOICE_KEY", KEY_SECRET)
    monkeypatch.setattr("nagents.web.live_auth._load_config", _load_config)

    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, auth)) as (_, client, headers, harnesses):
            revision = await configure(client, headers)
            capability = (await client.get("/api/live", headers=headers)).json()
            assert capability["voice_auth"] == "chatgpt"
            assert not capability["available"] and not capability["key_configured"]
            response = await client.post(
                "/api/live/sessions",
                headers=headers,
                json={"revision": revision, "session_id": harnesses[0].session_id, "sdp": OFFER},
            )
            assert response.status_code == 503
            assert not services[0].browser_calls and not services[0].relay_calls

    asyncio.run(check())


@pytest.mark.parametrize("auth", ["auto", "codex"])
def test_only_auto_may_use_an_api_key_when_no_codex_configuration_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login: LoginState,
    services: list[RoutedLiveService],
    auth: str,
) -> None:
    login.available = False
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent-codex"))
    monkeypatch.setenv("OPENAI_API_KEY", KEY_SECRET)
    monkeypatch.setenv("TEST_LOGIN_VOICE_KEY", KEY_SECRET)
    monkeypatch.setattr("nagents.web.live_auth._load_config", _load_config)

    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, auth)) as (_, client, headers, harnesses):
            revision = await configure(client, headers)
            capability = (await client.get("/api/live", headers=headers)).json()
            assert capability["available"] is (auth == "auto")
            assert capability["voice_auth"] == ("api-key" if auth == "auto" else "chatgpt")
            response = await client.post(
                "/api/live/sessions",
                headers=headers,
                json={"revision": revision, "session_id": harnesses[0].session_id},
            )
            assert response.status_code == (201 if auth == "auto" else 503)
            assert bool(services[0].relay_calls) is (auth == "auto")
            assert not services[0].browser_calls

    asyncio.run(check())
