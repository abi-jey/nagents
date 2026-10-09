"""Serve exposes only bounded catalog compatibility diagnostics at its log level."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from nagents.harness.config import HarnessConfig
from nagents.provider import _codex_catalog as catalog
from tests.support.config import connection
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("path", ["/api/models", "/api/providers/fixture/models"])
@pytest.mark.parametrize("outage", [False, True])
def test_catalog_diagnostics_use_host_logger_without_enabling_other_library_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, path: str, outage: bool
) -> None:
    versions = catalog.CatalogVersion()
    monkeypatch.setattr(
        versions,
        "_fetch",
        AsyncMock(side_effect=ValueError("SECRET-upstream-body"))
        if outage
        else AsyncMock(return_value=("0.163.0", "")),
    )

    async def models(*names: str) -> list[str]:
        await versions.resolve()
        logging.getLogger("nagents.mcp.client").info("SECRET-unrelated-MCP-log")
        return ["selected-catalog-model"]

    async def scenario() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", providers=connection(auth="api-key"))
        # This matches ngn serve: library/root logging stays at WARNING while
        # Uvicorn's own application logger accepts INFO.
        with caplog.at_level(logging.WARNING), caplog.at_level(logging.INFO, logger="uvicorn.error"):
            async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
                harness = harnesses[0]
                monkeypatch.setattr(harness.agent.provider, "get_model_list", models)
                monkeypatch.setattr(harness, "provider_models", models)
                response = await client.get(path, headers=headers)
                assert response.status_code == 200
                assert response.json()["models"] == ["selected-catalog-model"]
                details = [r for r in caplog.records if r.getMessage().startswith("Codex catalog compatibility:")]
                assert len(details) == 1 and details[0].name == "uvicorn.error"
                expected = (
                    f"version={catalog.FALLBACK_VERSION} source=bundled status=fallback"
                    if outage
                    else "version=0.163.0 source=npm status=checked"
                )
                assert expected in details[0].getMessage()
                assert "SECRET" not in caplog.text + response.text
                caplog.clear()
                versions._result("checked")
                assert not caplog.records, "The host diagnostic logger must not leak outside its request scope"

    asyncio.run(scenario())
