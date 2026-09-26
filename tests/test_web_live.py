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

from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.live import LiveConfig
from nagents.provider import FoundryProvider
from nagents.web.live import MAX_SDP_CHARACTERS
from nagents.web.live import create_agent
from nagents.web.live_runtime import LiveService
from nagents.web.live_settings import LIVE_PROVIDERS
from nagents.web.live_settings import LIVE_VOICES
from nagents.web.live_settings import LiveConnection
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import LiveValues
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import URL
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.agent import Agent

SECRET = "sk-fixture-live-private-key"
SESSION = "live-fixture-session"
OFFER = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def configuration(path: Path, *, demo: bool = False) -> HarnessConfig:
    return HarnessConfig(
        workspace=path, data_dir=path / "data", provider="anthropic", model="chat-only-model", auth="api-key", demo=demo
    )


class FakeLiveService:
    def __init__(self, factory: Callable[[str], Agent]) -> None:
        self.factory = factory
        self.loop = asyncio.get_running_loop()
        self.active_session_id = ""
        self.creates: list[tuple[str, str]] = []
        self.snapshots: list[tuple[str, int]] = []
        self.closes: list[str] = []
        self.agents: list[Agent] = []
        self.shutdown_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def create(self, sdp: str, voice: str = "") -> dict[str, object]:
        self.creates.append((sdp, voice))
        self.active_session_id = SESSION
        self.entered.set()
        await self.release.wait()

        async def construct() -> Agent:
            return self.factory(voice)

        agent = await asyncio.create_task(construct())  # Runtime supervisor inherits admission context.
        self.agents.append(agent)
        options = agent.provider.live_config
        assert options is not None
        return {"session_id": SESSION, "sdp": "fixture-answer", "model": agent.provider.model, "voice": options.voice}

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
        for agent in self.agents:
            await agent.close()
        self.active_session_id = ""
        return {"session_id": session_id, "status": "closed"}

    async def shutdown(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.shutdown_calls += 1
        for agent in self.agents:
            await agent.close()
        self.active_session_id = ""


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NGN_LIVE_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> list[FakeLiveService]:
    instances: list[FakeLiveService] = []

    def factory(agent_factory: Callable[[str], Agent]) -> FakeLiveService:
        service = FakeLiveService(agent_factory)
        instances.append(service)
        return service

    monkeypatch.setattr("nagents.web.app.LiveService", factory)
    return instances


async def configure(client: httpx.AsyncClient, headers: dict[str, str], *, key: str = SECRET) -> str:
    before = (await client.get("/api/live/settings", headers=headers)).json()
    response = await client.post(
        "/api/live/settings",
        headers=headers,
        json={"revision": before["revision"], "values": {**before["values"], "enabled": True}, "api_key": key},
    )
    assert response.status_code == 200 and SECRET not in response.text
    return str(response.json()["revision"])


def test_harness_has_no_live_yaml_or_environment_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not any(item.name.startswith("live_") for item in fields(HarnessConfig))
    monkeypatch.setenv("NGN_LIVE_ENABLED", "true")
    monkeypatch.setenv("NGN_LIVE_PROVIDER", "not-a-provider")
    monkeypatch.setenv("NGN_LIVE_API_KEY_ENV", "OPENAI_API_KEY")
    assert not hasattr(load_config(tmp_path), "live_enabled")
    path = tmp_path / "removed.yaml"
    path.write_text("live_enabled: true\n")
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
            assert initial["voices"] == list(LIVE_VOICES) and "Connection settings" in initial["reason"]
            revision = await configure(client, headers)
            info = await client.get("/api/live", headers=headers)
            assert info.json()["available"] is True and info.json()["reason"] == ""
            assert info.json()["revision"] == revision and info.json()["key_configured"] is True
            assert info.headers["cache-control"] == "no-store"
            assert info.headers["permissions-policy"] == "microphone=(self), camera=()"
            before_history = await harnesses[0].history()
            for voice in ("", "cedar"):
                response = await client.post(
                    "/api/live/sessions", headers=headers, json={"sdp": OFFER, "voice": voice, "revision": revision}
                )
                assert response.status_code == 201 and response.json() == {
                    "session_id": SESSION,
                    "sdp": "fixture-answer",
                    "model": "gpt-live-1",
                    "voice": voice or "marin",
                }
                assert (await client.get("/api/live", headers=headers)).json()["active_session_id"] == SESSION
                for query, after in (("", 0), ("?after=0", 0), ("?after=37", 37)):
                    snapshot = await client.get(f"/api/live/sessions/{SESSION}{query}", headers=headers)
                    assert snapshot.status_code == 200 and snapshot.json()["cursor"] == 38
                    assert services[0].snapshots[-1] == (SESSION, after)
                ended = await client.post(f"/api/live/sessions/{SESSION}/close", json={}, headers=headers)
                assert ended.status_code == 200 and ended.json()["status"] == "closed"
            assert await harnesses[0].history() == before_history and harnesses[0].config.provider == "anthropic"
            assert all(agent is not harnesses[0].agent for agent in services[0].agents)
            assert all(agent.provider.api_key == SECRET for agent in services[0].agents)
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


@pytest.mark.parametrize("provider", LIVE_PROVIDERS)
def test_settings_routes_create_real_provider_sessions_and_sidebands_after_reload(
    tmp_path: Path, provider: str
) -> None:
    """Only the remote provider is a fixture: app, settings, runtime, Agent and transports are real."""

    async def check() -> None:
        provisioned: list[dict[str, object]] = []
        sidebands: list[str] = []
        commands: list[tuple[str, str]] = []
        api_path = "/openai/v1" if provider == "azure_openai_compatible_v1" else "/v1"

        async def provision(request: web.Request) -> web.Response:
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            provisioned.append(cast("dict[str, object]", await request.json()))
            return web.json_response(
                {
                    "session": {"id": f"native-{len(provisioned)}", "client_secret": SECRET},
                    "transport": {"sdp": OFFER},
                }
            )

        async def attach(request: web.Request) -> web.WebSocketResponse:
            identifier = request.match_info["session_id"]
            if provider == "azure_openai_compatible_v1":
                assert request.headers["api-key"] == SECRET and "Authorization" not in request.headers
            else:
                assert request.headers["Authorization"] == f"Bearer {SECRET}"
            sidebands.append(identifier)
            socket = web.WebSocketResponse()
            await socket.prepare(request)
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
        upstream.router.add_post(f"{api_path}/live/sessions", provision)
        upstream.router.add_get(f"{api_path}/live/sessions/{{session_id}}/attach", attach)
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
                async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
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
                        created = await client.post(
                            "/api/live/sessions", headers=headers, json={"sdp": OFFER, "revision": revision}
                        )
                    assert created.status_code == 201 and SECRET not in created.text
                    identifier = str(created.json()["session_id"])
                    session_ids.append(identifier)
                    assert created.json() == {
                        "session_id": identifier,
                        "sdp": OFFER,
                        "model": "voice-ui-model",
                        "voice": "cedar",
                    }
                    assert identifier not in sidebands  # Browser IDs are local opaque handles, not provider IDs.
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
                    assert harnesses[0].config.provider == "anthropic"
                assert service._closed and not service.active_session_id
            assert len(set(session_ids)) == 2 and sidebands == ["native-1", "native-2"]
            assert commands == [("native-1", "session.close"), ("native-2", "session.close")]
            assert len(provisioned) == 2
            for body in provisioned:
                assert body["transport"] == {"type": "webrtc", "sdp": OFFER}
                session = body["session"]
                assert isinstance(session, dict)
                assert session["model"] == "voice-ui-model" and session["store"] is False and session["input"] == []
                assert session["audio"] == {"output": {"voice": "cedar"}}
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
    tmp_path: Path, services: list[FakeLiveService], demo: bool
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, demo=demo)) as (_, client, headers, _):
            revision = await configure(client, headers, key="")
            info = (await client.get("/api/live", headers=headers)).json()
            assert not info["available"] and not info["key_configured"]
            assert ("demo" if demo else "Connection settings") in info["reason"]
            refused = await client.post(
                "/api/live/sessions", json={"sdp": OFFER, "revision": revision}, headers=headers
            )
            assert refused.status_code == 503
            revision = await configure(client, headers)
            assert (await client.get("/api/live", headers=headers)).json()["available"] is (not demo)
            if demo:
                refused = await client.post(
                    "/api/live/sessions", json={"sdp": OFFER, "revision": revision}, headers=headers
                )
                assert refused.status_code == 503 and "demo" in refused.text
            assert not services[0].creates

    asyncio.run(check())


