"""MCP recovery keeps healthy tools, running work and resource ownership intact."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from nagents.harness.config import HarnessConfig
from nagents.mcp import MCPError
from nagents.mcp import MCPManager
from tests.harness.test_resource_reload import server
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from nagents.mcp import MCPServerConfig

pytestmark = pytest.mark.requires_posix


@dataclass
class Startup:
    attempts: Counter[str] = field(default_factory=Counter)
    recover: bool = False
    block: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


def configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[HarnessConfig, Startup]:
    healthy = server(tmp_path)
    probe = Startup()
    original = MCPManager.add_server

    async def add(
        manager: MCPManager, configured: MCPServerConfig, *, strict: bool = False
    ) -> list[Callable[..., object]]:
        probe.attempts[configured.name] += 1
        if configured.name == "broken":
            if probe.block:
                probe.entered.set()
                await probe.release.wait()
            if not probe.recover:
                raise MCPError("PRIVATE_SUBPROCESS_SECRET", stage="mcp_start", category="missing_environment")
        return await original(manager, configured, strict=strict)

    monkeypatch.setattr(MCPManager, "add_server", add)
    return HarnessConfig(
        tmp_path, data_dir=tmp_path / "data", mcp_servers=(healthy, replace(healthy, name="broken"))
    ), probe


@pytest.mark.asyncio
async def test_retry_authentication_validation_failure_and_recovery_leave_healthy_connection_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, probe = configuration(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
        harness = harnesses[0]
        healthy = harness.resources.current.manager._clients["fixture"]
        before = (await client.get("/api/tools", headers=headers)).json()
        assert before["mcp_diagnostics"]["status"] == "partial"
        assert before["mcp_diagnostics"]["connected_servers"] == 1
        assert any(tool["name"] == "mcp__fixture__echo" for tool in before["tools"])
        assert (await client.post("/api/tools/mcp/retry", json={"server": "broken"})).status_code == 403
        assert (
            await client.post("/api/tools/mcp/retry", headers=headers, json={"server": "fixture"})
        ).status_code == 409
        assert (
            await client.post("/api/tools/mcp/retry", headers=headers, json={"server": "unknown"})
        ).status_code == 409
        assert (
            await client.post(
                "/api/tools/mcp/retry", headers=headers, json={"server": "broken", "command": "UNTRUSTED"}
            )
        ).status_code == 422
        assert probe.attempts == {"fixture": 1, "broken": 1}
        failed = await client.post("/api/tools/mcp/retry", headers=headers, json={"server": "broken"})
        assert failed.status_code == 200
        assert failed.json()["mcp_retry"] == {"server": "broken", "status": "completed"}
        failure = failed.json()["mcp_diagnostics"]["failed_servers"][0]
        assert failure["category"] == "missing_environment" and not failure["retry_pending"]
        assert "PRIVATE_SUBPROCESS_SECRET" not in failed.text
        assert harness.resources.current.manager._clients["fixture"] is healthy
        probe.recover = True
        recovered = await client.post("/api/tools/mcp/retry", headers=headers, json={"server": "broken"})
        assert recovered.status_code == 200
        assert recovered.json()["mcp_diagnostics"]["failed_servers"] == []
        assert recovered.json()["mcp_diagnostics"]["connected_servers"] == 2
        assert any(tool["name"] == "mcp__broken__echo" for tool in recovered.json()["tools"])
        assert harness.resources.current.manager._clients["fixture"] is healthy
        assert probe.attempts == {"fixture": 1, "broken": 3}


@pytest.mark.asyncio
async def test_retry_during_active_run_is_queued_without_waiting_or_replacing_healthy_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, probe = configuration(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (app, client, headers, harnesses):
        state = app.state.web
        harness = harnesses[0]
        healthy = harness.resources.current.manager._clients["fixture"]
        stream = LiveStream(app, headers, state.selected_session_id, "working")
        await stream.event("text_chunk")
        active = state.run_for(state.selected_session_id)
        assert active is not None
        try:
            probe.recover = True
            for _ in range(2):
                result = await client.post("/api/tools/mcp/retry", headers=headers, json={"server": "broken"})
                assert result.status_code == 200
                assert result.json()["mcp_retry"]["status"] == "queued"
                assert result.json()["mcp_diagnostics"]["failed_servers"][0]["retry_pending"]
            assert probe.attempts == {"fixture": 1, "broken": 1}
            assert not active.task.done() and not state.mutating
            assert harness.resources.current.manager._clients["fixture"] is healthy
            # The run's next ordinary resource boundary, not the HTTP handler,
            # consumes the queued attempt exactly once.
            await harness.resources.reload()
            assert probe.attempts == {"fixture": 1, "broken": 2}
            assert not harness.resources.mcp_diagnostics().failed_servers
            assert harness.resources.current.manager._clients["fixture"] is healthy
        finally:
            await client.post("/api/cancel", headers=headers, json={"run_id": active.id})
            await stream.disconnect()


@pytest.mark.asyncio
async def test_idle_retry_allows_message_admission_and_settings_detachment_while_holding_resource_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, probe = configuration(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (app, client, headers, harnesses):
        state = app.state.web
        original = harnesses[0]
        close = AsyncMock(wraps=original.resources.aclose)
        monkeypatch.setattr(original.resources, "aclose", close)
        probe.block = True
        retry = asyncio.create_task(client.post("/api/tools/mcp/retry", headers=headers, json={"server": "broken"}))
        async with asyncio.timeout(HANG_GUARD):
            await probe.entered.wait()
        assert not state.mutating
        assert original in state.executions._leases
        # This admitted run must not be rejected by a global settings lock.
        stream = LiveStream(app, headers, state.selected_session_id, "working")
        await stream.event("text_chunk")
        active = state.run_for(state.selected_session_id)
        assert active is not None
        try:
            # Only the already-owned attempt stays blocked. A replacement
            # settings runtime may initialize its own accepted configuration.
            probe.block = False
            attempts_before_settings = probe.attempts["broken"]
            settings = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": settings["revision"],
                    "values": {**settings["values"], "model": "next-model"},
                },
            )
            assert saved.status_code == 200, saved.text
            assert state.harness is not original
            assert probe.attempts["broken"] == attempts_before_settings, (
                "Detaching settings inherits failed-server state"
            )
            close.assert_not_awaited()
            probe.release.set()
            async with asyncio.timeout(HANG_GUARD):
                result = await retry
            assert result.status_code == 200
            close.assert_not_awaited()  # The admitted run still owns it.
        finally:
            probe.release.set()
            await retry
            await client.post("/api/cancel", headers=headers, json={"run_id": active.id})
            await stream.disconnect()
    close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", ["request", "shutdown"])
async def test_cancelled_idle_retry_releases_lease_and_shutdown_closes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    config, probe = configuration(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (app, client, headers, harnesses):
        harness = harnesses[0]
        close = AsyncMock(wraps=harness.resources.aclose)
        monkeypatch.setattr(harness.resources, "aclose", close)
        probe.block = True
        retry = asyncio.create_task(client.post("/api/tools/mcp/retry", headers=headers, json={"server": "broken"}))
        async with asyncio.timeout(HANG_GUARD):
            await probe.entered.wait()
        if cancellation == "request":
            retry.cancel()
        else:
            await app.state.web.executions.close()
        with pytest.raises(asyncio.CancelledError):
            await retry
        assert not app.state.web.executions._leases
        assert not app.state.web.mutating
        assert harness.resources.current.manager._clients.get("fixture") is not None
        close.assert_not_awaited()
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_and_parallel_new_lanes_inherit_known_failures_without_retrying_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, probe = configuration(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (app, client, headers, harnesses):
        state = app.state.web
        streams: list[LiveStream] = []
        try:
            first = LiveStream(app, headers, state.selected_session_id, "first")
            streams.append(first)
            await first.event("text_chunk")
            settings = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": settings["revision"],
                    "values": {**settings["values"], "model": "next-model"},
                },
            )
            assert saved.status_code == 200, saved.text
            assert probe.attempts["broken"] == 1
            # The first additional chat owns the detached primary; the next
            # creates a genuinely new execution lane with inherited failures.
            for prompt in ("second", "third"):
                new = await client.post("/api/sessions/new", headers=headers, json={})
                assert new.status_code == 200, new.text
                stream = LiveStream(app, headers, new.json()["session_id"], prompt)
                streams.append(stream)
                await stream.event("text_chunk")
            assert len(harnesses) == 3
            assert probe.attempts["broken"] == 1
            assert all(harness.resources.mcp_diagnostics().failed_servers for harness in harnesses)
        finally:
            for stream in streams:
                if active := state.run_for(stream.session_id):
                    await client.post("/api/cancel", headers=headers, json={"run_id": active.id})
                await stream.disconnect()


@pytest.mark.asyncio
async def test_retry_does_not_mark_retained_owner_with_different_accepted_server_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, probe = configuration(tmp_path, monkeypatch)
    async with client_app(tmp_path, config=config) as (app, client, headers, _):
        state = app.state.web
        other = ControlledHarness(
            replace(config, mcp_servers=(replace(config.mcp_servers[1], env={"DISTINCT": "PRIVATE_OLD"}),))
        )
        await state.executions.borrow_auth(other)
        await other.initialize(create_session=False)
        state.executions.owners["retained"] = other
        mark = AsyncMock()
        original_request = other.resources.request_mcp_retry
        calls: list[str] = []

        def request(name: str) -> None:
            calls.append(name)
            original_request(name)

        monkeypatch.setattr(other.resources, "request_mcp_retry", request)
        monkeypatch.setattr(other.resources, "reload", mark)
        try:
            result = await client.post("/api/tools/mcp/retry", headers=headers, json={"server": "broken"})
            assert result.status_code == 200
            assert result.json()["mcp_retry"]["status"] == "completed"
            assert not calls
            mark.assert_not_awaited()
            assert not other.resources.mcp_diagnostics().failed_servers[0].retry_pending
            assert "PRIVATE_OLD" not in result.text
            assert probe.attempts["broken"] == 3
        finally:
            await state.executions.dispose("retained")
