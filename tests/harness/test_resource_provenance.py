"""Configuration freshness follows trusted logical locations, not inode generations."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import load_config
from nagents.harness.resource_config import read_resource_configuration
from tests.harness.test_resource_reload import server
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import ApprovalRequest
    from nagents.types import Message
    from tests.support.providers import FakeProvider


def rotate(volume: Path, target: str) -> None:
    replacement = volume / "..next"
    replacement.symlink_to(target, target_is_directory=True)
    os.replace(replacement, volume / "..data")


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_configmap_rotation_updates_next_request_and_retains_old_called_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = server(tmp_path)
    volume = tmp_path / "configmap"
    volume.mkdir()
    for generation, name in (("..generation-one", "before"), ("..generation-two", "after")):
        directory = volume / generation
        directory.mkdir()
        (directory / "config.json").write_text(
            json.dumps(
                {"mcp_servers": {name: {"command": configured.command, "args": configured.args, "cwd": configured.cwd}}}
            )
        )
    rotate(volume, "..generation-one")
    source = volume / "config.json"
    source.symlink_to("..data/config.json")
    config = load_config(tmp_path, source)
    assert config.config_paths[-1] == source and config.resource_paths[-1] == source
    assert read_resource_configuration(config).sources[-1].resolved == volume / "..generation-one" / "config.json"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            assert "mcp__before__echo" in provider.schemas[-1]
            rotate(volume, "..generation-two")
            shutil.rmtree(volume / "..generation-one")
            yield ToolCallEvent(id="old", name="mcp__before__echo", arguments={})
        else:
            assert messages[-1].content == "one:{}"
            assert "mcp__after__echo" in provider.schemas[-1]
            assert "mcp__before__echo" not in provider.schemas[-1]
            yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.resource_paths = config.resource_paths

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    session = harness.session_id
    try:
        events = await collect(harness)
        assert harness.session_id == session
        assert [item.result for item in events if isinstance(item, ToolResultEvent)] == ["one:{}"]
        assert read_resource_configuration(config).sources[-1].resolved == volume / "..generation-two" / "config.json"
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_late_untrusted_project_is_reported_once_and_never_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    trusted = tmp_path / "trusted.json"
    trusted.write_text("{}")
    project = tmp_path / ".ngn" / "config.json"
    (tmp_path / "fixture.txt").write_text("data")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            project.parent.mkdir(exist_ok=True)
            project.write_text(
                json.dumps(
                    {
                        "mcp_servers": {
                            "untrusted": {"command": "MUST_NOT_RUN", "env": {"TOKEN": "PROJECT_SECRET_VALUE"}}
                        }
                    }
                )
            )
            yield ToolCallEvent(id="read", name="read_file", arguments={"path": "fixture.txt"})
        else:
            assert not any(name.startswith("mcp__") for name in provider.schemas[-1])
            yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.resource_paths = (trusted,)
    try:
        with caplog.at_level(logging.INFO, logger="nagents.harness.resources"):
            events = await collect(harness)
            await harness.resources.reload()
        warnings = [
            record.getMessage()
            for record in caplog.records
            if "Ignoring untrusted project configuration" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert str(project) in warnings[0] and str(trusted) in warnings[0]
        assert "--trust-project" in warnings[0] and "approved declaration" in warnings[0]
        assert "PROJECT_SECRET_VALUE" not in caplog.text and "MUST_NOT_RUN" not in caplog.text
        assert not harness.resources.current.manager.configs
        assert (
            sum("Ignoring untrusted project configuration" in str(getattr(event, "text", "")) for event in events) == 1
        )
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_source_precedence_reports_the_effective_explicit_empty_mcp_map(tmp_path: Path) -> None:
    low, high = tmp_path / "global.json", tmp_path / "explicit.json"
    low.write_text(json.dumps({"mcp_servers": {"github": {"command": sys.executable}}}))
    high.write_text('{"mcp_servers": {}}')
    config = load_config(tmp_path, high)
    config.resource_paths = (low, high)
    selected = read_resource_configuration(config)
    assert selected.servers == () and selected.mcp_source == str(high)
    high.write_text('{"model": "unrelated"}')
    selected = read_resource_configuration(config)
    assert len(selected.servers) == 1 and selected.mcp_source == str(low)


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_reload_failure_provenance_distinguishes_parse_start_and_discovery_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from nagents.mcp import MCPError
    from nagents.mcp.client import MCPClient

    configured = server(tmp_path)
    source = tmp_path / "trusted.json"

    def write(command: str) -> None:
        source.write_text(
            json.dumps(
                {
                    "mcp_servers": {
                        "fixture": {
                            "command": command,
                            "args": [*configured.args, "PRIVATE_ARG_VALUE"],
                            "env": {"KEY": "PRIVATE_ENV_VALUE"},
                            "cwd": configured.cwd,
                        }
                    }
                }
            )
        )

    write(configured.command)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.resource_paths = (source,)
    try:
        with caplog.at_level(logging.INFO):
            await harness.initialize()
            original = harness.agent.tool_registry.get("mcp__fixture__echo")
            source.write_text('{"mcp_servers": "PRIVATE_INVALID_JSON_VALUE"')
            await harness.resources.reload()
            await harness.resources.reload()
            assert "stage=configuration" in harness.resources.last_error
            assert harness.agent.tool_registry.get("mcp__fixture__echo") is original
            write(str(tmp_path / "missing-server"))
            await harness.resources.reload()
            assert "stage=mcp_start" in harness.resources.last_error
            assert harness.agent.tool_registry.get("mcp__fixture__echo") is original
            write(configured.command)

            async def fail_discovery(self: MCPClient) -> list[dict[str, object]]:
                raise MCPError("PRIVATE_UPSTREAM_VALUE bad credentials", code=-32001)

            monkeypatch.setattr(MCPClient, "list_tools", fail_discovery)
            await harness.resources.reload()
            assert "stage=mcp_discover" in harness.resources.last_error
            assert harness.agent.tool_registry.get("mcp__fixture__echo") is original
        failures = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Resource reload failed:")
        ]
        assert len(failures) == 3
        assert all(str(source) in message and "previous tools" in message for message in failures)
        assert all(
            value not in caplog.text
            for value in (
                "PRIVATE_ARG_VALUE",
                "PRIVATE_ENV_VALUE",
                "PRIVATE_INVALID_JSON_VALUE",
                "PRIVATE_UPSTREAM_VALUE",
            )
        )
        assert len([message for message in harness.diagnostics if message.startswith("Resource reload failed:")]) == 1
    finally:
        await harness.close()
