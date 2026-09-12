"""Workspace settings API, transactional persistence and portable runtime coverage."""

import asyncio
import builtins
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import aiosqlite
import httpx
import pytest

from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.dictation import VoiceDictation
from nagents.harness.provider import HarnessProvider
from nagents.provider import CodexProvider
from nagents.types import Message
from nagents.web.app import create_app
from nagents.web.settings import SettingsValues
from nagents.web.settings import WebSettings
from tests.test_web import URL
from tests.test_web import ControlledHarness
from tests.test_web import LiveStream
from tests.test_web import client_app
from tests.test_web import no_guarded_workspace_io as no_guarded_workspace_io

LEGACY_FIELDS = (
    "model",
    "agent",
    "shell_timeout",
    "max_output",
    "max_file_bytes",
    "max_tool_rounds",
    "max_subagent_depth",
)
DICTATION_FIELDS = ("dictation_enabled", "dictation_model", "dictation_language", "dictation_max_seconds")


def assert_runtime(harness: Harness, values: dict[str, object]) -> None:
    """Only the original seven preferences are applied to the live chat config."""
    current = SettingsValues.current(harness).model_dump()
    assert {key: current[key] for key in LEGACY_FIELDS} == {key: values[key] for key in LEGACY_FIELDS}


def configuration(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        demo=True,
        profiles={"audit": AgentProfile(mode="reviewer", model="profile-model", instructions="Trusted review rules")},
    )


def test_settings_safe_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_API_KEY", "SECRET-key-value")

    async def check() -> None:
        config = replace(
            configuration(tmp_path),
            demo=False,
            auth="api-key",
            api="responses",
            base_url="https://example.invalid/private-endpoint",
            api_key_env="TEST_API_KEY",
            diagnostics=("SECRET-diagnostic",),
        )
        config.profiles["audit"].instructions = "SECRET-profile-instructions"
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.provider.api_key = "SECRET-provider-value"
            with (
                patch.object(harness, "describe", side_effect=AssertionError("Unsafe projection")),
                patch.object(harness.agent.provider, "verify_model", side_effect=AssertionError("No model lookup")),
                patch.object(harness.agent.provider, "generate", side_effect=AssertionError("No model request")),
            ):
                response = await client.get("/api/settings", headers=headers)
                assert response.status_code == 200
                body = response.json()
                assert set(body) == {
                    "values",
                    "defaults",
                    "profiles",
                    "revision",
                    "persisted",
                    "effective_mode",
                    "connection",
                    "dictation",
                }
                assert body["values"] == body["defaults"] == SettingsValues.current(harness).model_dump()
                assert set(body["values"]) == set(SettingsValues.model_fields) == {*LEGACY_FIELDS, *DICTATION_FIELDS}
                assert body["persisted"] is False and body["effective_mode"] == "build"
                assert body["profiles"] == [
                    {"name": "build", "mode": "build", "model": ""},
                    {"name": "agent", "mode": "build", "model": ""},
                    {"name": "reviewer", "mode": "reviewer", "model": ""},
                    {"name": "audit", "mode": "reviewer", "model": "profile-model"},
                ]
                assert body["connection"] == {
                    "provider": "openai",
                    "api": "responses",
                    "auth_status": harness.auth_status(),
                }
                assert "SECRET" not in response.text and "private-endpoint" not in response.text
                assert str(tmp_path) not in response.text
                assert response.headers["cache-control"] == "no-store"
                saved = await client.post(
                    "/api/settings", json={"revision": body["revision"], "values": body["values"]}, headers=headers
                )
                assert saved.status_code == 200

    asyncio.run(check())


def test_catalog_read_preserves_saved_row_and_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CATALOG_KEY", "fake-api-key")

    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key", api_key_env="TEST_CATALOG_KEY")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            initial = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                json={"revision": initial["revision"], "values": {**initial["values"], "model": "manual-model"}},
                headers=headers,
            )
            assert saved.status_code == 200
            before = saved.json()
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                rows = list(await db.execute_fetchall("SELECT * FROM ngn_web_settings"))
                assert len(rows) == 1
                with patch.object(
                    harness.agent.provider, "get_model_list", AsyncMock(return_value=["different-model"])
                ):
                    response = await client.get("/api/models", headers=headers)
                    assert response.status_code == 200 and response.json()["models"] == ["different-model"]
                assert (await client.get("/api/settings", headers=headers)).json() == before
                assert list(await db.execute_fetchall("SELECT * FROM ngn_web_settings")) == rows
            assert harness.config.model == harness.agent.provider.model == "manual-model"

    asyncio.run(check())


