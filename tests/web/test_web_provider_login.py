"""Device-login browser data is bounded, token-free, and owned by the server."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents._async import join_owned
from nagents.harness.auth import DeviceAuthorization
from nagents.harness.config import HarnessConfig
from nagents.harness.connection import build_provider
from nagents.harness.providers import ProviderProfile
from nagents.provider import OpenAIProvider
from nagents.web import provider_login
from tests.support.channels import site
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from nagents.web.provider_login import LoginSnapshot
    from nagents.web.provider_login import ProviderLogin
    from tests.support.channels import Site

pytestmark = pytest.mark.requires_posix


def login_service(app: Site) -> ProviderLogin:
    return cast("ProviderLogin", cast("FastAPI", app.client.app).state.provider_login)


def wait_status(app: Site, status: str) -> LoginSnapshot:
    service = login_service(app)

    async def wait() -> LoginSnapshot:
        async with asyncio.timeout(HANG_GUARD):
            while service.status != status:
                await asyncio.sleep(0.001)
        return service.snapshot()

    assert app.client.portal is not None
    return app.client.portal.call(wait)


def pending_login(app: Site, monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    assert app.client.portal is not None
    release: asyncio.Event = app.client.portal.call(asyncio.Event)

    async def start() -> DeviceAuthorization:
        return DeviceAuthorization(
            "https://auth.openai.com/codex/device", "TEST-CODE", "private-device-id", 1, monotonic() + 60
        )

    async def complete(authorization: DeviceAuthorization) -> None:
        assert authorization.device_auth_id == "private-device-id"
        await release.wait()

    monkeypatch.setattr(app.state.harness.openai_auth, "start_device_login", start)
    monkeypatch.setattr(app.state.harness.openai_auth, "complete_device_login", complete)
    return release


def test_login_completes_selected_provider_and_hides_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with site(tmp_path, monkeypatch) as app:
        release = pending_login(app, monkeypatch)
        # The shared site fixture substitutes a FakeProvider during startup.
        # Exercise the real replacement factory for the user-initiated login.
        monkeypatch.setattr("nagents.harness.runtime.build_provider", build_provider)
        previous_provider = app.state.harness.agent.provider
        assert app.client.get("/api/login/chatgpt", headers=app.headers).json()["status"] == "idle"
        response = app.client.post("/api/login/chatgpt", headers=app.headers, json={})
        assert response.status_code == 200
        pending = wait_status(app, "pending")
        assert pending["user_code"] == "TEST-CODE" and pending["expires_in"] > 0
        duplicate = app.client.post("/api/login/chatgpt", headers=app.headers, json={})
        assert duplicate.json()["id"] == pending["id"]
        assert "private-device-id" not in response.text + duplicate.text
        assert app.client.post("/api/sessions/new", headers=app.headers, json={}).status_code == 409
        assert app.client.portal is not None
        app.client.portal.call(release.set)
        completed = wait_status(app, "completed")
        assert completed["user_code"] == completed["verification_url"] == ""
        assert not app.state.mutating
        assert app.state.harness.config.provider == "fixture"
        assert app.state.harness.config.provider_profile().auth == "chatgpt"
        assert app.state.harness.agent.provider is not previous_provider
        assert isinstance(app.state.harness.agent.provider, OpenAIProvider)
        assert app.state.harness.agent.provider.uses_chatgpt_auth
        assert app.state.harness.provider_store.load().providers["fixture"].auth == "chatgpt"
        assert app.history(app.main)["history"] == []
        assert "private-device-id" not in caplog.text and "TEST-CODE" not in caplog.text


def test_login_cancel_is_scoped_and_leaves_provider_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        pending_login(app, monkeypatch)
        assert app.client.post("/api/login/chatgpt", headers=app.headers, json={}).status_code == 200
        pending = wait_status(app, "pending")
        assert (
            app.client.post("/api/login/chatgpt/cancel", headers=app.headers, json={"id": "stale"}).status_code == 409
        )
        cancelled = app.client.post("/api/login/chatgpt/cancel", headers=app.headers, json={"id": pending["id"]})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["user_code"] == ""
        assert not app.state.mutating and app.state.harness.config.provider_profile().auth == "api-key"
        assert app.client.post("/api/sessions/new", headers=app.headers, json={}).status_code == 200


def test_login_error_is_safe_and_shutdown_joins_pending_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        pending_login(app, monkeypatch)

        async def fail(authorization: DeviceAuthorization) -> None:
            raise RuntimeError("private-access-token")

        monkeypatch.setattr(app.state.harness.openai_auth, "complete_device_login", fail)
        assert app.client.post("/api/login/chatgpt", headers=app.headers, json={}).status_code == 200
        failed = wait_status(app, "failed")
        assert failed["user_code"] == "" and "private-access-token" not in str(failed)
        assert not app.state.mutating
        pending_login(app, monkeypatch)
        assert app.client.post("/api/login/chatgpt", headers=app.headers, json={}).status_code == 200
        wait_status(app, "pending")
        service = login_service(app)
    assert service.status == "cancelled" and service.closed
    assert all(task.done() for task in service.tasks)
    assert service.code == ""


def test_login_requires_same_origin_and_server_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        assert app.client.get("/api/login/chatgpt").status_code == 403
        assert app.client.post("/api/login/chatgpt", headers={"X-Ngn-Token": app.token}, json={}).status_code == 403
        assert app.client.post("/api/login/chatgpt", headers=app.headers, json={"api_key": "no"}).status_code == 422


@pytest.mark.parametrize("condition", ["busy", "live", "demo", "custom-provider"])
def test_login_rejects_conflicting_or_unsupported_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        service = login_service(app)
        called = False

        async def unexpected_start() -> DeviceAuthorization:
            nonlocal called
            called = True
            raise AssertionError("Must reject before network authorization")

        monkeypatch.setattr(app.state.harness.openai_auth, "start_device_login", unexpected_start)
        if condition == "busy":
            app.state.mutating = True
        elif condition == "live":
            monkeypatch.setattr(service, "live_active", lambda: "active-voice")
        elif condition == "demo":
            app.state.harness.config.demo = True
        else:
            store = app.state.harness.provider_store.global_store
            before = store.load()
            before.providers["fixture"] = ProviderProfile(kind="anthropic")
            store.save(before, expected=before.revision)
        try:
            result = app.client.post("/api/login/chatgpt", headers=app.headers, json={})
            assert result.status_code == 409, result.text
            assert not called
        finally:
            app.state.mutating = False


def test_login_creates_a_named_default_when_unconfigured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        async with client_app(tmp_path, config=config, controlled=False) as (app, client, headers, harnesses):
            harness = harnesses[0]
            assert not harness.config.provider

            async def start() -> DeviceAuthorization:
                return DeviceAuthorization(
                    "https://auth.openai.com/codex/device", "TEST-CODE", "private-device-id", 1, monotonic() + 60
                )

            async def complete(authorization: DeviceAuthorization) -> None:
                assert authorization.device_auth_id == "private-device-id"

            monkeypatch.setattr(harness.openai_auth, "start_device_login", start)
            monkeypatch.setattr(harness.openai_auth, "complete_device_login", complete)
            assert (await client.post("/api/login/chatgpt", headers=headers, json={})).status_code == 200
            service = cast("ProviderLogin", app.state.provider_login)
            async with asyncio.timeout(HANG_GUARD):
                while service.status in {"starting", "pending"}:
                    await asyncio.sleep(0.001)
            assert service.status == "completed", service.snapshot()
            saved = harness.provider_store.load_scope("workspace")
            assert saved.active == "chatgpt" and saved.providers["chatgpt"].auth == "chatgpt"
            assert str(harness.config.provider) == "chatgpt"

    asyncio.run(check())


def test_login_expires_and_clears_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        pending_login(app, monkeypatch)

        async def start() -> DeviceAuthorization:
            return DeviceAuthorization(
                "https://auth.openai.com/codex/device", "TEST-CODE", "private-device-id", 1, monotonic() + 0.05
            )

        monkeypatch.setattr(app.state.harness.openai_auth, "start_device_login", start)
        assert app.client.post("/api/login/chatgpt", headers=app.headers, json={}).status_code == 200
        expired = wait_status(app, "failed")
        assert expired["user_code"] == expired["verification_url"] == ""
        assert not app.state.mutating


@pytest.mark.parametrize("cancel_after_commit", [False, True])
def test_login_completion_waits_for_idle_release_even_after_late_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_after_commit: bool
) -> None:
    with site(tmp_path, monkeypatch) as app:
        release_login = pending_login(app, monkeypatch)
        assert app.client.portal is not None
        applied: asyncio.Event = app.client.portal.call(asyncio.Event)
        release_apply: asyncio.Event = app.client.portal.call(asyncio.Event)

        async def join_with_completion_gate(task: asyncio.Task[None]) -> None:
            await join_owned(task)
            # Gate only the owned apply, not the outer login task joined by
            # cancel/close. This exposes the exact scheduling gap deterministically.
            if task.get_name() != "ngn-web-device-login":
                applied.set()
                await release_apply.wait()

        monkeypatch.setattr(provider_login, "join_owned", join_with_completion_gate)
        assert app.client.post("/api/login/chatgpt", headers=app.headers, json={}).status_code == 200
        pending = wait_status(app, "pending")
        app.client.portal.call(release_login.set)

        async def wait_applied() -> None:
            async with asyncio.timeout(HANG_GUARD):
                await applied.wait()

        app.client.portal.call(wait_applied)
        try:
            assert app.state.mutating
            assert app.state.harness.config.provider_profile().auth == "chatgpt"
            before_release = app.client.get("/api/login/chatgpt", headers=app.headers).json()
            assert before_release["status"] != "completed"
            assert app.client.post("/api/sessions/new", headers=app.headers, json={}).status_code == 409
            if cancel_after_commit:
                result = app.client.post("/api/login/chatgpt/cancel", headers=app.headers, json={"id": pending["id"]})
                assert result.status_code == 200 and result.json()["status"] == "completed"
            else:
                app.client.portal.call(release_apply.set)
            completed = wait_status(app, "completed")
            assert completed["user_code"] == completed["verification_url"] == ""
            assert not app.state.mutating
            assert app.client.post("/api/sessions/new", headers=app.headers, json={}).status_code == 200
        finally:
            app.client.portal.call(release_apply.set)
