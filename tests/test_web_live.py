"""UI-only Live setup and the real serve boundary, using only local provider fixtures."""

from __future__ import annotations

import asyncio
import os
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest
from aiohttp import web
from fastapi import HTTPException
from pydantic import SecretStr

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.live import LiveConfig
from nagents.provider import FoundryProvider
from nagents.provider.openai import CodexCredentials
from nagents.web.live import create_agent
from nagents.web.live import create_login_config
from nagents.web.live_runtime import LiveService
from nagents.web.live_settings import LIVE_PROVIDERS
from nagents.web.live_settings import LIVE_VOICES
from nagents.web.live_settings import LiveConnection
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import LiveValues
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import URL
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.agent import Agent
    from nagents.web.live_handoff import LoginHandoff
    from nagents.web.live_login import LoginVoiceConfig

SECRET = "sk-fixture-live-private-key"
SESSION = "live-fixture-session"


def configuration(path: Path, *, demo: bool = False) -> HarnessConfig:
    providers = ScopedProviderRegistryStore(path)
    if not providers.load().active:
        providers.save_scope(
            ProviderRegistry(
                active="chat",
                providers={
                    "chat": ProviderProfile(kind="anthropic", auth="api-key"),
                    "voice": ProviderProfile(kind="openai", auth="chatgpt"),
                },
            ),
            expected="0" * 64,
            scope="global",
        )
    return HarnessConfig(
        workspace=path,
        data_dir=path / "data",
        provider="chat",
        providers=connection("anthropic", name="chat", auth="api-key"),
        model="chat-only-model",
        demo=demo,
    )


class FakeLiveService:
    def __init__(
        self, factory: Callable[[str], Agent], login_factory: Callable[[str], LoginVoiceConfig | None]
    ) -> None:
        self.factory = factory
        self.login_factory = login_factory
        self.loop = asyncio.get_running_loop()
        self.active_session_id = ""
        self.creates: list[str] = []
        self.snapshots: list[tuple[str, int]] = []
        self.closes: list[str] = []
        self.configs: list[LoginVoiceConfig] = []
        self.delegations: list[dict[str, object]] = []
        self.shutdown_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def create_stream(self, voice: str = "") -> dict[str, object]:
        self.creates.append(voice)
        self.active_session_id = SESSION
        self.entered.set()
        await self.release.wait()

        async def construct() -> LoginVoiceConfig:
            config = self.login_factory(voice)
            assert config is not None
            await config.credentials()
            return config

        config = await asyncio.create_task(construct())
        self.configs.append(config)
        return {"session_id": SESSION, "model": config.model, "voice": config.voice}

    async def snapshot(self, session_id: str, after: int = 0) -> dict[str, object]:
        self.snapshots.append((session_id, after))
        if session_id != SESSION:
            raise HTTPException(404, "Live session not found.")
        return {
            "session_id": session_id,
            "status": "connected",
            "model": "gpt-live-1-codex",
            "voice": "cove",
            "events": [{"seq": 38, "type": "transcript", "speaker": "assistant", "text": "Hello."}],
            "cursor": 38,
        }

    def delegation_reporter(self, session_id: str) -> Callable[[dict[str, object]], None]:
        return self.delegations.append

    async def close(self, session_id: str) -> dict[str, object]:
        self.closes.append(session_id)
        self.active_session_id = ""
        return {"session_id": session_id, "status": "closed"}

    async def shutdown(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.shutdown_calls += 1
        self.active_session_id = ""


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NGN_LIVE_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def chatgpt_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OpenAIAuth, "logged_in", lambda auth: True)

    async def credentials(auth: OpenAIAuth) -> CodexCredentials:
        return CodexCredentials(SECRET, "fixture-account")

    monkeypatch.setattr(OpenAIAuth, "credentials", credentials)


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> list[FakeLiveService]:
    instances: list[FakeLiveService] = []

    def factory(
        agent_factory: Callable[[str], Agent],
        *,
        login_factory: Callable[[str], LoginVoiceConfig | None] | None = None,
        caption_factory: object = None,
        context_factory: object = None,
    ) -> FakeLiveService:
        del caption_factory, context_factory
        assert login_factory is not None
        service = FakeLiveService(agent_factory, login_factory)
        instances.append(service)
        return service

    monkeypatch.setattr("nagents.web.app.LiveService", factory)
    return instances