@pytest.mark.parametrize(
    "mode", ["ready", "admin-disabled", "preference-disabled", "demo", "missing-key", "invalid-key", "invalid-endpoint"]
)
def test_dictation_projection_is_local_safe_and_separate_from_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("TEST_CHAT_KEY", "SECRET-chat-key")
    monkeypatch.setenv("TEST_VOICE_KEY", "SECRET-voice-key")
    if mode == "missing-key":
        monkeypatch.delenv("TEST_VOICE_KEY")
    elif mode == "invalid-key":
        monkeypatch.setenv("TEST_VOICE_KEY", "SECRET-invalid\nkey")

    async def check() -> None:
        config = replace(
            configuration(tmp_path),
            demo=mode == "demo",
            auth="chatgpt",
            api_key_env="TEST_CHAT_KEY",
            dictation_enabled=mode != "admin-disabled",
            dictation_api_key_env="TEST_VOICE_KEY",
            dictation_base_url=(
                "https://example.invalid:bad/private-endpoint"
                if mode == "invalid-endpoint"
                else "https://api.openai.com/v1"
            ),
        )
        with (
            patch("builtins.__import__", wraps=builtins.__import__) as imports,
            patch("aiohttp.ClientSession", side_effect=AssertionError("No network resources")),
            patch.object(VoiceDictation, "capture", side_effect=AssertionError("No microphone")),
            patch.object(VoiceDictation, "transcribe", side_effect=AssertionError("No transcription")),
        ):
            async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
                harness = harnesses[0]
                provider = harness.agent.provider
                if not config.demo:
                    assert isinstance(provider, CodexProvider)
                before = (await client.get("/api/settings", headers=headers)).json()
                values = {
                    **before["values"],
                    "dictation_enabled": mode != "preference-disabled",
                    "dictation_model": "  selected-transcriber  ",
                    "dictation_language": "en",
                    "dictation_max_seconds": 300,
                }
                saved = await client.post(
                    "/api/settings", json={"revision": before["revision"], "values": values}, headers=headers
                )
                assert saved.status_code == 200
                body = saved.json()
                projection = body["dictation"]
                assert projection == {
                    "enabled": mode not in {"admin-disabled", "preference-disabled", "demo"},
                    "available": mode == "ready",
                    "admin_enabled": config.dictation_enabled,
                    "status": projection["status"],
                    "api_key_env": "TEST_VOICE_KEY",
                    "max_seconds": 120,
                    "max_bytes": 120 * 32000 + 4096,
                    "sample_rate": 16000,
                    "channels": 1,
                    "sample_width": 2,
                    "content_type": "audio/wav",
                    "revision": body["revision"],
                }
                expected_status = {
                    "ready": "ready",
                    "admin-disabled": "administrator",
                    "preference-disabled": "web settings",
                    "demo": "demo mode",
                    "missing-key": "Set TEST_VOICE_KEY",
                    "invalid-key": "Set TEST_VOICE_KEY",
                    "invalid-endpoint": "Set dictation_base_url",
                }
                assert expected_status[mode] in projection["status"]
                assert body["values"]["dictation_model"] == "selected-transcriber"
                assert body["connection"] == before["connection"]
                assert harness.agent.provider is provider
                assert provider.model == before["values"]["model"]
                assert {key: getattr(harness.config, key) for key in DICTATION_FIELDS} == {
                    key: getattr(config, key) for key in DICTATION_FIELDS
                }
                assert "SECRET" not in saved.text and "private-endpoint" not in saved.text
                assert "https://" not in saved.text
                # Voice setup problems also leave unrelated chat settings writable.
                response = await client.post(
                    "/api/settings",
                    json={"revision": body["revision"], "values": {**body["values"], "max_tool_rounds": 5}},
                    headers=headers,
                )
                assert response.status_code == 200 and harness.agent.max_tool_rounds == 5
            assert all(call.args[0].split(".")[0] != "sounddevice" for call in imports.call_args_list)

    asyncio.run(check())


def test_dictation_config_is_detached_and_preserves_startup_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_VOICE_KEY", "synthetic-voice-key")

    async def check() -> None:
        config = replace(
            configuration(tmp_path),
            demo=False,
            auth="api-key",
            api_key_env="TEST_CHAT_KEY",
            dictation_enabled=True,
            dictation_api_key_env="TEST_VOICE_KEY",
            dictation_base_url="https://example.invalid/trusted-transcription",
            dictation_max_seconds=60,
        )
        harness = ControlledHarness(config)
        try:
            await harness.initialize()
            settings = WebSettings(harness)
            await settings.load()
            values = SettingsValues.model_validate(
                {
                    **settings.values.model_dump(),
                    "dictation_model": "preferred-transcriber",
                    "dictation_language": "de",
                    "dictation_max_seconds": 1,
                }
            )
            await settings.change(settings.revision, values)
            detached = settings.dictation_config()
            assert isinstance(detached, HarnessConfig) and detached is not config
            assert detached.dictation_enabled and not detached.demo
            assert detached.dictation_model == "preferred-transcriber" and detached.dictation_language == "de"
            assert detached.dictation_max_seconds == 1
            assert detached.dictation_base_url == "https://example.invalid/trusted-transcription"
            assert detached.dictation_api_key_env == "TEST_VOICE_KEY"
            before = settings.dictation_snapshot()
            assert before["available"] is True and before["max_bytes"] == 32000 + 4096
            # Neither a consumer's snapshot nor mutable live chat config can alter
            # the captured admin inputs, including nested configuration objects.
            detached.profiles["audit"].instructions = "mutated snapshot"
            detached.dictation_enabled = False
            detached.dictation_max_seconds = 300
            detached.dictation_base_url = "https://snapshot.invalid/v1"
            detached.dictation_api_key_env = "SNAPSHOT_KEY"
            detached.demo = True
            config.dictation_enabled = False
            config.dictation_max_seconds = 300
            config.dictation_base_url = "https://live.invalid/v1"
            config.dictation_api_key_env = "LIVE_KEY"
            config.demo = True
            fresh = settings.dictation_config()
            assert fresh.dictation_enabled and not fresh.demo
            assert fresh.dictation_base_url == "https://example.invalid/trusted-transcription"
            assert fresh.dictation_api_key_env == "TEST_VOICE_KEY"
            assert fresh.profiles["audit"].instructions == "Trusted review rules"
            assert config.profiles["audit"].instructions == "Trusted review rules"
            assert settings.dictation_snapshot() == before
            assert settings.snapshot()["dictation"] == before
        finally:
            await harness.close()

    asyncio.run(check())


