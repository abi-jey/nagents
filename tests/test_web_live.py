"""Dedicated Live configuration and the real serve HTTP/lifespan boundary, without provider I/O."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from nagents.harness.config import LIVE_VOICES
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.live import LiveConfig
from nagents.provider import FoundryProvider
from nagents.provider import ProviderType
from nagents.web.live import MAX_SDP_CHARACTERS
from nagents.web.live import capabilities
from nagents.web.live import create_agent
from tests.support.web import URL
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.agent import Agent

SECRET = "sk-fixture-live-private-key"
CHAT_SECRET = "sk-fixture-chat-private-key"
SESSION = "live-fixture-session"
OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def configuration(path: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace=path,
        data_dir=path / "data",
        provider="anthropic",
        model="chat-only-model",
        auth="api-key",
        api_key_env="CHAT_TEST_KEY",
        live_enabled=True,
    )


class FakeLiveService:
    """Runtime contract stub; real service ownership/races have their own suite."""

    def __init__(self, factory: Callable[[str], Agent]) -> None:
        self.factory = factory
        self.loop = asyncio.get_running_loop()
        self.active_session_id = ""
        self.creates: list[tuple[str, str]] = []
        self.snapshots: list[tuple[str, int]] = []
        self.closes: list[str] = []
        self.shutdown_calls = 0

    async def create(self, sdp: str, voice: str = "") -> dict[str, object]:
        self.creates.append((sdp, voice))
        self.active_session_id = SESSION
        return {"session_id": SESSION, "sdp": "fixture-answer", "model": "gpt-live-1", "voice": voice or "marin"}

    async def snapshot(self, session_id: str, after: int = 0) -> dict[str, object]:
        self.snapshots.append((session_id, after))
        if session_id != SESSION:
            raise HTTPException(404, "Live session not found.")
        return {
            "session_id": session_id,
            "status": "connected",
            "model": "gpt-live-1",
            "voice": "marin",
            "events": [{"seq": 38, "type": "transcript", "speaker": "assistant", "text": "Hello."}],
            "cursor": 38,
        }

    async def close(self, session_id: str) -> dict[str, object]:
        self.closes.append(session_id)
        self.active_session_id = ""
        return {"session_id": session_id, "status": "closed"}

    async def shutdown(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.shutdown_calls += 1
        self.active_session_id = ""
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NGN_LIVE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("CHAT_TEST_KEY", CHAT_SECRET)


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> list[FakeLiveService]:
    instances: list[FakeLiveService] = []

    def factory(agent_factory: Callable[[str], Agent]) -> FakeLiveService:
        service = FakeLiveService(agent_factory)
        instances.append(service)
        return service

    monkeypatch.setattr("nagents.web.app.LiveService", factory)
    return instances


def test_live_defaults_environment_and_yaml_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = load_config(tmp_path)
    assert not defaults.live_enabled and defaults.live_provider == "openai"
    assert defaults.live_model == "gpt-live-1" and defaults.live_voice == "marin"
    assert defaults.live_backend_model == LiveConfig().backend_model
    assert defaults.live_base_url == "" and defaults.live_api_key_env == "OPENAI_API_KEY"
    for name, value in {
        "ENABLED": "1",
        "PROVIDER": "openai_compatible",
        "MODEL": "voice-deployment",
        "BACKEND_MODEL": "reasoning-deployment",
        "VOICE": "cedar",
        "BASE_URL": "https://voice.example.invalid/v1",
        "API_KEY_ENV": "DEDICATED_VOICE_KEY",
    }.items():
        monkeypatch.setenv(f"NGN_LIVE_{name}", value)
    env = load_config(tmp_path)
    assert env.live_enabled and env.live_provider == "openai_compatible"
    assert env.live_model == "voice-deployment" and env.live_backend_model == "reasoning-deployment"
    assert env.live_voice == "cedar" and env.live_base_url == "https://voice.example.invalid/v1"
    assert env.live_api_key_env == "DEDICATED_VOICE_KEY"
    config_file = tmp_path / "live.yaml"
    config_file.write_text("live_enabled: false\nlive_voice: marin\nlive_model: yaml-model\n")
    configured = load_config(tmp_path, config_file)
    assert not configured.live_enabled and configured.live_voice == "marin" and configured.live_model == "yaml-model"
    assert configured.live_backend_model == "reasoning-deployment"
    assert SECRET not in repr(configured) and CHAT_SECRET not in repr(configured)
    monkeypatch.setenv("NGN_LIVE_ENABLED", "yes")
    with pytest.raises(ValueError, match="NGN_LIVE_ENABLED"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "document",
    [
        'live_enabled: "true"',
        "live_provider: unknown",
        "live_model: 123",
        'live_model: ""',
        'live_model: "not a model"',
        "live_model: " + "x" * 129,
        'live_backend_model: "  "',
        "live_voice: custom-secret-voice",
        "live_api_key_env: sk-literal-secret",
        "live_api_key: literal-secret",
        "live_base_url: file:///tmp/voice",
        "live_base_url: http://voice.example.invalid/v1",
        "live_base_url: https://user:secret@voice.example.invalid/v1",
        "live_base_url: https://voice.example.invalid/v1?key=secret",
        "live_base_url: https://voice.example.invalid/v1#secret",
        "live_base_url: https://voice.example.invalid:not-a-port/v1",
        "live_base_url: https://voice.example.invalid/v1/live/sessions",
        "live_base_url: https://voice.example.invalid/v1/responses",
    ],
)
def test_live_config_rejects_invalid_or_credential_bearing_values(tmp_path: Path, document: str) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(document)
    with pytest.raises(ValueError):
        load_config(tmp_path, path)


def test_untrusted_project_cannot_enable_or_redirect_live(tmp_path: Path) -> None:
    project = tmp_path / ".ngn"
    project.mkdir()
    (project / "config.yaml").write_text(
        "live_enabled: true\nlive_base_url: https://voice.example.invalid/v1\nlive_api_key_env: OTHER_KEY\n"
    )
    with pytest.warns(UserWarning, match="Ignoring untrusted project"):
        config = load_config(tmp_path)
    assert not config.live_enabled and not config.live_base_url and config.live_api_key_env == "OPENAI_API_KEY"
    trusted = load_config(tmp_path, trust_project=True)
    assert trusted.live_enabled and trusted.live_api_key_env == "OTHER_KEY"


@pytest.mark.parametrize("provider", ["openai", "openai_compatible", "azure_openai_compatible_v1"])
def test_voice_factory_owns_only_dedicated_provider_and_tool_free_hosted_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("DEDICATED_VOICE_KEY", SECRET)
    config = replace(
        configuration(tmp_path),
        live_provider=provider,
        live_base_url="https://voice.example.invalid/openai/v1",
        live_api_key_env="DEDICATED_VOICE_KEY",
        live_model="voice-deployment",
        live_backend_model="reasoning-deployment",
        live_voice="cedar",
        plugins=("not.imported:setup",),
    )

    async def check() -> None:
        agent = create_agent(config)
        second = create_agent(config, "marin")
        try:
            assert agent.provider is not second.provider and agent.session is not second.session
            assert agent.provider.api_key == SECRET and agent.provider.api_key != CHAT_SECRET
            assert agent.provider.live_endpoint() == "https://voice.example.invalid/openai/v1/live/sessions"
            assert agent.provider.model == "voice-deployment" and agent.provider.retry_config.max_retries == 0
            assert agent.tool_registry.get_all() == [] and agent.plugins == []
            assert agent.workspace is None and agent.delegation_agent is None and agent.audio is None
            assert agent.compactor is None and agent.skill_discoverer is None and not agent.save_tool_outputs
            assert agent.session.db_path == Path(":memory:") and not agent.session._initialized
            payload = agent.live_configuration(media=True)
            assert payload["audio"] == {"output": {"voice": "cedar"}}
            assert second.live_configuration(media=True)["audio"] == {"output": {"voice": "marin"}}
            assert payload["input"] == [] and payload["store"] is False
            assert payload["delegation"] == {
                "type": "responses",
                "responses": {
                    "model": "reasoning-deployment",
                    "instructions": LiveConfig().backend_instructions,
                    "tools": [],
                    "tool_choice": "auto",
                },
            }
            headers = await agent.provider.auth_headers(agent.provider.live_endpoint(websocket=True))
            if provider == "azure_openai_compatible_v1":
                assert isinstance(agent.provider, FoundryProvider) and headers == {"api-key": SECRET}
            else:
                assert agent.provider.provider_type == ProviderType.OPENAI_COMPATIBLE
                assert headers == {"Authorization": f"Bearer {SECRET}"}
            assert SECRET not in str(capabilities(config)) and CHAT_SECRET not in str(payload)
        finally:
            await agent.close()
            await second.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("changes", "key", "reason"),
    [
        ({"live_enabled": False}, SECRET, "disabled"),
        ({"demo": True}, SECRET, "demo"),
        ({}, "", "OPENAI_API_KEY"),
        ({"live_api_key_env": "UNSET_VOICE_KEY"}, SECRET, "UNSET_VOICE_KEY"),
        ({"live_provider": "anthropic"}, SECRET, "unsupported"),
        ({"live_provider": "azure_openai_compatible_v1"}, SECRET, "live_base_url"),
        ({}, "secret\ninvalid-key", "invalid"),
    ],
)
def test_capability_reason_and_disabled_admission_are_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    services: list[FakeLiveService],
    changes: dict[str, object],
    key: str,
    reason: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.delenv("UNSET_VOICE_KEY", raising=False)
    config = configuration(tmp_path)
    for name, value in changes.items():
        setattr(config, name, value)
    config.validate()

    async def check() -> None:
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            response = await client.get("/api/live", headers=headers)
            assert response.status_code == 200
            info = response.json()
            assert info["available"] is False and reason in info["reason"]
            assert info["model"] == "gpt-live-1" and info["voice"] == "marin"
            assert info["backend_model"] == LiveConfig().backend_model and info["voices"] == list(LIVE_VOICES)
            assert info["active_session_id"] == ""
            refused = await client.post("/api/live/sessions", json={"sdp": OFFER}, headers=headers)
            assert refused.status_code == 503 and refused.json()["detail"] == info["reason"]
            assert not services[0].creates
            for value in (response.text, refused.text):
                assert SECRET not in value and CHAT_SECRET not in value and "invalid-key" not in value
            # Cleanup/read remain available if configuration becomes unavailable during a call.
            assert (
                await client.post(f"/api/live/sessions/{SESSION}/close", json={}, headers=headers)
            ).status_code == 200
        assert services[0].shutdown_calls == 1

    asyncio.run(check())


def test_live_routes_pass_exact_contract_and_lifespan_owns_service(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        assert not services
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            service = services[0]
            assert app.state.live is service and service.loop is asyncio.get_running_loop()
            discover = await client.get("/api/live", headers=headers)
            assert discover.json() == {
                "available": True,
                "reason": "",
                "provider": "openai",
                "model": "gpt-live-1",
                "backend_model": LiveConfig().backend_model,
                "voice": "marin",
                "voices": list(LIVE_VOICES),
                "active_session_id": "",
            }
            assert discover.headers["cache-control"] == "no-store"
            assert discover.headers["permissions-policy"] == "microphone=(self), camera=()"
            for voice in ("", "cedar"):
                body = {"sdp": OFFER, **({"voice": voice} if voice else {})}
                response = await client.post("/api/live/sessions", json=body, headers=headers)
                assert response.status_code == 201
                assert response.json() == {
                    "session_id": SESSION,
                    "sdp": "fixture-answer",
                    "model": "gpt-live-1",
                    "voice": voice or "marin",
                }
                assert (await client.get("/api/live", headers=headers)).json()["active_session_id"] == SESSION
                for query, after in (("", 0), ("?after=0", 0), ("?after=37", 37)):
                    snapshot = await client.get(f"/api/live/sessions/{SESSION}{query}", headers=headers)
                    assert snapshot.status_code == 200 and snapshot.json()["cursor"] == 38
                    assert snapshot.json()["events"][0]["text"] == "Hello."
                    assert service.snapshots[-1] == (SESSION, after)
                ended = await client.post(f"/api/live/sessions/{SESSION}/close", json={}, headers=headers)
                assert ended.status_code == 200 and ended.json() == {"session_id": SESSION, "status": "closed"}
            assert service.creates == [(OFFER, ""), (OFFER, "cedar")] and service.closes == [SESSION, SESSION]
            assert service.shutdown_calls == 0
            # The factory captures startup Live configuration, not mutable chat settings.
            harness = harnesses[0]
            harness.config.model = "changed-chat-model"
            harness.config.live_voice = "cedar"
            agent = service.factory("")
            try:
                assert agent is not harness.agent and agent.session is not harness.agent.session
                assert agent.provider.model == "gpt-live-1"
                assert agent.provider.live_config is not None and agent.provider.live_config.voice == "marin"
            finally:
                await agent.close()
        assert service.shutdown_calls == 1 and service.active_session_id == ""

    asyncio.run(check())


def test_live_discovery_with_real_service_requires_no_provider_connection(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            response = await client.get("/api/live", headers=headers)
            assert response.status_code == 200
            assert response.json()["available"] is True
            assert response.json()["active_session_id"] == ""
            assert (await client.get("/api/live/sessions/unknown?after=0", headers=headers)).status_code == 404

    asyncio.run(check())


def test_all_live_routes_retain_token_host_origin_and_fetch_metadata_guards(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            for method, path in (
                ("GET", "/api/live"),
                ("POST", "/api/live/sessions"),
                ("GET", f"/api/live/sessions/{SESSION}?after=0"),
                ("POST", f"/api/live/sessions/{SESSION}/close"),
            ):
                for changed in (
                    {},
                    {"Origin": URL},
                    {**headers, "X-Ngn-Token": "wrong"},
                    {**headers, "Host": "untrusted.example"},
                    {**headers, "Origin": "http://localhost:8765"},
                    {**headers, "Origin": "null"},
                    {**headers, "Sec-Fetch-Site": "cross-site"},
                ):
                    response = await client.request(method, path, headers=changed, json={"sdp": OFFER})
                    assert response.status_code == 403
                if method == "POST":
                    response = await client.post(path, headers={"X-Ngn-Token": headers["X-Ngn-Token"]}, json={})
                    assert response.status_code == 403
            assert not services[0].creates and not services[0].snapshots and not services[0].closes

    asyncio.run(check())


def test_live_inputs_are_strict_bounded_and_validation_never_echoes_values(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            bodies: list[object] = [
                {},
                [],
                {"sdp": 123},
                {"sdp": None},
                {"sdp": ""},
                {"sdp": " \r\n "},
                {"sdp": "a" * (MAX_SDP_CHARACTERS + 1)},
                {"sdp": OFFER, "voice": None},
                {"sdp": OFFER, "voice": True},
                {"sdp": OFFER, "voice": SECRET},
                {"sdp": OFFER, "voice": "a" * 65},
                {"sdp": OFFER, "api_key": SECRET},
                {"sdp": OFFER, "base_url": f"https://{SECRET}.invalid"},
            ]
            for body in bodies:
                response = await client.post("/api/live/sessions", json=body, headers=headers)
                assert response.status_code == 422 and SECRET not in response.text
            for content, content_type, expected in (
                (f'{{"sdp":"{SECRET}",', "application/json", 422),
                (OFFER, "application/sdp", 415),
                ("x" * 65537, "application/json", 413),
            ):
                response = await client.post(
                    "/api/live/sessions", content=content, headers={**headers, "Content-Type": content_type}
                )
                assert response.status_code == expected and SECRET not in response.text
            assert not services[0].creates

    asyncio.run(check())


def test_live_cursor_exception_accepts_only_one_bounded_nonsecret_query(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            for query in (
                "after=-1",
                "after=1.5",
                "after=",
                "after=true",
                "after=0&after=1",
                "after=0&token=secret",
                "token=secret",
                "after=" + "9" * 17,
            ):
                response = await client.get(f"/api/live/sessions/{SESSION}?{query}", headers=headers)
                assert response.status_code == 400
            assert (await client.get(f"/api/live/sessions/{SESSION}?after={2**53}", headers=headers)).status_code == 422
            for path in ("/api/live", "/api/bootstrap", "/api/sessions", f"/api/live/sessions/{SESSION}/close"):
                assert (await client.get(f"{path}?after=0", headers=headers)).status_code == 400
            assert (
                await client.post(f"/api/live/sessions/{SESSION}?after=0", json={}, headers=headers)
            ).status_code == 400
            for invalid in ("x" * 129, "invalid.id"):
                assert (await client.get(f"/api/live/sessions/{invalid}", headers=headers)).status_code == 422
            assert not services[0].snapshots
            assert (
                await client.get(f"/api/live/sessions/{SESSION}?after={2**53 - 1}", headers=headers)
            ).status_code == 200
            assert services[0].snapshots == [(SESSION, 2**53 - 1)]

    asyncio.run(check())


def test_safe_service_errors_and_unexpected_errors_do_not_return_credentials(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with (
            client_app(tmp_path, config=configuration(tmp_path)) as (app, _, headers, _),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=URL
            ) as client,
        ):
            for failure, status in (
                (HTTPException(409, "End the active Live session first."), 409),
                (RuntimeError(f"upstream failed: {SECRET} {CHAT_SECRET}"), 500),
            ):
                with patch.object(services[0], "create", AsyncMock(side_effect=failure)):
                    response = await client.post("/api/live/sessions", json={"sdp": OFFER}, headers=headers)
                    assert response.status_code == status
                    assert SECRET not in response.text and CHAT_SECRET not in response.text
                    assert response.headers["cache-control"] == "no-store"
            assert (await client.get("/api/live/sessions/unknown", headers=headers)).status_code == 404

    asyncio.run(check())


def test_partial_startup_shuts_down_live_service(tmp_path: Path, services: list[FakeLiveService]) -> None:
    async def check() -> None:
        with (
            patch.object(ControlledHarness, "initialize", AsyncMock(side_effect=RuntimeError("fixture startup"))),
            pytest.raises(RuntimeError, match="fixture startup"),
        ):
            async with client_app(tmp_path, config=configuration(tmp_path)):
                pytest.fail("Startup should not yield")
        assert len(services) == 1 and services[0].shutdown_calls == 1

    asyncio.run(check())


def test_live_shutdown_error_still_closes_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        with pytest.raises(RuntimeError, match="fixture cleanup"):
            async with client_app(tmp_path, config=configuration(tmp_path)) as (_, _, _, harnesses):
                harness = harnesses[0]
                monkeypatch.setattr(services[0], "shutdown", AsyncMock(side_effect=RuntimeError("fixture cleanup")))
        assert harness._closed

    asyncio.run(check())
