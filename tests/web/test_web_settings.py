"""Workspace settings API, transactional persistence and portable runtime coverage."""

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import aiosqlite
import httpx
import pytest

from nagents.compactor import Messages
from nagents.compactor import Tokens
from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.types import Message
from nagents.types import ToolCall
from nagents.web.app import create_app
from nagents.web.settings import SettingsValues
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import URL
from tests.support.web import ControlledHarness
from tests.support.web import LiveStream
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

LEGACY_FIELDS = (
    "model",
    "agent",
    "shell_timeout",
    "max_output",
    "max_file_bytes",
    "max_tool_rounds",
    "max_subagent_depth",
)
PROVIDER_FIELDS = ("provider", "base_url", "api", "auth", "api_key_env")
COMPACTION_FIELDS = ("compact_trigger", "compact_tokens", "compact_messages")
SUBMIT_FIELDS = ("submit_mode",)
READ_ONLY_FIELDS = ("read_only",)


def legacy_values(version: int, values: dict[str, object]) -> dict[str, object]:
    """Strip fields that did not exist in the requested saved schema."""
    if version == 1:
        return {key: values[key] for key in LEGACY_FIELDS}
    if version == 2:
        return {key: values[key] for key in LEGACY_FIELDS}
    if version == 3:
        # Version 3 predates submit_mode and read_only, persisted from version 4.
        return {
            key: values[key] for key in SettingsValues.model_fields if key not in (*SUBMIT_FIELDS, *READ_ONLY_FIELDS)
        }
    return {key: values[key] for key in SettingsValues.model_fields}


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


def test_web_and_terminal_share_global_model_until_workspace_overrides_it(tmp_path: Path) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    store.model_store("global").save("global-chat-model")

    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            first = (await client.get("/api/settings", headers=headers)).json()
            assert first["values"]["model"] == "global-chat-model"
            assert first["defaults"]["model"] == "global-chat-model"
            assert harnesses[0].agent.provider.model == "global-chat-model"
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": first["revision"],
                    "values": {**first["values"], "model": "workspace-chat-model"},
                },
            )
            assert saved.status_code == 200
            assert store.model_store("workspace").load() == "workspace-chat-model"
            assert store.model_store("global").load() == "global-chat-model"
            assert load_config(tmp_path).model == "workspace-chat-model"
            reset = await client.post(
                "/api/settings/reset", headers=headers, json={"revision": saved.json()["revision"]}
            )
            assert reset.status_code == 200
            assert store.model_store("workspace").load() == ""
            assert load_config(tmp_path).model == "global-chat-model"

    asyncio.run(check())


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
                    "scope",
                    "values",
                    "defaults",
                    "profiles",
                    "revision",
                    "persisted",
                    "effective_mode",
                    "effective_model",
                    "connection",
                    "providers",
                    "apis",
                    "auths",
                    "read_only_locked",
                }
                assert body["values"] == body["defaults"] == SettingsValues.current(harness).model_dump()
                assert (
                    set(body["values"])
                    == set(SettingsValues.model_fields)
                    == {
                        *LEGACY_FIELDS,
                        *PROVIDER_FIELDS,
                        *COMPACTION_FIELDS,
                        *SUBMIT_FIELDS,
                        *READ_ONLY_FIELDS,
                    }
                )
                assert body["providers"] == sorted(body["providers"]) and "openai" in body["providers"]
                assert body["apis"] == ["auto", "chat_completions", "responses", "messages"]
                assert body["auths"] == ["auto", "api-key", "chatgpt", "codex", "entra"]
                assert body["persisted"] is False and body["effective_mode"] == "build"
                assert body["profiles"] == [
                    {"name": "assistant", "mode": "build", "model": ""},
                    {"name": "audit", "mode": "reviewer", "model": "profile-model"},
                ]
                assert body["connection"] == {
                    "provider": "openai",
                    "api": "responses",
                    "auth": "api-key",
                    "base_url": "https://example.invalid/private-endpoint",
                    "api_key_env": "TEST_API_KEY",
                    "key_configured": False,
                    "auth_status": harness.auth_status(),
                }
                assert "SECRET" not in response.text
                assert str(tmp_path) not in response.text
                assert response.headers["cache-control"] == "no-store"
                saved = await client.post(
                    "/api/settings", json={"revision": body["revision"], "values": body["values"]}, headers=headers
                )
                assert saved.status_code == 200

    asyncio.run(check())