async def configure(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    before = (await client.get("/api/live/settings?scope=workspace", headers=headers)).json()
    response = await client.post(
        "/api/live/settings",
        headers=headers,
        json={
            "scope": "workspace",
            "revision": before["revision"],
            "overrides": {"enabled": True, "connection_id": "voice", "backend_mode": "assistant"},
        },
    )
    assert response.status_code == 200 and SECRET not in response.text
    return str(response.json()["revision"])


def test_harness_has_no_live_json_or_environment_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not any(item.name.startswith("live_") for item in fields(HarnessConfig))
    monkeypatch.setenv("NGN_LIVE_ENABLED", "true")
    monkeypatch.setenv("NGN_LIVE_PROVIDER", "not-a-provider")
    monkeypatch.setenv("NGN_LIVE_API_KEY_ENV", "OPENAI_API_KEY")
    assert not hasattr(load_config(tmp_path), "live_enabled")
    path = tmp_path / "removed.json"
    path.write_text('{"live_enabled": true}\n')
    with pytest.raises(ValueError, match="Unknown configuration fields"):
        load_config(tmp_path, path)


@pytest.mark.parametrize("provider", LIVE_PROVIDERS)
def test_factory_uses_only_committed_key_and_tool_free_hosted_agent(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-process-key")
    values = LiveValues.model_validate(
        {
            **LiveValues.defaults().model_dump(),
            "enabled": True,
            "backend_mode": "hosted",
            "provider": provider,
            "base_url": "https://voice.example.invalid/openai/v1",
            "model": "voice-deployment",
            "backend_model": "reasoning-deployment",
            "voice": "cedar",
        }
    )

    async def check() -> None:
        connection = LiveConnection(values, "1" * 64, SecretStr(SECRET))
        agent, second = create_agent(connection), create_agent(connection, "marin")
        try:
            assert agent.provider is not second.provider and agent.session is not second.session
            assert agent.provider.api_key == SECRET and os.environ["OPENAI_API_KEY"] == "wrong-process-key"
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
                assert headers == {"Authorization": f"Bearer {SECRET}"}
        finally:
            await agent.close()
            await second.close()

    asyncio.run(check())


def test_factory_configures_voice_as_the_existing_assistants_frontend() -> None:
    async def handle(transcript: str) -> str:
        return "The existing assistant's verified result"

    async def check() -> None:
        values = LiveValues.model_validate(
            {**LiveValues.defaults().model_dump(), "enabled": True, "backend_mode": "assistant"}
        )
        agent = create_agent(LiveConnection(values, "1" * 64, SecretStr(SECRET)), client_handler=handle)
        try:
            config = agent.provider.live_config
            assert config is not None and config.client_handler is handle and config.delegation == "client"
            payload = agent.live_configuration()
            assert payload["delegation"] == {"type": "client"}
            assert isinstance(payload["instructions"], str)
            assert "voice of the assistant in the selected chat" in payload["instructions"]
            assert "chat history, configured provider, tools, and approval rules" in payload["instructions"]
            assert "hosted backend" not in payload["instructions"]
            assert agent.tool_registry.get_all() == [] and agent.session.db_path == Path(":memory:")
        finally:
            await agent.close()

    asyncio.run(check())


def test_login_factory_binds_voice_to_existing_assistant_without_api_key() -> None:
    async def handle(request: LoginHandoff) -> str:
        return "The existing assistant's verified result"

    async def credentials() -> CodexCredentials:
        return CodexCredentials(SECRET, "fixture-account")

    values = LiveValues.model_validate({**LiveValues.defaults().model_dump(), "enabled": True})
    connection = LiveConnection(
        values, "1" * 64, SecretStr(""), credential_available=True, voice_auth="chatgpt", login_credentials=credentials
    )
    config = create_login_config(connection, "ember", handle)
    assert config.handler is handle and config.credentials is credentials and config.voice == "ember"
    assert config.model == "gpt-live-1-codex"
    assert "voice of the assistant in the selected chat" in config.instructions
    assert "chat history, configured provider, tools, and approval rules" in config.instructions


def test_setup_and_calls_work_without_environment_and_persist_after_restart(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            assert not os.environ.get("OPENAI_API_KEY")
            settings: LiveSettings = app.state.live_settings
            initial = (await client.get("/api/live", headers=headers)).json()
            assert initial["enabled"] is False and initial["available"] is False and not initial["key_configured"]
            assert initial["model"] == "gpt-live-1" and initial["backend_model"] == LiveConfig().backend_model
            assert initial["voices"] == list(LIVE_VOICES) and "Voice settings" in initial["reason"]
            revision = await configure(client, headers)
            info = await client.get("/api/live", headers=headers)
            assert info.json()["available"] is True and info.json()["reason"] == ""
            assert info.json()["revision"] == revision and info.json()["key_configured"] is True
            assert info.headers["cache-control"] == "no-store"
            assert info.headers["permissions-policy"] == "microphone=(self), camera=()"
            before_history = await harnesses[0].history()
            for voice in ("", "ember"):
                response = await client.post(
                    "/api/live/sessions",
                    headers=headers,
                    json={"voice": voice, "revision": revision, "session_id": harnesses[0].session_id},
                )
                assert response.status_code == 201 and response.json() == {
                    "session_id": SESSION,
                    "model": "gpt-live-1-codex",
                    "voice": voice or "sol",
                }
                assert (await client.get("/api/live", headers=headers)).json()["active_session_id"] == SESSION
                for query, after in (("", 0), ("?after=0", 0), ("?after=37", 37)):
                    snapshot = await client.get(f"/api/live/sessions/{SESSION}{query}", headers=headers)
                    assert snapshot.status_code == 200 and snapshot.json()["cursor"] == 38
                    assert services[0].snapshots[-1] == (SESSION, after)
                ended = await client.post(f"/api/live/sessions/{SESSION}/close", json={}, headers=headers)
                assert ended.status_code == 200 and ended.json()["status"] == "closed"
            assert (
                await harnesses[0].history() == before_history
                and harnesses[0].config.provider_profile().kind == "anthropic"
            )
            assert len(services[0].configs) == 2
            assert not os.environ.get("OPENAI_API_KEY")
            with pytest.raises(HTTPException):
                settings.admitted()
        assert services[0].shutdown_calls == 1 and settings._closed
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            restored = (await client.get("/api/live", headers=headers)).json()
            assert restored["available"] is True and restored["revision"] == revision
            assert restored["active_session_id"] == ""
            assert SECRET not in str(restored)

    asyncio.run(check())


def test_main_assistant_mode_binds_selected_chat_and_exposes_current_provider(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            before = (await client.get("/api/live/settings", headers=headers)).json()
            assert before["values"]["backend_mode"] == "assistant"
            revision = await configure(client, headers)
            ready = (await client.get("/api/live", headers=headers)).json()
            assert ready["backend_mode"] == "assistant"
            assert ready["assistant"] == {"agent": "assistant", "provider": "anthropic", "model": "chat-only-model"}
            assert ready["available"] is True and SECRET not in str(ready)
            missing = await client.post("/api/live/sessions", headers=headers, json={"revision": revision})
            assert missing.status_code == 422
            wrong = await client.post(
                "/api/live/sessions",
                headers=headers,
                json={
                    "revision": revision,
                    "session_id": "ngn-another-chat",
                },
            )
            assert wrong.status_code == 409 and not services[0].creates
            selected = harnesses[0].session_id
            with patch.object(app.state.web.designed_channels, "pin", AsyncMock(return_value=("designed", "source"))):
                pinned = await client.post(
                    "/api/live/sessions",
                    headers=headers,
                    json={
                        "revision": revision,
                        "session_id": selected,
                    },
                )
                assert pinned.status_code == 409 and not services[0].creates
            started = await client.post(
                "/api/live/sessions",
                headers=headers,
                json={
                    "revision": revision,
                    "session_id": selected,
                },
            )
            assert started.status_code == 201
            assert services[0].configs[0].handler is not None
            assert harnesses[0].agent.tool_registry.get("shell") is not None
            await client.post(f"/api/live/sessions/{SESSION}/close", headers=headers, json={})

    asyncio.run(check())


@pytest.mark.parametrize("provider", LIVE_PROVIDERS)
def test_settings_routes_create_real_provider_websockets_after_reload(tmp_path: Path, provider: str) -> None:
    """Only the remote provider is a fixture: app, settings, runtime, Agent and transports are real."""

    async def check() -> None:
        started: list[dict[str, object]] = []
        commands: list[tuple[str, str]] = []
        api_path = "/openai/v1" if provider == "azure_openai_compatible_v1" else "/v1"

        async def connect(request: web.Request) -> web.WebSocketResponse:
            if provider == "azure_openai_compatible_v1":
                assert request.headers["api-key"] == SECRET and "Authorization" not in request.headers
            else:
                assert request.headers["Authorization"] == f"Bearer {SECRET}"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            first = await socket.receive_json()
            assert first["type"] == "session.start"
            started.append(cast("dict[str, object]", first["session"]))
            identifier = f"native-{len(started)}"
            await socket.send_json({"type": "session.started", "session": {"id": identifier}})
            await socket.send_json(
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Hello",
                    "start_ms": 0,
                    "end_ms": 10,
                }
            )
            await socket.send_json(
                {
                    "type": "session.output_transcript.delta",
                    "delta": f"Hi {SECRET}",
                    "start_ms": 10,
                    "end_ms": 20,
                }
            )
            async for message in socket:
                event = message.json()
                kind = str(event["type"])
                commands.append((identifier, kind))
                if kind == "session.close":
                    await socket.send_json(
                        {"type": "session.closed", "usage": {"seconds": 1}, "reason": "client_close"}
                    )
                    break
            return socket

        upstream = web.Application()
        upstream.router.add_get(f"{api_path}/live/sessions", connect)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            host = f"http://127.0.0.1:{runner.addresses[0][1]}"
            # Foundry appends /v1; this exercises the configured prefix without patching endpoints.
            base_url = host + ("/openai" if provider == "azure_openai_compatible_v1" else "/v1")
            revision = ""
            session_ids: list[str] = []
            for restart in (False, True):
                async with client_app(
                    tmp_path,
                    config=HarnessConfig(
                        workspace=tmp_path,
                        data_dir=tmp_path / "data",
                        providers=connection("anthropic", auth="api-key"),
                        model="chat-only-model",
                    ),
                ) as (app, client, headers, harnesses):
                    assert not os.environ.get("OPENAI_API_KEY")
                    service: LiveService = app.state.live
                    assert isinstance(service, LiveService)
                    saved = (await client.get("/api/live/settings", headers=headers)).json()
                    if not restart:
                        response = await client.post(
                            "/api/live/settings",
                            headers=headers,
                            json={
                                "revision": saved["revision"],
                                "values": {
                                    **saved["values"],
                                    "enabled": True,
                                    "provider": provider,
                                    "base_url": base_url,
                                    "model": "voice-ui-model",
                                    "backend_model": "hosted-ui-model",
                                    "backend_mode": "hosted",
                                    "voice": "cedar",
                                },
                                "api_key": SECRET,
                            },
                        )
                        assert response.status_code == 200 and SECRET not in response.text
                        saved = response.json()
                        revision = saved["revision"]
                    assert saved["revision"] == revision and saved["key_configured"] is True
                    assert saved["values"]["base_url"] == base_url and SECRET not in str(saved)
                    info = (await client.get("/api/live", headers=headers)).json()
                    assert info["available"] and info["provider"] == provider and info["active_session_id"] == ""
                    before_history = await harnesses[0].history()
                    async with asyncio.timeout(HANG_GUARD):
                        created = await client.post("/api/live/sessions", headers=headers, json={"revision": revision})
                    assert created.status_code == 201 and SECRET not in created.text
                    identifier = str(created.json()["session_id"])
                    session_ids.append(identifier)
                    assert {key: created.json()[key] for key in ("session_id", "model", "voice")} == {
                        "session_id": identifier,
                        "model": "voice-ui-model",
                        "voice": "cedar",
                    }
                    assert created.json()["context"]["mode"] == "none"
                    assert created.json()["context"]["bytes"] == 0
                    assert created.json()["context"]["chat_session_id"] == harnesses[0].session_id
                    assert identifier not in [f"native-{i}" for i in range(1, len(started) + 1)]
                    assert (await client.get("/api/live", headers=headers)).json()["active_session_id"] == identifier
                    async with asyncio.timeout(HANG_GUARD):
                        while True:
                            response = await client.get(f"/api/live/sessions/{identifier}?after=0", headers=headers)
                            assert response.status_code == 200 and SECRET not in response.text
                            snapshot = response.json()
                            captions = [event for event in snapshot["events"] if event["type"] == "transcript"]
                            if len(captions) == 2:
                                break
                            await asyncio.sleep(0.001)
                    assert snapshot["status"] == "connected"
                    assert [(event["speaker"], event["text"]) for event in captions] == [
                        ("user", "Hello"),
                        ("assistant", "Hi [redacted]"),
                    ]
                    blocked = await client.post(
                        "/api/live/settings", headers=headers, json={"revision": revision, "values": saved["values"]}
                    )
                    assert blocked.status_code == 409
                    if not restart:
                        async with asyncio.timeout(HANG_GUARD):
                            closed = await client.post(
                                f"/api/live/sessions/{identifier}/close", headers=headers, json={}
                            )
                        assert closed.status_code == 200 and closed.json()["status"] == "closed"
                        assert not service.active_session_id
                    # The second call is left active to exercise actual app shutdown ownership.
                    assert await harnesses[0].history() == before_history
                    assert harnesses[0].config.provider_profile().kind == "anthropic"
                assert service._closed and not service.active_session_id
            assert len(set(session_ids)) == 2
            assert commands == [("native-1", "session.close"), ("native-2", "session.close")]
            assert len(started) == 2
            for session in started:
                assert isinstance(session, dict)
                assert session["model"] == "voice-ui-model" and session["store"] is False and session["input"] == []
                assert session["audio"] == {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "output": {"voice": "cedar"},
                }
                assert session["delegation"] == {
                    "type": "responses",
                    "responses": {
                        "model": "hosted-ui-model",
                        "instructions": LiveConfig().backend_instructions,
                        "tools": [],
                        "tool_choice": "auto",
                    },
                }
        finally:
            await runner.cleanup()

    asyncio.run(check())


@pytest.mark.parametrize("demo", [False, True])
def test_unavailable_setup_and_demo_remain_configurable(
    tmp_path: Path, services: list[FakeLiveService], demo: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, demo=demo)) as (_, client, headers, harnesses):
            monkeypatch.setattr(OpenAIAuth, "logged_in", lambda auth: False)
            revision = await configure(client, headers)
            info = (await client.get("/api/live", headers=headers)).json()
            assert not info["available"] and not info["key_configured"]
            assert ("demo" if demo else "Sign in") in info["reason"]
            refused = await client.post(
                "/api/live/sessions",
                json={"revision": revision, "session_id": harnesses[0].session_id},
                headers=headers,
            )
            assert refused.status_code == 503
            monkeypatch.setattr(OpenAIAuth, "logged_in", lambda auth: True)
            revision = await configure(client, headers)
            assert (await client.get("/api/live", headers=headers)).json()["available"] is (not demo)
            if demo:
                refused = await client.post(
                    "/api/live/sessions",
                    json={"revision": revision, "session_id": harnesses[0].session_id},
                    headers=headers,
                )
                assert refused.status_code == 503 and "demo" in refused.text
            assert not services[0].creates

    asyncio.run(check())


def test_stale_revisions_and_active_or_provisioning_calls_reject_settings_changes(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            stale = (await client.get("/api/live", headers=headers)).json()["revision"]
            revision = await configure(client, headers)
            for path, body in (
                ("/api/live/sessions", {"revision": stale}),
                (
                    "/api/live/settings",
                    {"scope": "workspace", "revision": stale, "overrides": {"enabled": True}},
                ),
            ):
                response = await client.post(path, headers=headers, json=body)
                assert response.status_code == 409 and SECRET not in response.text
            assert not services[0].creates
            current = (await client.get("/api/live/settings", headers=headers)).json()
            service = services[0]
            service.release.clear()
            creating = asyncio.create_task(
                client.post(
                    "/api/live/sessions",
                    headers=headers,
                    json={"revision": revision, "session_id": harnesses[0].session_id},
                )
            )
            await service.entered.wait()
            try:
                assert (
                    await client.post(
                        "/api/live/settings",
                        headers=headers,
                        json={"scope": "workspace", "revision": revision, "overrides": current["overrides"]},
                    )
                ).status_code == 409
                assert (
                    await client.post(
                        "/api/live/sessions",
                        headers=headers,
                        json={"revision": revision, "session_id": harnesses[0].session_id},
                    )
                ).status_code == 409
            finally:
                service.release.set()
            assert (await creating).status_code == 201
            assert (
                await client.post(
                    "/api/live/settings",
                    headers=headers,
                    json={"scope": "workspace", "revision": revision, "overrides": current["overrides"]},
                )
            ).status_code == 409
            assert len(service.configs) == 1
            await client.post(f"/api/live/sessions/{SESSION}/close", headers=headers, json={})
            assert (
                await client.post(
                    "/api/live/settings",
                    headers=headers,
                    json={"scope": "workspace", "revision": revision, "overrides": current["overrides"]},
                )
            ).status_code == 200

    asyncio.run(check())


def test_all_live_routes_retain_auth_origin_and_fetch_guards(tmp_path: Path, services: list[FakeLiveService]) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            for method, path in (
                ("GET", "/api/live"),
                ("GET", "/api/live/settings"),
                ("POST", "/api/live/settings"),
                ("POST", "/api/live/sessions"),
                ("GET", f"/api/live/sessions/{SESSION}?after=0"),
                ("GET", f"/api/live/sessions/{SESSION}/delegations/task"),
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
                    assert (await client.request(method, path, headers=changed, json={})).status_code == 403
                if method == "POST":
                    assert (
                        await client.post(path, headers={"X-Ngn-Token": headers["X-Ngn-Token"]}, json={})
                    ).status_code == 403
            assert not services[0].creates and not services[0].snapshots and not services[0].closes

    asyncio.run(check())


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("provider", "anthropic"),
        ("model", ""),
        ("model", "x" * 129),
        ("backend_model", "has spaces"),
        ("voice", "unknown"),
        ("base_url", "http://remote.invalid/v1"),
        ("base_url", f"https://user:{SECRET}@voice.invalid/v1"),
        ("base_url", f"https://voice.invalid/v1?key={SECRET}"),
        ("base_url", "https://voice.invalid/v1/live/sessions"),
        ("base_url", "https://voice.invalid:wrong/v1"),
    ],
)
def test_invalid_settings_never_echo_inputs(tmp_path: Path, field: str, value: object) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            before = (await client.get("/api/live/settings", headers=headers)).json()
            response = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "revision": before["revision"],
                    "values": {**before["values"], field: value},
                    "api_key": SECRET,
                },
            )
            assert response.status_code == 422 and SECRET not in response.text
            assert (await client.get("/api/live/settings", headers=headers)).json() == before

    asyncio.run(check())


