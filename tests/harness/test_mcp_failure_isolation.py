"""Unavailable MCP servers never restart at every model/tool boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.followups import RunInput
from nagents.harness.types import Notice
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig
from nagents.mcp.client import MCPClient
from nagents.types import ToolCall
from tests.harness.test_resource_reload import server
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import ApprovalRequest
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_posix]


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


def failing_server(tmp_path: Path, *, hang: bool = False) -> MCPServerConfig:
    source = tmp_path / "unavailable.py"
    source.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "with Path('failed-starts').open('a') as f: f.write(str(os.getpid()) + '\\n')\n"
        "if not Path('repaired').exists() and not os.environ.get('FIXTURE_RECOVER'):\n"
        + (
            "    for line in sys.stdin: pass\n    sys.exit(0)\n"
            if hang
            else "    print('Missing required environment variable PRIVATE_ENV_KEY=PRIVATE_VALUE', file=sys.stderr)\n"
            "    sys.exit(2)\n"
        )
        + "exec(Path('server.py').read_text())\n"
    )
    return MCPServerConfig("unavailable", sys.executable, ["-u", str(source)], cwd=str(tmp_path))


async def approve(request: ApprovalRequest) -> bool:
    return True


async def done(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="finished")


@pytest.mark.parametrize("hang", [False, True])
async def test_failure_is_skipped_while_tools_and_followup_messages_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, hang: bool
) -> None:
    healthy = server(tmp_path)
    failed = failing_server(tmp_path, hang=hang)
    initialize = MCPClient._initialize
    failed_attempts = 0

    async def short_timeout(client: MCPClient) -> None:
        nonlocal failed_attempts
        if client.config.name == "unavailable":
            failed_attempts += 1
            if hang:
                # Start the intentional protocol timeout only after the fixture
                # process is ready, independently of loaded-runner startup speed.
                async with asyncio.timeout(HANG_GUARD):
                    while (
                        not (tmp_path / "failed-starts").exists()
                        or len((tmp_path / "failed-starts").read_text().splitlines()) < failed_attempts
                    ):
                        await asyncio.sleep(0.01)
                client.request_timeout = 0.03
        await initialize(client)

    monkeypatch.setattr(MCPClient, "_initialize", short_timeout)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            assert harness.submit_followup(harness.session_id, RunInput("queued while responding", "queued"))
            assert harness.followups.pending
            yield ToolCallEvent(id="working", name="mcp__fixture__echo", arguments={})
        else:
            assert any(message.role == "user" and message.content == "queued while responding" for message in messages)
            yield TextDoneEvent(text="finished")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (healthy, failed)
    harness.approval_handler = approve
    try:
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(harness.initialize(), HANG_GUARD)
            first_client = harness.resources.current.manager._clients["fixture"]
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            events += await asyncio.wait_for(collect(harness), HANG_GUARD)
        assert len(providers[0].requests) == 3
        assert any(isinstance(event, ToolResultEvent) and event.result == "one:{}" for event in events)
        assert sum(isinstance(event, DoneEvent) for event in events) == 2
        assert len([event for event in events if isinstance(event, Notice) and "unavailable" in event.text]) == 1
        assert harness.resources.current.manager._clients["fixture"] is first_client
        assert len((tmp_path / "starts").read_text().splitlines()) == 1
        assert len((tmp_path / "failed-starts").read_text().splitlines()) == 1
        assert failed_attempts == 1
        snapshot = harness.resources.mcp_diagnostics()
        assert snapshot.status == "partial"
        assert snapshot.connected_servers == 1 and snapshot.configured_servers == 2
        assert snapshot.failed_servers[0].category == ("timeout" if hang else "missing_environment")
        assert "PRIVATE" not in json.dumps(snapshot.snapshot()) + caplog.text + str(harness.diagnostics)
        assert sum("MCP server unavailable:" in record.message for record in caplog.records) == 1
    finally:
        await harness.close()
    assert (tmp_path / "starts").read_text() == (tmp_path / "stops").read_text()


@pytest.mark.parametrize("recovery", ["retry", "config", "environment", "source"])
async def test_recovery_retries_only_when_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
) -> None:
    healthy = server(tmp_path)
    failed = failing_server(tmp_path)
    harness, _ = setup_harness(tmp_path, monkeypatch, done)
    harness.config.mcp_servers = (healthy, failed)
    try:
        await harness.initialize()
        client = harness.resources.current.manager._clients["fixture"]
        await harness.resources.reload()
        assert len((tmp_path / "failed-starts").read_text().splitlines()) == 1
        if recovery == "retry":
            (tmp_path / "repaired").touch()
            harness.resources.request_mcp_retry("unavailable")
            harness.resources.request_mcp_retry("unavailable")
            assert harness.resources.mcp_diagnostics().failed_servers[0].retry_pending
        elif recovery == "config":
            harness.config.mcp_servers = (healthy, replace(failed, env={"FIXTURE_RECOVER": "PRIVATE_SECRET"}))
        elif recovery == "environment":
            monkeypatch.setenv("FIXTURE_RECOVER", "PRIVATE_SECRET")
        else:
            (tmp_path / "repaired").touch()
            source = tmp_path / "unavailable.py"
            source.write_text(source.read_text() + "\n# fixed installation\n")
        await harness.resources.reload()
        assert not harness.resources.mcp_diagnostics().failed_servers
        assert harness.resources.mcp_diagnostics().status == "loaded"
        assert len((tmp_path / "failed-starts").read_text().splitlines()) == 2
        if recovery != "environment":
            assert harness.resources.current.manager._clients["fixture"] is client
        result = await harness.resources.current.manager.execute_tool(
            ToolCall("recovered", "mcp__unavailable__echo", {})
        )
        assert result == "one:{}"
        with pytest.raises(ValueError, match="not unavailable"):
            harness.resources.request_mcp_retry("unavailable")
    finally:
        await harness.close()


async def test_healthy_addition_survives_other_server_discovery_failure_and_old_call_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    original_list = MCPClient.list_tools

    async def list_tools(client: MCPClient) -> list[dict[str, object]]:
        if client.config.name == "bad_discovery":
            raise ValueError("PRIVATE_SCHEMA_VALUE")
        return await original_list(client)

    monkeypatch.setattr(MCPClient, "list_tools", list_tools)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            harness.config.mcp_servers = (
                configured,
                replace(configured, name="new"),
                replace(configured, name="bad_discovery"),
            )
            yield ToolCallEvent(id="pinned", name="mcp__fixture__echo", arguments={})
        else:
            assert "mcp__new__echo" in provider.schemas[-1]
            assert "mcp__bad_discovery__echo" not in provider.schemas[-1]
            yield TextDoneEvent(text="finished")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (configured,)
    harness.approval_handler = approve
    try:
        events = await collect(harness)
        assert any(isinstance(event, ToolResultEvent) and event.result == "one:{}" for event in events)
        failure = harness.resources.mcp_diagnostics().failed_servers[0]
        assert failure.stage == "mcp_discover" and failure.category == "invalid_configuration"
        assert len((tmp_path / "starts").read_text().splitlines()) == 3
        assert len((tmp_path / "stops").read_text().splitlines()) == 1
    finally:
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


async def test_rejected_generation_releases_shared_connection_without_disconnecting_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    harness, _ = setup_harness(tmp_path, monkeypatch, done)
    harness.config.mcp_servers = (configured,)
    plugin = tmp_path / "invalid.py"
    plugin.write_text("def setup(harness):\n    harness.agent.max_tool_rounds = 0\n")
    try:
        await harness.initialize()
        client = harness.resources.current.manager._clients["fixture"]
        harness.config.plugins = (str(plugin) + ":setup",)
        await harness.resources.reload()
        assert harness.resources.last_error and client.is_connected
        assert harness.resources.current.manager._connections["fixture"].references == 1
        assert (
            await harness.resources.current.manager.execute_tool(ToolCall("live", "mcp__fixture__echo", {})) == "one:{}"
        )
    finally:
        await harness.close()
    assert (tmp_path / "starts").read_text() == (tmp_path / "stops").read_text()


async def test_cancelling_discovery_releases_only_the_candidate_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    harness, _ = setup_harness(tmp_path, monkeypatch, done)
    harness.config.mcp_servers = (configured,)
    entered = asyncio.Event()
    original_list = MCPClient.list_tools
    waiting: list[MCPClient] = []

    async def wait(client: MCPClient) -> list[dict[str, object]]:
        if client.config.name == "waiting":
            waiting.append(client)
            entered.set()
            await asyncio.Event().wait()
        return await original_list(client)

    try:
        await harness.initialize()
        owner = harness.resources.current.manager._clients["fixture"]
        harness.config.mcp_servers = (configured, replace(configured, name="waiting"))
        monkeypatch.setattr(MCPClient, "list_tools", wait)
        reload = asyncio.create_task(harness.resources.reload())
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        reload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload
        assert owner.is_connected and waiting[0]._process is None
        assert harness.resources.current.manager._connections["fixture"].references == 1
        assert not harness.resources.mcp_diagnostics().failed_servers
    finally:
        await harness.close()


async def test_manager_shared_clients_are_released_once_and_pinned_calls_remain_usable(tmp_path: Path) -> None:
    configured = server(tmp_path)
    first, second = MCPManager([]), MCPManager([])
    await first.add_server(configured, strict=True)
    second.retain_server(first, "fixture")
    client = first._clients["fixture"]
    try:
        await first.disconnect_all()
        await first.disconnect_all()
        assert client.is_connected
        assert await second.execute_tool(ToolCall("retained", "mcp__fixture__echo", {})) == "one:{}"
        assert second._connections["fixture"].references == 1
    finally:
        await second.disconnect_all()
    assert not client.is_connected
    assert (tmp_path / "starts").read_text() == (tmp_path / "stops").read_text()


async def test_tool_list_notification_refreshes_schema_without_restarting_or_changing_pinned_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    source = tmp_path / "server.py"
    source.write_text(
        source.read_text().replace(
            "        else: result = {}",
            '            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)\n'
            "        else: result = {}",
        )
    )
    expected = {"type": "object", "properties": {"new_argument": {"type": "string"}}}
    harness, _ = setup_harness(tmp_path, monkeypatch, done)
    harness.config.mcp_servers = (configured,)
    try:
        await harness.initialize()
        original = harness.agent.tool_registry.get("mcp__fixture__echo")
        assert original is not None
        client = harness.resources.current.manager._clients["fixture"]
        (tmp_path / "schema.json").write_text(json.dumps(expected))
        await client.call_tool("echo", {})
        assert client.tools_revision == 1
        await harness.resources.reload()
        updated = harness.agent.tool_registry.get("mcp__fixture__echo")
        assert updated is not None and updated.parameters == expected
        assert original.parameters != expected
        assert harness.resources.current.manager._clients["fixture"] is client
        assert len((tmp_path / "starts").read_text().splitlines()) == 1
        assert not harness.resources.current.manager.server_tools_changed("fixture")
    finally:
        await harness.close()


async def test_dirty_discovery_failure_is_skipped_and_retains_unrelated_healthy_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    harness, _ = setup_harness(tmp_path, monkeypatch, done)
    harness.config.mcp_servers = (configured, replace(configured, name="dirty"))
    original_list = MCPClient.list_tools
    attempts = 0

    async def changed(client: MCPClient) -> list[dict[str, object]]:
        nonlocal attempts
        if client.config.name == "dirty":
            attempts += 1
            raise TimeoutError("PRIVATE_TIMEOUT")
        return await original_list(client)

    try:
        await harness.initialize()
        healthy = harness.resources.current.manager._clients["fixture"]
        dirty = harness.resources.current.manager._clients["dirty"]
        dirty.tools_revision += 1
        monkeypatch.setattr(MCPClient, "list_tools", changed)
        await harness.resources.reload()
        await harness.resources.reload()
        assert attempts == 1
        assert harness.resources.current.manager._clients["fixture"] is healthy
        assert not dirty.is_connected
        failure = harness.resources.mcp_diagnostics().failed_servers[0]
        assert failure.name == "dirty" and failure.stage == "mcp_discover" and failure.category == "timeout"
    finally:
        await harness.close()


async def test_dead_connection_attempts_recovery_once_then_stays_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    harness, _ = setup_harness(tmp_path, monkeypatch, done)
    harness.config.mcp_servers = (configured,)
    attempts = 0

    async def fail(client: MCPClient) -> None:
        nonlocal attempts
        attempts += 1
        raise ConnectionError("PRIVATE_CONNECTION_DETAILS")

    try:
        await harness.initialize()
        client = harness.resources.current.manager._clients["fixture"]
        await client.disconnect()
        monkeypatch.setattr(MCPClient, "connect", fail)
        await harness.resources.reload()
        await harness.resources.reload()
        assert attempts == 1
        assert harness.resources.mcp_diagnostics().failed_servers[0].category == "disconnected"
        assert not harness.resources.current.manager._clients
    finally:
        await harness.close()


async def test_new_host_owner_inherits_failed_state_but_revalidates_changed_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = server(tmp_path)
    failed = failing_server(tmp_path)
    original, _ = setup_harness(tmp_path, monkeypatch, done)
    original.config.mcp_servers = (failed,)
    replacement, _ = setup_harness(tmp_path, monkeypatch, done)
    replacement.config.mcp_servers = (failed,)
    try:
        await original.initialize()
        original.resources.request_mcp_retry("unavailable")
        replacement.resources.inherit_mcp_failures(original.resources)
        assert replacement.resources.mcp_retry_matches("unavailable", original.resources)
        assert not replacement.resources.mcp_diagnostics().failed_servers[0].retry_pending
        await replacement.initialize()
        assert len((tmp_path / "failed-starts").read_text().splitlines()) == 1
        replacement.config.mcp_servers = (healthy, replace(failed, env={"FIXTURE_RECOVER": "PRIVATE_ROTATION"}))
        await replacement.resources.reload()
        assert len((tmp_path / "failed-starts").read_text().splitlines()) == 2
        assert not replacement.resources.mcp_diagnostics().failed_servers
        assert original.resources.mcp_diagnostics().failed_servers
        assert not replacement.resources.mcp_retry_matches("unavailable", original.resources)
    finally:
        await replacement.close()
        await original.close()