def test_settings_change_preserves_submit_mode_when_omitted(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            assert before["values"]["submit_mode"] == "queue"
            selected = {**before["values"], "submit_mode": "interrupt", "model": "submitted-model"}
            saved = await client.post(
                "/api/settings", json={"revision": before["revision"], "values": selected}, headers=headers
            )
            assert saved.status_code == 200
            assert saved.json()["values"]["submit_mode"] == "interrupt"
            assert harnesses[0].config.submit_mode == "interrupt"
            # An older client omits submit_mode; the committed value is preserved.
            omitted = {key: value for key, value in saved.json()["values"].items() if key not in SUBMIT_FIELDS}
            omitted["model"] = "omitted-model"
            again = await client.post(
                "/api/settings", json={"revision": saved.json()["revision"], "values": omitted}, headers=headers
            )
            assert again.status_code == 200
            assert again.json()["values"]["submit_mode"] == "interrupt"
            assert harnesses[0].config.submit_mode == "interrupt"

    asyncio.run(check())


@pytest.mark.parametrize("version", [1, 2, 3])
def test_settings_legacy_rows_inherit_trusted_submit_mode(tmp_path: Path, version: int) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), submit_mode="interrupt")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = legacy_values(version, initial["values"])
        assert "submit_mode" not in values
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, ?, ?, ?)",
                (version, json.dumps(values), "a" * 64),
            )
            await db.commit()
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"]["submit_mode"] == "interrupt"
            assert harnesses[0].config.submit_mode == "interrupt"

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


