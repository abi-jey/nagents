"""A global model can be repaired where its referenced connection is absent."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.events import ErrorEvent
from nagents.harness.config import load_config
from nagents.harness.model_bindings import ModelBinding
from nagents.harness.provider import UnavailableModelProvider
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import submit_and_wait

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_cross_workspace_missing_global_reference_opens_settings_and_can_be_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_store = ScopedProviderRegistryStore(first)
    profile = ProviderProfile(kind="openai", auth="api-key")
    first_store.global_store.save(ProviderRegistry(active="home", providers={"home": profile}), expected="0" * 64)
    first_store.workspace_store.save(ProviderRegistry(providers={"first-only": profile}), expected="0" * 64)
    missing = ModelBinding("first-only", "global-selected-model")
    first_store.model_store("global").save_binding(missing)
    config = load_config(second)
    config.demo = True
    config.data_dir = tmp_path / "state"
    assert config.unavailable_model_binding == missing
    assert config.provider == ""
    network = AsyncMock(side_effect=AssertionError("Model setup and offline repair must not make network requests"))
    with patch("nagents.http.HTTPClient._get_session", network):
        async with client_app(second, controlled=False, config=config) as (app, client, headers, _):
            harness = app.state.web.harness
            assert isinstance(harness.agent.provider, UnavailableModelProvider)
            failure = [
                event async for event in harness.agent.provider.generate([Message(role="user", content="hello")])
            ]
            assert len(failure) == 1 and isinstance(failure[0], ErrorEvent)
            assert failure[0].code == "MODEL_CONNECTION_UNAVAILABLE"
            before = (await client.get("/api/settings", headers=headers)).json()
            assert before["model_binding"] == missing.as_dict()
            assert before["model_binding_available"] is False
            assert "first-only" in before["model_binding_unavailable_reason"]
            repaired = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": before["revision"],
                    "values": {**before["values"], "model": "working-model"},
                    "model_binding": {"provider": "home", "id": "working-model"},
                },
            )
            assert repaired.status_code == 200, repaired.text
            assert repaired.json()["model_binding_available"] is True
            assert not isinstance(app.state.web.harness.agent.provider, UnavailableModelProvider)
            assert not app.state.web.harness.config.unavailable_model_binding.configured
            async with asyncio.timeout(HANG_GUARD):
                events = await submit_and_wait(app, headers, app.state.web.selected_session_id, "hello")
            assert any(event.get("event") == "text_done" for event in events)
            assert not any(event.get("event") == "error" for event in events)
            assert first_store.model_binding("global") == missing
        network.assert_not_awaited()


def test_unknown_standalone_connection_remains_an_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "operator.json"
    path.write_text('{"provider_id": "not-configured"}')
    with pytest.raises(ValueError, match="Unknown provider connection"):
        load_config(tmp_path, path)