@pytest.mark.parametrize("field", [*LEGACY_FIELDS, *DICTATION_FIELDS])
def test_settings_require_all_eleven_fields(tmp_path: Path, field: str) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            before = (await client.get("/api/settings", headers=headers)).json()
            values = dict(before["values"])
            del values[field]
            response = await client.post(
                "/api/settings", json={"revision": before["revision"], "values": values}, headers=headers
            )
            assert response.status_code == 422
            assert (await client.get("/api/settings", headers=headers)).json() == before

    asyncio.run(check())


def test_settings_runtime_profile_model_limits_and_identity(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            before = (await client.get("/api/settings", headers=headers)).json()
            config, provider = harness.config, harness.agent.provider
            registry, executor, approval = (
                harness.agent.tool_registry,
                harness.agent.tool_executor,
                harness.approval_handler,
            )
            session_id = harness.session_id
            harness.tools.read_hashes["kept.txt"] = "old-read"
            await harness.agent.session.add_message(session_id, Message(role="user", content="keep history"))
            old_child = harness.tasks._create_child("agent")
            values = {
                **before["values"],
                "model": "  vendor/free-text:model-v2  ",
                "agent": "audit",
                "shell_timeout": 600,
                "max_output": 1048576,
                "max_file_bytes": 4194304,
                "max_tool_rounds": 1000,
                "max_subagent_depth": 0,
            }
            response = await client.post(
                "/api/settings", json={"revision": before["revision"], "values": values}, headers=headers
            )
            assert response.status_code == 200
            saved = response.json()
            values["model"] = "vendor/free-text:model-v2"
            assert saved["values"] == values and saved["defaults"] == before["defaults"]
            assert saved["persisted"] is True and saved["revision"] != before["revision"]
            assert saved["effective_mode"] == "reviewer"
            assert SettingsValues.current(harness).model_dump() == values
            assert harness.config is config and harness.agent.provider is provider
            assert isinstance(provider, HarnessProvider) and provider.harness_config is config
            assert harness.agent.tool_registry is registry and harness.agent.tool_executor is executor
            assert harness.approval_handler is approval and harness.session_id == session_id
            assert harness.tools.read_hashes == {"kept.txt": "old-read"}
            assert (await harness.history())[0].content == "keep history"
            assert harness.agent.max_tool_rounds == 1000 and not harness.can_delegate
            assert "Trusted review rules" in (harness.agent.system_prompt or "")
            assert "cannot delegate" in (harness.agent.system_prompt or "")
            with pytest.raises(PermissionError):
                harness.tools.writable()
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["model"] == values["model"] and bootstrap["agent"] == "audit"
            new_child = harness.tasks._create_child("agent")
            assert new_child.agent.provider.model == values["model"]
            assert old_child.agent.provider.model == before["values"]["model"]
            await old_child.close()
            await new_child.close()
            # Aliases do not erase the explicitly selected model or permission ceiling.
            harness._permission_ceiling = "reviewer"
            values.update(agent="agent", max_subagent_depth=8)
            response = await client.post(
                "/api/settings", json={"revision": saved["revision"], "values": values}, headers=headers
            )
            assert response.status_code == 200 and response.json()["effective_mode"] == "reviewer"
            assert harness.can_delegate and "depth 8" in (harness.agent.system_prompt or "")
            await client.post("/api/sessions/new", json={}, headers=headers)
            assert (await client.get("/api/settings", headers=headers)).json()["values"] == values

    asyncio.run(check())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", ""),
        ("model", "   "),
        ("model", "x" * 201),
        ("model", "bad\x00model"),
        ("model", "bad\nmodel"),
        ("model", "bad\x7fmodel"),
        ("model", "bad\u202emodel"),
        ("model", 12),
        ("agent", "missing-SECRET-profile"),
        ("agent", True),
        ("shell_timeout", 0),
        ("shell_timeout", -1),
        ("shell_timeout", 600.01),
        ("shell_timeout", "60"),
        ("shell_timeout", True),
        ("shell_timeout", float("nan")),
        ("shell_timeout", float("inf")),
        ("shell_timeout", -float("inf")),
        ("max_output", 1023),
        ("max_output", 1048577),
        ("max_output", True),
        ("max_output", 2048.0),
        ("max_output", "2048"),
        ("max_file_bytes", 1023),
        ("max_file_bytes", 4194305),
        ("max_file_bytes", False),
        ("max_file_bytes", 2048.5),
        ("max_tool_rounds", 0),
        ("max_tool_rounds", 1001),
        ("max_tool_rounds", True),
        ("max_tool_rounds", 2.0),
        ("max_subagent_depth", -1),
        ("max_subagent_depth", 9),
        ("max_subagent_depth", False),
        ("max_subagent_depth", 2.0),
        ("dictation_enabled", 1),
        ("dictation_enabled", "true"),
        ("dictation_enabled", None),
        ("dictation_model", ""),
        ("dictation_model", "   "),
        ("dictation_model", "x" * 201),
        ("dictation_model", "bad\x00model"),
        ("dictation_model", "bad\nmodel"),
        ("dictation_model", "bad\x7fmodel"),
        ("dictation_model", "bad\u202emodel"),
        ("dictation_model", 12),
        ("dictation_language", "EN"),
        ("dictation_language", "english"),
        ("dictation_language", "en-US"),
        ("dictation_language", "en\n"),
        ("dictation_language", " en "),
        ("dictation_language", " "),
        ("dictation_language", "éé"),
        ("dictation_language", True),
        ("dictation_max_seconds", 0),
        ("dictation_max_seconds", 301),
        ("dictation_max_seconds", True),
        ("dictation_max_seconds", 120.0),
        ("dictation_max_seconds", "120"),
        *[
            (name, "SECRET-forbidden")
            for name in (
                "provider",
                "base_url",
                "api",
                "auth",
                "api_key",
                "api_key_env",
                "dictation_base_url",
                "dictation_api_key_env",
                "dictation_api_key",
                "plugins",
                "workspace",
                "data_dir",
                "trust_project",
                "demo",
                "profiles",
                "config",
                "path",
                "temperature",
                "reasoning",
                "compaction",
            )
        ],
    ],
)
def test_settings_strict_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            body = {"revision": before["revision"], "values": {**before["values"], field: value}}
            response = await client.post(
                "/api/settings", content=json.dumps(body), headers={**headers, "Content-Type": "application/json"}
            )
            assert response.status_code == 422
            assert isinstance(response.json()["detail"], str) and "SECRET" not in response.text
            assert (await client.get("/api/settings", headers=headers)).json() == before
            assert SettingsValues.current(harnesses[0]).model_dump() == before["values"]

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/api/settings", "/api/settings/reset"])
def test_settings_request_guards(tmp_path: Path, path: str) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            before = (await client.get("/api/settings", headers=headers)).json()
            body = {"revision": before["revision"]}
            if path == "/api/settings":
                body["values"] = before["values"]
            for bad in ({}, {**body, "revision": True}, {**body, "revision": ""}, {**body, "SECRET": "secret"}):
                response = await client.post(path, json=bad, headers=headers)
                assert response.status_code == 422 and isinstance(response.json()["detail"], str)
                assert "SECRET" not in response.text
            assert (await client.get("/api/settings")).status_code == 403
            for bad_headers in (
                {},
                {"Origin": URL},
                {"X-Ngn-Token": headers["X-Ngn-Token"]},
                {**headers, "Origin": "http://localhost:8765"},
                {**headers, "Host": "evil.example"},
                {**headers, "Sec-Fetch-Site": "cross-site"},
            ):
                assert (await client.post(path, json=body, headers=bad_headers)).status_code == 403
            assert (
                await client.post(path, content="{}", headers={**headers, "Content-Type": "text/plain"})
            ).status_code == 415
            assert (
                await client.post(path, content="x" * 65537, headers={**headers, "Content-Type": "application/json"})
            ).status_code == 413
            assert (await client.get("/api/settings?path=SECRET", headers=headers)).status_code == 400
            assert (await client.get("/api/settings", headers=headers)).json() == before

    asyncio.run(check())