@pytest.mark.parametrize("field", LEGACY_FIELDS)
def test_settings_require_all_legacy_fields(tmp_path: Path, field: str) -> None:
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
            old_child = harness.tasks._create_child("assistant")
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
            assert saved["effective_model"] == "profile-model"
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
            assert bootstrap["model"] == "profile-model" and bootstrap["agent"] == "audit"
            new_child = harness.tasks._create_child("assistant")
            assert new_child.agent.provider.model == "profile-model"
            assert old_child.agent.provider.model == before["values"]["model"]
            await old_child.close()
            await new_child.close()
            # Leaving the agent's explicit model restores the workspace choice.
            harness._permission_ceiling = "reviewer"
            values.update(agent="assistant", max_subagent_depth=8)
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
        ("compact_trigger", "sometimes"),
        ("compact_trigger", "AUTO"),
        ("compact_trigger", ""),
        ("compact_trigger", True),
        ("compact_tokens", 1023),
        ("compact_tokens", 10000001),
        ("compact_tokens", True),
        ("compact_tokens", 2048.0),
        ("compact_tokens", "2048"),
        ("compact_messages", 0),
        ("compact_messages", 10001),
        ("compact_messages", False),
        ("compact_messages", 2.0),
        ("compact_messages", "100"),
        *[
            (name, "SECRET-forbidden")
            for name in (
                "provider",
                "base_url",
                "api",
                "auth",
                "api_key",
                "api_key_env",
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


def test_compaction_criteria_apply_to_the_live_agent(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            before = (await client.get("/api/settings", headers=headers)).json()
            assert before["values"]["compact_trigger"] == "auto"
            assert harness.agent.compactor == "self" and harness.agent.compact_on is None
            tokens = {**before["values"], "compact_trigger": "tokens", "compact_tokens": 50000}
            saved = await client.post(
                "/api/settings", json={"revision": before["revision"], "values": tokens}, headers=headers
            )
            assert saved.status_code == 200
            assert harness.agent.compactor == "self"
            assert isinstance(harness.agent.compact_on, Tokens) and harness.agent.compact_on.total == 50000
            assert harness.agent.compact_on.input == 40000 and harness.agent.compact_on.output == 5000
            assert SettingsValues.current(harness).compact_trigger == "tokens"
            messages = {**saved.json()["values"], "compact_trigger": "messages", "compact_messages": 7}
            saved = await client.post(
                "/api/settings", json={"revision": saved.json()["revision"], "values": messages}, headers=headers
            )
            assert saved.status_code == 200
            assert isinstance(harness.agent.compact_on, Messages) and harness.agent.compact_on.length == 7
            assert SettingsValues.current(harness).compact_messages == 7
            off = {**saved.json()["values"], "compact_trigger": "off"}
            saved = await client.post(
                "/api/settings", json={"revision": saved.json()["revision"], "values": off}, headers=headers
            )
            assert saved.status_code == 200
            assert harness.agent.compactor is None and harness.agent.compact_on is None
            assert SettingsValues.current(harness).compact_trigger == "off"
            reset = await client.post(
                "/api/settings/reset", json={"revision": saved.json()["revision"]}, headers=headers
            )
            assert reset.status_code == 200
            assert reset.json()["values"]["compact_trigger"] == "auto"
            assert harness.agent.compactor == "self" and harness.agent.compact_on is None

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


@pytest.mark.parametrize("version", [1, 2, 3, 4])
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
        assert config.model == "gpt-6-luna" and config.agent == "assistant"
        if version != 4:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE ngn_web_settings SET version = ?, values_json = ?",
                    (version, json.dumps(legacy_values(version, values))),
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


@pytest.mark.parametrize("legacy_agent", ["build", "agent", "reviewer"])
def test_settings_legacy_default_agent_migrates_to_assistant(tmp_path: Path, legacy_agent: str) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        # Version 3 predates submit_mode and read_only; the reader rejects them in a v3 row.
        legacy_fields = (*SUBMIT_FIELDS, *READ_ONLY_FIELDS)
        values = {key: value for key, value in initial["values"].items() if key not in legacy_fields}
        values["agent"] = legacy_agent
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 3, ?, ?)",
                (json.dumps(values), "a" * 64),
            )
            await db.commit()
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["persisted"] is True and loaded["revision"] == "a" * 64
            assert loaded["values"]["agent"] == "assistant"
            harness = harnesses[0]
            assert harness.config.agent == "assistant"
            assert loaded["values"]["read_only"] is (legacy_agent == "reviewer")
            assert harness.config.read_only is (legacy_agent == "reviewer")
            if legacy_agent == "reviewer":
                # Legacy reviewer migrates to the read-only assistant: no writes/shell.
                assert loaded["effective_mode"] == "reviewer" and harness.mode == "reviewer"
                for call in (
                    ToolCall("call", "write", {"path": "blocked.txt", "content": "no"}),
                    ToolCall("call", "shell", {"command": "true"}),
                    ToolCall("call", "edit", {"path": "blocked.txt", "old": "a", "new": "b"}),
                ):
                    result = await harness.agent.tool_executor.execute(call)
                    assert result.error and "Read-only mode denies" in result.error
                assert not (tmp_path / "blocked.txt").exists()
            else:
                assert loaded["effective_mode"] == "build" and harness.mode == "build"

    asyncio.run(check())


@pytest.mark.requires_posix
def test_settings_stored_read_only_false_cannot_lower_startup_floor(tmp_path: Path) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key", read_only=True)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
            assert initial["values"]["read_only"] is True and initial["read_only_locked"] is True
        # A persisted v4 row explicitly clears the preference; the startup floor
        # is an admin ceiling, not a stored preference.
        values = {**initial["values"], "read_only": False}
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 4, ?, ?)",
                (json.dumps(values), "c" * 64),
            )
            await db.commit()
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["persisted"] is True and loaded["revision"] == "c" * 64
            # Stored preference remains False, but the effective floor stays reviewer.
            assert loaded["values"]["read_only"] is False
            assert loaded["read_only_locked"] is True
            harness = harnesses[0]
            assert harness.config.read_only is True and harness.mode == "reviewer"
            denied = await harness.agent.tool_executor.execute(ToolCall("call", "write", {"path": "x", "content": "y"}))
            assert denied.error and "Read-only mode denies" in denied.error

    asyncio.run(check())


@pytest.mark.parametrize("legacy_agent", ["reviewer", "audit"])
def test_settings_explicit_custom_reviewer_keeps_identity(tmp_path: Path, legacy_agent: str) -> None:
    async def check() -> None:
        # An explicitly configured profile named like a legacy value is not migrated.
        config = replace(
            configuration(tmp_path),
            demo=False,
            auth="api-key",
            profiles={"reviewer": AgentProfile(mode="reviewer")}
            if legacy_agent == "reviewer"
            else configuration(tmp_path).profiles,
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = {
            key: value for key, value in initial["values"].items() if key not in (*SUBMIT_FIELDS, *READ_ONLY_FIELDS)
        }
        values["agent"] = legacy_agent
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, 3, ?, ?)",
                (json.dumps(values), "b" * 64),
            )
            await db.commit()
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"]["agent"] == legacy_agent
            assert loaded["values"]["read_only"] is False
            assert harnesses[0].config.agent == legacy_agent
            assert harnesses[0].mode == "reviewer"

    asyncio.run(check())