def test_stale_revisions_and_active_or_provisioning_calls_reject_settings_changes(
    tmp_path: Path, services: list[FakeLiveService]
) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            stale = (await client.get("/api/live", headers=headers)).json()["revision"]
            revision = await configure(client, headers)
            for path, body in (
                ("/api/live/sessions", {"sdp": OFFER, "revision": stale}),
                (
                    "/api/live/settings",
                    {"revision": stale, "values": LiveValues.defaults().model_dump(), "api_key": SECRET},
                ),
            ):
                response = await client.post(path, headers=headers, json=body)
                assert response.status_code == 409 and SECRET not in response.text
            assert not services[0].creates
            current = (await client.get("/api/live/settings", headers=headers)).json()
            service = services[0]
            service.release.clear()
            creating = asyncio.create_task(
                client.post("/api/live/sessions", headers=headers, json={"sdp": OFFER, "revision": revision})
            )
            await service.entered.wait()
            try:
                assert (
                    await client.post(
                        "/api/live/settings", headers=headers, json={"revision": revision, "values": current["values"]}
                    )
                ).status_code == 409
                assert (
                    await client.post("/api/live/sessions", headers=headers, json={"sdp": OFFER, "revision": revision})
                ).status_code == 409
            finally:
                service.release.set()
            assert (await creating).status_code == 201
            assert (
                await client.post(
                    "/api/live/settings", headers=headers, json={"revision": revision, "values": current["values"]}
                )
            ).status_code == 409
            assert service.agents[0].provider.api_key == SECRET
            await client.post(f"/api/live/sessions/{SESSION}/close", headers=headers, json={})
            assert (
                await client.post(
                    "/api/live/settings", headers=headers, json={"revision": revision, "values": current["values"]}
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
            valid: dict[str, object] = {"sdp": OFFER, "revision": revision}
            body: object
            for body in (
                {},
                [],
                {"sdp": OFFER},
                {**valid, "sdp": 123},
                {**valid, "sdp": None},
                {**valid, "sdp": " \r\n "},
                {**valid, "sdp": "a" * (MAX_SDP_CHARACTERS + 1)},
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
                    (OFFER, "application/sdp", 415),
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
            client_app(tmp_path, config=configuration(tmp_path)) as (app, _, headers, _),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=URL
            ) as client,
        ):
            revision = await configure(client, headers)
            for failure, status in (
                (HTTPException(409, "End the active Live session first."), 409),
                (RuntimeError(SECRET), 500),
            ):
                with patch.object(services[0], "create", AsyncMock(side_effect=failure)):
                    response = await client.post(
                        "/api/live/sessions", json={"sdp": OFFER, "revision": revision}, headers=headers
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