def test_settings_stale_other_client_and_harness_busy(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (app, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            body = {"revision": before["revision"], "values": {**before["values"], "model": "new-model"}}
            with harnesses[0].operation("other library operation"):
                assert (await client.post("/api/settings", json=body, headers=headers)).status_code == 409
                assert (
                    await client.post("/api/settings/reset", json={"revision": before["revision"]}, headers=headers)
                ).status_code == 409
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as other:
                assert (await other.get("/api/settings", headers=headers)).json() == before
                assert (await client.post("/api/settings", json=body, headers=headers)).status_code == 200
                assert (await other.post("/api/settings", json=body, headers=headers)).status_code == 409
                assert (
                    await other.post("/api/settings/reset", json={"revision": before["revision"]}, headers=headers)
                ).status_code == 409

    asyncio.run(check())


@pytest.mark.parametrize("prompt", ["approval", "wait"])
def test_settings_reject_active_run_and_approval(tmp_path: Path, prompt: str) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (app, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            stream = LiveStream(app, headers, harnesses[0].session_id, prompt)
            await stream.event("approval" if prompt == "approval" else "text_chunk")
            try:
                assert (await client.get("/api/settings", headers=headers)).json() == before
                assert (
                    await client.post(
                        "/api/settings",
                        json={"revision": before["revision"], "values": before["values"]},
                        headers=headers,
                    )
                ).status_code == 409
                assert (
                    await client.post("/api/settings/reset", json={"revision": before["revision"]}, headers=headers)
                ).status_code == 409
            finally:
                await stream.disconnect()

    asyncio.run(check())


@pytest.mark.parametrize("version", [1, 2])
def test_settings_persist_fresh_lifespan_and_reset_clears_override(tmp_path: Path, version: int) -> None:
    async def check() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            session_id = harnesses[0].session_id
            db_path = harnesses[0].agent.session.db_path
            await harnesses[0].agent.session.add_message(session_id, Message(role="user", content="PVC history"))
            values = {
                **initial["values"],
                "model": "saved-model",
                "agent": "audit",
                "shell_timeout": 0.5,
                "max_output": 1024,
                "max_file_bytes": 1024,
                "max_tool_rounds": 1,
                "max_subagent_depth": 0,
            }
            response = await client.post(
                "/api/settings", json={"revision": initial["revision"], "values": values}, headers=headers
            )
            assert response.status_code == 200
            saved = response.json()
        assert config.model == "gpt-4.1" and config.agent == "build"
        if version == 1:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE ngn_web_settings SET version = 1, values_json = ?",
                    (json.dumps({key: values[key] for key in LEGACY_FIELDS}),),
                )
                await db.commit()
        async with client_app(tmp_path, config=replace(config, model="new-trusted-default")) as (
            _,
            client,
            headers,
            harnesses,
        ):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"] == values and loaded["persisted"] is True
            assert loaded["revision"] == saved["revision"]
            assert loaded["defaults"]["model"] == "new-trusted-default"
            assert SettingsValues.current(harnesses[0]).model_dump() == values
            assert harnesses[0].agent.max_tool_rounds == 1
            resumed = await client.post("/api/sessions/resume", json={"session_id": session_id}, headers=headers)
            assert resumed.json()["history"][0]["content"] == "PVC history"
            reset = await client.post("/api/settings/reset", json={"revision": loaded["revision"]}, headers=headers)
            assert reset.status_code == 200
            assert reset.json()["values"] == loaded["defaults"] and reset.json()["persisted"] is False
            assert reset.json()["revision"] != loaded["revision"]
            assert harnesses[0].agent.max_tool_rounds == config.max_tool_rounds
            async with aiosqlite.connect(db_path) as db, db.execute("SELECT COUNT(*) FROM ngn_web_settings") as cursor:
                assert await cursor.fetchone() == (0,)
        async with client_app(tmp_path, config=replace(config, model="future-trusted-default")) as (
            _,
            client,
            headers,
            _,
        ):
            current = (await client.get("/api/settings", headers=headers)).json()
            assert current["values"] == current["defaults"] and current["persisted"] is False
            assert current["values"]["model"] == "future-trusted-default"

    asyncio.run(check())


def test_settings_v1_reader_preserves_row_history_revision_and_upgrades_on_save(tmp_path: Path) -> None:
    async def check() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            harness = harnesses[0]
            session_id, db_path = harness.session_id, harness.agent.session.db_path
            await harness.agent.session.add_message(session_id, Message(role="user", content="legacy history"))
        legacy = {
            **{key: initial["values"][key] for key in LEGACY_FIELDS},
            "model": "legacy-chat-model",
            "agent": "audit",
            "max_tool_rounds": 3,
            "max_subagent_depth": 0,
        }
        payload, revision = json.dumps(legacy), "a" * 64
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 1, ?, ?)",
                (payload, revision),
            )
            await db.commit()

        for language, limit in (("en", 90), ("fr", 30)):
            trusted = replace(
                config,
                dictation_enabled=True,
                dictation_model="trusted-transcriber",
                dictation_language=language,
                dictation_max_seconds=limit,
            )
            async with client_app(tmp_path, config=trusted) as (_, client, headers, harnesses):
                loaded = (await client.get("/api/settings", headers=headers)).json()
                values = {
                    **legacy,
                    "dictation_enabled": True,
                    "dictation_model": "trusted-transcriber",
                    "dictation_language": language,
                    "dictation_max_seconds": limit,
                }
                assert loaded["values"] == values
                assert loaded["persisted"] is True and loaded["revision"] == revision
                assert loaded["dictation"]["revision"] == revision
                assert_runtime(harnesses[0], values)
                assert (await client.get("/api/settings", headers=headers)).json() == loaded
                async with aiosqlite.connect(db_path) as db:
                    assert list(await db.execute_fetchall("SELECT * FROM ngn_web_settings")) == [
                        (1, 1, payload, revision)
                    ]
                resumed = await client.post("/api/sessions/resume", json={"session_id": session_id}, headers=headers)
                assert resumed.json()["history"][0]["content"] == "legacy history"
                if language == "fr":
                    values.update(
                        dictation_model="saved-transcriber", dictation_language="de", dictation_max_seconds=300
                    )
                    response = await client.post(
                        "/api/settings", json={"revision": revision, "values": values}, headers=headers
                    )
                    assert response.status_code == 200
                    saved = response.json()
                    assert saved["revision"] != revision and saved["values"] == values
                    async with aiosqlite.connect(db_path) as db:
                        assert list(
                            await db.execute_fetchall("SELECT version, values_json, revision FROM ngn_web_settings")
                        ) == [(2, SettingsValues.model_validate(values).model_dump_json(), saved["revision"])]

        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"] == saved["values"] and loaded["revision"] == saved["revision"]
            assert loaded["defaults"] == initial["defaults"]
            assert loaded["dictation"]["admin_enabled"] is False
            assert loaded["dictation"]["enabled"] is False and loaded["dictation"]["max_seconds"] == 120
            reset = await client.post("/api/settings/reset", json={"revision": loaded["revision"]}, headers=headers)
            assert reset.status_code == 200
            assert reset.json()["values"] == initial["defaults"] and reset.json()["persisted"] is False
            assert reset.json()["revision"] != loaded["revision"]
            assert reset.json()["dictation"]["revision"] == reset.json()["revision"]
            assert_runtime(harnesses[0], initial["defaults"])
            resumed = await client.post("/api/sessions/resume", json={"session_id": session_id}, headers=headers)
            assert resumed.json()["history"][0]["content"] == "legacy history"
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"] == initial["defaults"] and loaded["persisted"] is False

    asyncio.run(check())


