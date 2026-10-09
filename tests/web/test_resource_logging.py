"""Default serve logging exposes safe source/count metadata without payloads."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.web.app import create_app
from tests.harness.test_resource_reload import server
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_default_serve_logger_reports_each_mcp_discovery_without_arguments_or_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    configured = server(tmp_path)
    configured.args.append("PRIVATE_COMMAND_ARGUMENT")
    configured.env = {"PRIVATE_KEY": "PRIVATE_ENV_VALUE"}
    harness.config.mcp_servers = (configured,)
    assets = tmp_path / "assets"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<html></html>")
    app = create_app(harness.config, assets=assets, harness_factory=lambda _: harness)
    # Match CLI's WARNING root and Uvicorn's INFO host logger. A plain INFO
    # nagents logger would stay silent here, recreating the deployed gap.
    with caplog.at_level(logging.WARNING), caplog.at_level(logging.INFO, logger="uvicorn.error"):
        async with app.router.lifespan_context(app):
            assert harness.resources.logger.name == "uvicorn.error"
            await harness.resources.reload()
            await harness.resources.reload()
    summaries = [record for record in caplog.records if "ngn MCP discovery:" in record.getMessage()]
    assert len(summaries) == 3 and all(record.name == "uvicorn.error" for record in summaries)
    assert "phase=startup" in summaries[0].getMessage()
    assert all("phase=reload" in record.getMessage() for record in summaries[1:])
    for record in summaries:
        data = json.loads(record.getMessage().split("diagnostics=", 1)[1])
        assert data["configured_servers"] == data["connected_servers"] == 1
        assert data["registered_tools"] == data["advertised_tools"] == 1
        assert data["status"] == "loaded" and data["effective_source"] == "programmatic configuration"
    assert "PRIVATE_COMMAND_ARGUMENT" not in caplog.text
    assert "PRIVATE_ENV_VALUE" not in caplog.text and "PRIVATE_KEY" not in caplog.text
