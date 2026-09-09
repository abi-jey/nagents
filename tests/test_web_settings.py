"""Workspace settings API, transactional persistence and portable runtime coverage."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import httpx
import pytest

from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.types import Message
from nagents.web.app import create_app
from nagents.web.settings import SettingsValues
from tests.test_web import URL
from tests.test_web import ControlledHarness
from tests.test_web import LiveStream
from tests.test_web import client_app
from tests.test_web import no_guarded_workspace_io as no_guarded_workspace_io


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
                }
                assert body["values"] == body["defaults"] == SettingsValues.current(harness).model_dump()
                assert set(body["values"]) == set(SettingsValues.model_fields)
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


def test_settings_persist_fresh_lifespan_and_reset_clears_override(tmp_path: Path) -> None:
    async def check() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/settings", headers=headers)).json()
            session_id = harnesses[0].session_id
            db_path = harnesses[0].agent.session.db_path
            await harnesses[0].agent.session.add_message(session_id, Message(role="user", content="PVC history"))
            values = {
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
                json={"revision": initial["revision"], "values": {**initial["values"], "model": "old-saved-model"}},
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
            assert SettingsValues.current(harness).model_dump() == before["values"]
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
                    json={"revision": before["revision"], "values": {**before["values"], "model": "previous-model"}},
                    headers=headers,
                )
                assert saved.status_code == 200
                before = saved.json()
            values = {**before["values"], "model": "committed-model", "agent": "reviewer", "max_tool_rounds": 2}
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
            assert SettingsValues.current(harness).model_dump() == after["values"]
            assert harness.agent.max_tool_rounds == after["values"]["max_tool_rounds"]
            assert (await client.post("/api/sessions/new", json={}, headers=headers)).status_code == 200
        async with client_app(tmp_path) as (_, client, headers, _):
            assert (await client.get("/api/settings", headers=headers)).json()["values"] == after["values"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "corruption", ["json", "version", "unknown", "missing", "removed-profile", "oversized", "nonfinite", "revision"]
)
def test_settings_corrupt_saved_row_fails_closed(tmp_path: Path, corruption: str) -> None:
    async def check() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            before = (await client.get("/api/settings", headers=headers)).json()
            db_path = harnesses[0].agent.session.db_path
        values = before["values"]
        version, revision = 1, "a" * 64
        if corruption == "unknown":
            values["trust_project"] = True
        elif corruption == "missing":
            del values["agent"]
        elif corruption == "removed-profile":
            values["agent"] = "SECRET-removed-profile"
        elif corruption == "nonfinite":
            values["shell_timeout"] = float("nan")
        elif corruption == "version":
            version = 2
        elif corruption == "revision":
            revision = "SECRET-invalid-revision"
        payload = json.dumps(values)
        if corruption == "json":
            payload = "{SECRET-invalid-json"
        elif corruption == "oversized":
            payload = " " * 16385 + payload
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO ngn_web_settings (id, version, values_json, revision) VALUES (1, ?, ?, ?)",
                (version, payload, revision),
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