@pytest.mark.parametrize("restriction", ["disabled", "lower-ceiling", "demo"])
def test_dictation_saved_preferences_respect_restarted_admin_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restriction: str
) -> None:
    monkeypatch.setenv("TEST_VOICE_KEY", "synthetic-voice-key")

    async def check() -> None:
        config = replace(
            configuration(tmp_path),
            demo=False,
            auth="api-key",
            api_key_env="TEST_CHAT_KEY",
            dictation_enabled=True,
            dictation_api_key_env="TEST_VOICE_KEY",
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            before = (await client.get("/api/settings", headers=headers)).json()
            response = await client.post(
                "/api/settings",
                json={
                    "revision": before["revision"],
                    "values": {**before["values"], "dictation_language": "de", "dictation_max_seconds": 300},
                },
                headers=headers,
            )
            assert response.status_code == 200
            saved = response.json()
            assert saved["dictation"]["available"] is True and saved["dictation"]["max_seconds"] == 120

        trusted = replace(
            config,
            demo=restriction == "demo",
            dictation_enabled=restriction != "disabled",
            dictation_max_seconds=15,
        )
        async with client_app(tmp_path, config=trusted) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"] == saved["values"] and loaded["revision"] == saved["revision"]
            assert (
                loaded["dictation"]["enabled"] == loaded["dictation"]["available"] == (restriction == "lower-ceiling")
            )
            assert loaded["dictation"]["admin_enabled"] == trusted.dictation_enabled
            assert loaded["dictation"]["max_seconds"] == 15 and loaded["dictation"]["max_bytes"] == 15 * 32000 + 4096
            response = await client.post(
                "/api/settings",
                json={"revision": loaded["revision"], "values": {**loaded["values"], "model": "other-chat-model"}},
                headers=headers,
            )
            assert response.status_code == 200 and harnesses[0].agent.provider.model == "other-chat-model"
            assert harnesses[0].config.dictation_enabled == trusted.dictation_enabled
            assert harnesses[0].config.dictation_max_seconds == 15
            reset = await client.post(
                "/api/settings/reset", json={"revision": response.json()["revision"]}, headers=headers
            )
            assert reset.status_code == 200 and reset.json()["values"] == loaded["defaults"]
            assert reset.json()["dictation"]["max_seconds"] == 15

    asyncio.run(check())


@pytest.mark.parametrize("version", [1, 2])
def test_settings_stored_revision_conflict_preserves_dictation_preferences(tmp_path: Path, version: int) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = initial["values"] if version == 2 else {key: initial["values"][key] for key in LEGACY_FIELDS}
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, ?, ?, ?)",
                (version, json.dumps(values), "a" * 64),
            )
            await db.commit()
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            async with aiosqlite.connect(db_path) as db:
                await db.execute("UPDATE ngn_web_settings SET revision = ?", ("b" * 64,))
                await db.commit()
                row = list(await db.execute_fetchall("SELECT * FROM ngn_web_settings"))
            for path in ("/api/settings", "/api/settings/reset"):
                body = {"revision": before["revision"]}
                if path == "/api/settings":
                    body["values"] = {
                        **before["values"],
                        "dictation_enabled": True,
                        "dictation_language": "de",
                        "dictation_max_seconds": 30,
                    }
                response = await client.post(path, json=body, headers=headers)
                assert response.status_code == 409
                assert (await client.get("/api/settings", headers=headers)).json() == before
                assert_runtime(harnesses[0], before["values"])
                async with aiosqlite.connect(db_path) as db:
                    assert list(await db.execute_fetchall("SELECT * FROM ngn_web_settings")) == row

    asyncio.run(check())