def test_strict_bounded_session_settings_and_cursor_inputs(tmp_path: Path, services: list[FakeLiveService]) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            revision = await configure(client, headers)
            valid: dict[str, object] = {"revision": revision}
            body: object
            for body in (
                {},
                [],
                {"voice": "cedar"},
                {**valid, "sdp": "no browser SDP"},
                {**valid, "voice": True},
                {**valid, "voice": SECRET},
                {**valid, "api_key": SECRET},
                {**valid, "revision": SECRET},
            ):
                response = await client.post("/api/live/sessions", json=body, headers=headers)
                assert response.status_code == 422 and SECRET not in response.text
            values = LiveValues.defaults().model_dump()
            for changes in (
                {"api_key": 123},
                {"api_key": "\n" + SECRET},
                {"api_key": "x" * 4097},
                {"api_key": SECRET, "clear_api_key": True},
                {"clear_api_key": "false"},
                {"unknown": SECRET},
                {"values": {**values, "secret": SECRET}},
                {"values": {"enabled": True}},
            ):
                response = await client.post(
                    "/api/live/settings", headers=headers, json={"revision": revision, "values": values, **changes}
                )
                assert response.status_code == 422 and SECRET not in response.text
            for path in ("/api/live/sessions", "/api/live/settings"):
                for content, content_type, status in (
                    (f'{{"secret":"{SECRET}",', "application/json", 422),
                    ("offer", "application/sdp", 415),
                    ("x" * 65537, "application/json", 413),
                ):
                    response = await client.post(
                        path, content=content, headers={**headers, "Content-Type": content_type}
                    )
                    assert response.status_code == status and SECRET not in response.text
            for query in (
                "after=-1",
                "after=1.5",
                "after=",
                "after=true",
                "after=0&after=1",
                "after=0&token=secret",
                "after=" + "9" * 17,
            ):
                assert (await client.get(f"/api/live/sessions/{SESSION}?{query}", headers=headers)).status_code == 400
            assert (await client.get(f"/api/live/sessions/{SESSION}?after={2**53}", headers=headers)).status_code == 422
            for path in ("/api/live", "/api/live/settings", "/api/bootstrap"):
                assert (await client.get(f"{path}?after=0", headers=headers)).status_code == 400
            assert not services[0].creates and not services[0].snapshots

    asyncio.run(check())