@pytest.mark.requires_posix
def test_settings_read_only_cannot_be_escaped_by_a_false_flag(tmp_path: Path) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key", read_only=True)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            assert before["read_only_locked"] is True
            assert before["values"]["read_only"] is True
            # A client that tries to clear the flag cannot escape the startup ceiling.
            attempted = {**before["values"], "read_only": False, "model": "escape-model"}
            response = await client.post(
                "/api/settings", json={"revision": before["revision"], "values": attempted}, headers=headers
            )
            assert response.status_code == 200
            harness = harnesses[0]
            assert harness.config.read_only is True and harness.mode == "reviewer"
            assert response.json()["read_only_locked"] is True
            # The stored preference may remain False; the admin ceiling still binds.
            assert response.json()["values"]["read_only"] is False
            denied = await harness.agent.tool_executor.execute(ToolCall("call", "write", {"path": "x", "content": "y"}))
            assert denied.error and "Read-only mode denies" in denied.error

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

        async with client_app(tmp_path, config=replace(config, model="new-trusted-default")) as (
            _,
            client,
            headers,
            harnesses,
        ):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            values = {**initial["values"], **legacy}
            assert loaded["values"] == values
            assert loaded["defaults"]["model"] == "new-trusted-default"
            assert loaded["persisted"] is True and loaded["revision"] == revision
            assert_runtime(harnesses[0], values)
            async with aiosqlite.connect(db_path) as db:
                assert list(await db.execute_fetchall("SELECT * FROM ngn_web_settings")) == [(1, 1, payload, revision)]
            resumed = await client.post("/api/sessions/resume", json={"session_id": session_id}, headers=headers)
            assert resumed.json()["history"][0]["content"] == "legacy history"
            saved_response = await client.post(
                "/api/settings", json={"revision": revision, "values": values}, headers=headers
            )
            assert saved_response.status_code == 200
            saved = saved_response.json()
            assert saved["revision"] != revision and saved["values"] == values
            async with (
                aiosqlite.connect(db_path) as db,
                db.execute("SELECT version, values_json FROM ngn_web_settings") as cursor,
            ):
                row = await cursor.fetchone()
                assert row is not None
                assert row[0] == 5

        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"] == saved["values"]
            assert loaded["revision"] == saved["revision"]
            reset = await client.post("/api/settings/reset", json={"revision": loaded["revision"]}, headers=headers)
            assert reset.status_code == 200
            assert reset.json()["values"] == initial["defaults"] and reset.json()["persisted"] is False
            assert_runtime(harnesses[0], initial["defaults"])
            resumed = await client.post("/api/sessions/resume", json={"session_id": session_id}, headers=headers)
            assert resumed.json()["history"][0]["content"] == "legacy history"
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            loaded = (await client.get("/api/settings", headers=headers)).json()
            assert loaded["values"] == initial["defaults"] and loaded["persisted"] is False

    asyncio.run(check())


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_settings_stored_revision_conflict_preserves_preferences(tmp_path: Path, version: int) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = legacy_values(version, initial["values"])
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
                    body["values"] = {**before["values"], "max_tool_rounds": 3}
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
                        },
                    },
                    headers=headers,
                )
                assert saved.status_code == 200
                before = saved.json()
            values = {
                **before["values"],
                "model": "committed-model",
                "agent": "assistant",
                "max_tool_rounds": 2,
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
                    await asyncio.wait_for(entered.wait(), HANG_GUARD)
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
                    response = await asyncio.wait_for(request, HANG_GUARD)
                    assert response.status_code == 500
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(request, HANG_GUARD)
            after = (await client.get("/api/settings", headers=headers)).json()
            assert after["values"] == (before["values"] if fail_commit else values)
            assert after["persisted"] == (before["persisted"] if fail_commit else not reset)
            assert_runtime(harness, after["values"])
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
@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_settings_corrupt_saved_row_fails_closed(tmp_path: Path, corruption: str, version: int) -> None:
    async def check() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = legacy_values(version, before["values"])
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
            saved_version = 6
        elif corruption == "revision":
            revision = "SECRET-invalid-revision"
        elif corruption == "wrong-schema":
            values["unknown_preference"] = True
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
        assert harness.config.agent == "assistant"

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


def test_provider_override_stores_key_write_only_and_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DEPLOY_CHAT_KEY", "deployment-key")
    monkeypatch.delenv("TEST_OPENROUTER_KEY", raising=False)

    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key", api_key_env="TEST_DEPLOY_CHAT_KEY")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            before = (await client.get("/api/settings", headers=headers)).json()
            values = {
                **before["values"],
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4.1-flash",
                "base_url": "",
                "api": "auto",
                "auth": "api-key",
                "api_key_env": "TEST_OPENROUTER_KEY",
            }
            response = await client.post(
                "/api/settings",
                json={
                    "revision": before["revision"],
                    "values": values,
                    "api_key": "write-only-secret-value",
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["values"] == values
            assert body["defaults"]["provider"] == "openai"
            assert body["connection"]["provider"] == "openrouter"
            assert body["connection"]["key_configured"] is True
            assert "write-only-secret-value" not in response.text
            assert harness.config.provider == "openrouter"
            assert harness.config.model == "deepseek/deepseek-v4.1-flash"
            assert harness.agent.provider.provider_type.value == "openrouter"
            assert os.environ["TEST_OPENROUTER_KEY"] == "write-only-secret-value"
            listed = await client.get("/api/settings", headers=headers)
            assert listed.json() == body and "write-only-secret-value" not in listed.text
            with patch.object(
                harness.agent.provider, "get_model_list", AsyncMock(return_value=["deepseek/deepseek-v4.1-flash"])
            ):
                models = await client.get("/api/models", headers=headers)
            assert models.json() == {"models": ["deepseek/deepseek-v4.1-flash"], "source": "openrouter"}
            async with aiosqlite.connect(harness.agent.session.db_path) as db:
                assert list(await db.execute_fetchall("SELECT provider, secret FROM ngn_web_provider_keys")) == [
                    ("openrouter", "write-only-secret-value")
                ]
                stored = list(await db.execute_fetchall("SELECT values_json FROM ngn_web_settings"))
                assert len(stored) == 1 and "write-only-secret-value" not in stored[0][0]
            cleared = await client.post(
                "/api/settings",
                json={"revision": body["revision"], "values": values, "clear_api_key": True},
                headers=headers,
            )
            assert cleared.status_code == 200
            assert cleared.json()["connection"]["key_configured"] is False
            assert "TEST_OPENROUTER_KEY" not in os.environ
            assert harness.config.provider == "openrouter"
            reset = await client.post(
                "/api/settings/reset", json={"revision": cleared.json()["revision"]}, headers=headers
            )
            assert reset.status_code == 200
            assert reset.json()["connection"]["provider"] == "openai"
            assert reset.json()["persisted"] is False
            assert harness.config.provider == "openai" and harness.config.api_key_env == "TEST_DEPLOY_CHAT_KEY"
            assert os.environ["TEST_DEPLOY_CHAT_KEY"] == "deployment-key"

    asyncio.run(check())


def test_provider_override_rejects_unsafe_combinations(tmp_path: Path) -> None:
    async def check() -> None:
        config = replace(configuration(tmp_path), demo=False, auth="api-key", api_key_env="TEST_DEPLOY_CHAT_KEY")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            before = (await client.get("/api/settings", headers=headers)).json()
            for case in (
                {"provider": "litellm", "base_url": ""},
                {"provider": "openrouter", "auth": "chatgpt"},
                {"provider": "openrouter", "api_key_env": "not-an-env-name"},
                {"provider": "unknown-provider"},
                {"provider": "openrouter", "base_url": "https://user:pass@example.invalid/v1"},
                {"provider": "openrouter", "api": "completions"},
            ):
                response = await client.post(
                    "/api/settings",
                    json={"revision": before["revision"], "values": {**before["values"], **case}},
                    headers=headers,
                )
                assert response.status_code == 422, (case, response.text)
            assert harness.config.provider == "openai"
            assert (await client.get("/api/settings", headers=headers)).json() == before

    asyncio.run(check())