def test_settings_defaults_captured_after_initial_model_resolution(tmp_path: Path) -> None:
    async def resolved(harness: Harness) -> None:
        harness.config.model = "resolved-codex-default"
        harness.agent.provider.model = "resolved-codex-default"

    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="chatgpt")
        with patch.object(Harness, "_use_chatgpt", resolved):
            for expected in ("resolved-codex-default", "saved-codex-model"):
                async with client_app(tmp_path, config=config) as (_, client, headers, _):
                    before = (await client.get("/api/settings", headers=headers)).json()
                    assert before["values"]["model"] == expected
                    assert before["defaults"]["model"] == "resolved-codex-default"
                    saved = await client.post(
                        "/api/settings",
                        json={
                            "revision": before["revision"],
                            "values": {**before["values"], "model": "saved-codex-model"},
                        },
                        headers=headers,
                    )
                    assert saved.status_code == 200
                    if expected == "saved-codex-model":
                        reset = await client.post(
                            "/api/settings/reset", json={"revision": saved.json()["revision"]}, headers=headers
                        )
                        assert reset.json()["values"]["model"] == "resolved-codex-default"

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["write", "commit", "instructions"])
@pytest.mark.parametrize("reset", [False, True])
def test_settings_failures_restore_live_and_persisted_values(tmp_path: Path, failure: str, reset: bool) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            initial = (await client.get("/api/settings", headers=headers)).json()
            response = await client.post(
                "/api/settings",
                json={
                    "revision": initial["revision"],
                    "values": {
                        **initial["values"],
                        "model": "old-saved-model",
                        "dictation_enabled": True,
                        "dictation_model": "old-transcriber",
                        "dictation_language": "de",
                        "dictation_max_seconds": 60,
                    },
                },
                headers=headers,
            )
            before = response.json()
            prompt = harness.agent.system_prompt
            db_path = harness.agent.session.db_path
            async with aiosqlite.connect(db_path) as db:
                async with db.execute("SELECT * FROM ngn_web_settings") as cursor:
                    old_row = await cursor.fetchone()
                if failure == "write":
                    action = "DELETE" if reset else "INSERT"
                    await db.execute(
                        f"CREATE TRIGGER fail_settings BEFORE {action} ON ngn_web_settings BEGIN SELECT RAISE(ABORT, 'SECRET-sql-error'); END"
                    )
                    await db.commit()
            body = {"revision": before["revision"]}
            if not reset:
                body["values"] = {
                    **before["values"],
                    "model": "candidate-model",
                    "agent": "audit",
                    "max_tool_rounds": 2,
                    "max_subagent_depth": 0,
                    "dictation_enabled": False,
                    "dictation_model": "candidate-transcriber",
                    "dictation_language": "fr",
                    "dictation_max_seconds": 30,
                }
            path = "/api/settings/reset" if reset else "/api/settings"
            if failure == "commit":
                with patch.object(
                    aiosqlite.Connection, "commit", side_effect=sqlite3.OperationalError("SECRET-commit-error")
                ):
                    response = await client.post(path, json=body, headers=headers)
            elif failure == "instructions":

                def broken_instructions() -> None:
                    harness.agent.system_prompt = "incomplete prompt"
                    raise ValueError("SECRET-instructions-error")

                with patch.object(harness, "refresh_instructions", broken_instructions):
                    response = await client.post(path, json=body, headers=headers)
            else:
                response = await client.post(path, json=body, headers=headers)
            assert response.status_code == 500 and "SECRET" not in response.text
            assert isinstance(response.json()["detail"], str)
            assert (await client.get("/api/settings", headers=headers)).json() == before
            assert_runtime(harness, before["values"])
            assert {key: getattr(harness.config, key) for key in DICTATION_FIELDS} == {
                key: initial["defaults"][key] for key in DICTATION_FIELDS
            }
            assert harness.agent.max_tool_rounds == before["values"]["max_tool_rounds"]
            assert harness.agent.system_prompt == prompt
            async with aiosqlite.connect(db_path) as db, db.execute("SELECT * FROM ngn_web_settings") as cursor:
                assert await cursor.fetchone() == old_row

    asyncio.run(check())


