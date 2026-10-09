"""MCP discovery is visible without reading or executing foreign configuration."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.cli import main
from nagents.harness import Harness
from nagents.harness import load_config
from nagents.harness.mcp_diagnostics import ignored_foreign_configs
from tests.harness.test_resource_reload import server
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def portable_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    # These diagnostics exercise configuration/MCP, not POSIX workspace tools.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)


def user_config() -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "ngn" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.asyncio
async def test_zero_servers_logs_every_reload_and_never_reads_foreign_or_untrusted_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = user_config()
    user.write_text("{}")
    foreign = (tmp_path / ".vscode" / "mcp.json", tmp_path / "opencode.json")
    project = tmp_path / ".ngn" / "config.json"
    for path in (*foreign, project):
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"mcp_servers":{"hidden":{"command":"PRIVATE_COMMAND","env":{"TOKEN":"PRIVATE_SECRET"}}}}')
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object) -> object:
        assert path not in (*foreign, project), "Ignored configuration contents must never be opened"
        return cast("Callable[..., object]", original_open)(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.warns(UserWarning, match="Ignoring untrusted project"):
        config = load_config(tmp_path)
    harness = Harness(config)
    try:
        with caplog.at_level(logging.INFO, logger="nagents.harness.resources"):
            await harness.initialize(create_session=False)
            await harness.resources.reload()
            await harness.resources.reload()
        diagnostics = harness.resources.mcp_diagnostics()
        assert diagnostics.configured_servers == diagnostics.connected_servers == 0
        assert diagnostics.registered_tools == diagnostics.advertised_tools == 0
        assert diagnostics.effective_source == "none" and diagnostics.status == "loaded"
        sources = {source.path: source for source in diagnostics.sources}
        assert sources[str(user)].status == "read" and sources[str(user)].trusted
        assert sources[str(project)].status == "ignored" and not sources[str(project)].trusted
        assert diagnostics.ignored_configs == tuple(str(path) for path in foreign)
        summaries = [record.getMessage() for record in caplog.records if "ngn MCP discovery:" in record.getMessage()]
        assert len(summaries) == 3
        assert "phase=startup" in summaries[0] and all('"configured_servers": 0' in text for text in summaries)
        assert sum("Ignoring foreign MCP configuration" in record.getMessage() for record in caplog.records) == 2
        public = json.dumps(diagnostics.snapshot()) + harness.describe() + caplog.text
        assert "PRIVATE_SECRET" not in public and "PRIVATE_COMMAND" not in public
        assert ".vscode/mcp.json" in public and "opencode.json" in public
        foreign[0].unlink()
        await harness.resources.reload()
        assert harness.resources.mcp_diagnostics().ignored_configs == (str(foreign[1]),)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_counts_follow_loaded_tools_and_retain_previous_source_after_failed_reload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    configured = server(tmp_path)
    user = user_config()
    user.write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "fixture": {
                        "command": configured.command,
                        "args": configured.args,
                        "cwd": configured.cwd,
                        "env": {"MCP_DIAGNOSTIC_SECRET": "PRIVATE_MCP_VALUE"},
                    }
                }
            }
        )
    )
    harness = Harness(load_config(tmp_path))
    try:
        with caplog.at_level(logging.INFO, logger="nagents.harness.resources"):
            await harness.initialize(create_session=False)
            loaded = harness.resources.mcp_diagnostics()
            assert loaded.configured_servers == loaded.connected_servers == 1
            assert loaded.registered_tools == loaded.advertised_tools == 1
            assert loaded.effective_source == str(user)
            harness.tool_settings.save({"assistant": {"mcp__fixture__echo": False}}, "")
            assert harness.resources.mcp_diagnostics().advertised_tools == 0
            user.write_text('{"mcp_servers":"PRIVATE_INVALID_VALUE"}')
            await harness.resources.reload()
            retained = harness.resources.mcp_diagnostics()
            assert retained.status == "retained" and retained.configured_servers == retained.connected_servers == 1
            assert retained.effective_source == str(user) and "stage=configuration" in retained.error
            user.write_text('{"mcp_servers":{}}')
            await harness.resources.reload()
            removed = harness.resources.mcp_diagnostics()
            assert removed.status == "loaded" and removed.configured_servers == removed.connected_servers == 0
            assert removed.registered_tools == 0 and removed.effective_source == str(user) and not removed.error
        public = caplog.text + json.dumps(loaded.snapshot()) + json.dumps(retained.snapshot())
        assert "PRIVATE_MCP_VALUE" not in public and "PRIVATE_INVALID_VALUE" not in public
        assert configured.command not in public and configured.args[-1] not in public
    finally:
        await harness.close()


def test_doctor_reports_missing_sources_and_ignored_files_even_when_demo_skips_plugins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user = user_config()
    user.write_text('{"plugins":["missing:setup"],"mcp_servers":{"disabled":{"command":"PRIVATE_COMMAND"}}}')
    foreign = tmp_path / "opencode.json"
    foreign.write_text("PRIVATE_FOREIGN_CONTENT")
    assert main(["--workspace", str(tmp_path), "--demo", "doctor"]) == 0
    output = capsys.readouterr().out
    assert "MCP: 1 configured servers, 0 connected" in output
    assert "(disabled)" in output and "MCP subprocess startup is disabled" in output
    assert str(user) in output and str(tmp_path / ".ngn" / "config.json") in output
    assert "missing; not trusted" in output and str(foreign) in output
    assert "PRIVATE_COMMAND" not in output and "PRIVATE_FOREIGN_CONTENT" not in output


@pytest.mark.asyncio
async def test_tools_endpoint_exposes_safe_read_only_discovery_metadata(tmp_path: Path) -> None:
    user = user_config()
    user.write_text("{}")
    foreign = tmp_path / ".vscode" / "mcp.json"
    foreign.parent.mkdir()
    foreign.write_text("PRIVATE_FOREIGN_SECRET")
    async with client_app(tmp_path, config=load_config(tmp_path)) as (_, client, headers, _):
        assert (await client.get("/api/tools")).status_code == 403
        response = await client.get("/api/tools", headers=headers)
        assert response.status_code == 200
        data = response.json()["mcp_diagnostics"]
        assert data["status"] == "loaded" and data["configured_servers"] == 0
        assert data["ignored_configs"] == [str(foreign)]
        assert any(source["path"] == str(user) and source["trusted"] for source in data["sources"])
        assert "PRIVATE_FOREIGN_SECRET" not in response.text


def test_explicit_ngn_configuration_is_not_ignored_just_because_of_its_filename(tmp_path: Path) -> None:
    named = tmp_path / "opencode.json"
    named.write_text('{"mcp_servers":{}}')
    config = load_config(tmp_path, named)
    assert named in config.resource_paths
    assert ignored_foreign_configs(config) == ()