def test_safe_runtime_errors_and_unexpected_failures_are_redacted(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with (
            client_app(tmp_path, config=configuration(tmp_path)) as (app, _, headers, harnesses),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=URL
            ) as client,
        ):
            revision = await configure(client, headers)
            for failure, status in (
                (HTTPException(409, "End the active Live session first."), 409),
                (RuntimeError(SECRET), 500),
            ):
                with patch.object(services[0], "create_stream", AsyncMock(side_effect=failure)):
                    response = await client.post(
                        "/api/live/sessions",
                        json={"revision": revision, "session_id": harnesses[0].session_id},
                        headers=headers,
                    )
                    assert response.status_code == status and SECRET not in response.text
                    assert response.headers["cache-control"] == "no-store"
            assert (await client.get("/api/live/sessions/unknown", headers=headers)).status_code == 404

    asyncio.run(check())


@pytest.mark.parametrize("target", [ControlledHarness, LiveSettings])
def test_partial_startup_cleans_up_service(
    tmp_path: Path, services: list[FakeLiveService], target: type[object]
) -> None:
    async def check() -> None:
        with (
            patch.object(
                target,
                "initialize" if target is ControlledHarness else "load",
                AsyncMock(side_effect=RuntimeError("fixture startup")),
            ),
            pytest.raises(RuntimeError, match="fixture startup"),
        ):
            async with client_app(tmp_path, config=configuration(tmp_path)):
                pytest.fail("Startup should not yield")
        assert len(services) == 1 and services[0].shutdown_calls == 1

    asyncio.run(check())


def test_live_shutdown_failure_still_closes_settings_and_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        with pytest.raises(RuntimeError, match="fixture cleanup"):
            async with client_app(tmp_path, config=configuration(tmp_path)) as (app, _, _, harnesses):
                harness = harnesses[0]
                settings: LiveSettings = app.state.live_settings
                monkeypatch.setattr(services[0], "shutdown", AsyncMock(side_effect=RuntimeError("fixture cleanup")))
        assert harness._closed and settings._closed and not settings._tasks

    asyncio.run(check())