@pytest.mark.parametrize("fail_commit", [False, True])
@pytest.mark.parametrize("reset", [False, True])
def test_settings_cancellation_joins_transaction_and_keeps_get_consistent(
    tmp_path: Path, fail_commit: bool, reset: bool
) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            harness = harnesses[0]
            before = (await client.get("/api/settings", headers=headers)).json()
            if reset:
                saved = await client.post(
                    "/api/settings",
                    json={
                        "revision": before["revision"],
                        "values": {
                            **before["values"],
                            "model": "previous-model",
                            "dictation_enabled": True,
                            "dictation_model": "previous-transcriber",
                            "dictation_language": "de",
                            "dictation_max_seconds": 60,
                        },
                    },
                    headers=headers,
                )
                assert saved.status_code == 200
                before = saved.json()
            values = {
                **before["values"],
                "model": "committed-model",
                "agent": "reviewer",
                "max_tool_rounds": 2,
                "dictation_enabled": True,
                "dictation_model": "committed-transcriber",
                "dictation_language": "fr",
                "dictation_max_seconds": 30,
            }
            body = {"revision": before["revision"]}
            if reset:
                values = before["defaults"]
            else:
                body["values"] = values
            entered, release = asyncio.Event(), asyncio.Event()
            commit = aiosqlite.Connection.commit

            async def delayed_commit(db: aiosqlite.Connection) -> None:
                entered.set()
                await release.wait()
                if fail_commit:
                    raise sqlite3.OperationalError("SECRET-delayed-commit")
                await commit(db)

            with patch.object(aiosqlite.Connection, "commit", delayed_commit):
                request = asyncio.create_task(
                    client.post("/api/settings/reset" if reset else "/api/settings", json=body, headers=headers)
                )
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    assert harness.config.model == values["model"]
                    assert (await client.get("/api/settings", headers=headers)).json() == before
                    assert (await client.get("/api/bootstrap")).json()["model"] == before["values"]["model"]
                    for path, body in (
                        ("/api/settings", {"revision": before["revision"], "values": values}),
                        ("/api/settings/reset", {"revision": before["revision"]}),
                        ("/api/sessions/new", {}),
                        ("/api/run", {"session_id": harness.session_id, "prompt": "wait"}),
                    ):
                        assert (await client.post(path, json=body, headers=headers)).status_code == 409
                    request.cancel()
                    await asyncio.sleep(0)
                    request.cancel()
                    await asyncio.sleep(0)
                    assert not request.done()
                finally:
                    release.set()
                if fail_commit:
                    response = await asyncio.wait_for(request, 5)
                    assert response.status_code == 500
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(request, 5)
            after = (await client.get("/api/settings", headers=headers)).json()
            assert after["values"] == (before["values"] if fail_commit else values)
            assert after["persisted"] == (before["persisted"] if fail_commit else not reset)
            assert_runtime(harness, after["values"])
            assert after["dictation"]["revision"] == after["revision"]
            assert after["dictation"]["max_seconds"] == after["values"]["dictation_max_seconds"]
            assert {key: getattr(harness.config, key) for key in DICTATION_FIELDS} == {
                key: before["defaults"][key] for key in DICTATION_FIELDS
            }
            assert harness.agent.max_tool_rounds == after["values"]["max_tool_rounds"]
            assert (await client.post("/api/sessions/new", json={}, headers=headers)).status_code == 200
        async with client_app(tmp_path) as (_, client, headers, _):
            assert (await client.get("/api/settings", headers=headers)).json()["values"] == after["values"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "corruption",
    [
        "json",
        "version",
        "unknown",
        "missing",
        "removed-profile",
        "oversized",
        "nonfinite",
        "revision",
        "wrong-schema",
        "coercion",
    ],
)
@pytest.mark.parametrize("version", [1, 2])
def test_settings_corrupt_saved_row_fails_closed(tmp_path: Path, corruption: str, version: int) -> None:
    async def check() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = before["values"] if version == 2 else {key: before["values"][key] for key in LEGACY_FIELDS}
        saved_version, revision = version, "a" * 64
        if corruption == "unknown":
            values["trust_project"] = True
        elif corruption == "missing":
            del values["agent"]
        elif corruption == "removed-profile":
            values["agent"] = "SECRET-removed-profile"
        elif corruption == "nonfinite":
            values["shell_timeout"] = float("nan")
        elif corruption == "coercion":
            values["max_output"] = "2048"
        elif corruption == "version":
            saved_version = 3
        elif corruption == "revision":
            revision = "SECRET-invalid-revision"
        elif corruption == "wrong-schema":
            if version == 1:
                values["dictation_enabled"] = True
            else:
                del values["dictation_model"]
        payload = json.dumps(values)
        if corruption == "json":
            payload = "{SECRET-invalid-json"
        elif corruption == "oversized":
            payload = " " * 16385 + payload
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, ?, ?, ?)",
                (saved_version, payload, revision),
            )
            await db.commit()
        harness = ControlledHarness(config)
        app = create_app(config, assets=tmp_path / "static", harness_factory=lambda _: harness)
        with pytest.raises(RuntimeError, match="Cannot load saved web settings") as error:
            async with app.router.lifespan_context(app):
                pytest.fail("Corrupt persisted settings must not silently restore build permissions")
        assert "SECRET" not in str(error.value) and harness.closed
        assert harness.config.agent == "build"

    asyncio.run(check())


@pytest.mark.requires_posix
def test_settings_tools_use_new_byte_and_timeout_limits(tmp_path: Path) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            before = (await client.get("/api/settings", headers=headers)).json()
            values = {**before["values"], "max_output": 1024, "max_file_bytes": 2048, "shell_timeout": 0.1}
            assert (
                await client.post(
                    "/api/settings", json={"revision": before["revision"], "values": values}, headers=headers
                )
            ).status_code == 200
            (tmp_path / "bounded.txt").write_text("x" * 1500)
            assert len(str((await harness.tools.read_file("bounded.txt"))["content"]).encode()) == 1024
            (tmp_path / "too-big.txt").write_text("x" * 2049)
            with pytest.raises(ValueError, match="2048 byte limit"):
                await harness.tools.read_file("too-big.txt")
            with pytest.raises(ValueError, match="timeout"):
                await harness.tools.shell("true", timeout=0.2)
            # No active web approval: even a valid bounded shell still requires approval.
            with pytest.raises(PermissionError, match="denied"):
                await harness.tools.shell("true")

    asyncio.run(check())
