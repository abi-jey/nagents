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


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_saved_agent_valid_model_shadows_missing_default_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ScopedProviderRegistryStore(workspace)
    home = ProviderProfile(kind="openai", auth="api-key")
    store.global_store.save(ProviderRegistry(active="home", providers={"home": home}), expected="0" * 64)
    store.model_store("global").save_binding(ModelBinding("home", "global-model"))
    definition = workspace / ".ngn/agents/mini.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        "---\nname: mini\ndescription: A helper\nmodel: {provider: home, id: mini-model}\n---\nHelp with the task.\n"
    )
    config = load_config(workspace, trust_project=True)
    config.demo = True
    config.data_dir = tmp_path / "state"
    network = AsyncMock(side_effect=AssertionError("Startup must not contact an alternative provider"))
    with patch("nagents.http.HTTPClient._get_session", network):
        async with client_app(workspace, controlled=False, config=config) as (_, client, headers, _):
            initial = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={"revision": initial["revision"], "values": {**initial["values"], "agent": "mini"}},
            )
            assert saved.status_code == 200, saved.text
        missing = ModelBinding("removed", "missing-default")
        store.model_store("global").save_binding(missing)
        restart = load_config(workspace, trust_project=True)
        restart.demo = True
        restart.data_dir = tmp_path / "state"
        assert restart.unavailable_model_binding == missing
        async with client_app(workspace, controlled=False, config=restart) as (app, client, headers, _):
            harness = app.state.web.harness
            assert harness.config.agent == "mini"
            assert harness.config.provider == "home"
            assert harness.agent.provider.model == "mini-model"
            assert not isinstance(harness.agent.provider, UnavailableModelProvider)
            snapshot = (await client.get("/api/settings", headers=headers)).json()
            assert snapshot["values"]["agent"] == "mini"
            assert snapshot["effective_model"] == "mini-model"
            assert snapshot["model_binding"] == missing.as_dict()
            assert snapshot["model_binding_available"] is False
        network.assert_not_awaited()


def test_unknown_standalone_connection_remains_an_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "operator.json"
    path.write_text('{"provider_id": "not-configured"}')
    with pytest.raises(ValueError, match="Unknown provider connection"):
        load_config(tmp_path, path)


@pytest.mark.requires_posix
@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["definition", "overlay"])
async def test_saved_agent_missing_connection_survives_restart_and_repairs_before_next_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ScopedProviderRegistryStore(workspace)
    home = ProviderProfile(kind="openai", auth="api-key")
    store.global_store.save(ProviderRegistry(active="home", providers={"home": home}), expected="0" * 64)
    store.model_store("global").save_binding(ModelBinding("home", "global-model"))
    definition = workspace / ".ngn/agents/mini.md"
    definition.parent.mkdir(parents=True)

    def write_definition(provider: str, model: str) -> None:
        definition.write_text(
            f"---\nname: mini\ndescription: A small helper\nmodel:\n  provider: {provider}\n  id: {model}\n---\nHelp with the assigned task.\n"
        )

    write_definition("home", "mini-model")
    config = load_config(workspace, trust_project=True)
    config.demo = True
    config.data_dir = tmp_path / "state"
    network = AsyncMock(
        side_effect=AssertionError("Saved-agent startup and offline execution must not request a provider")
    )
    with patch("nagents.http.HTTPClient._get_session", network):
        async with client_app(workspace, controlled=False, config=config) as (_, client, headers, _):
            initial = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={"revision": initial["revision"], "values": {**initial["values"], "agent": "mini"}},
            )
            assert saved.status_code == 200, saved.text

        missing = ModelBinding("removed", "unavailable-mini-model")
        if source == "definition":
            write_definition(missing.provider, missing.id)
        else:
            store.agent_models.save("mini", missing, expected="0" * 64)
        restart = load_config(workspace, trust_project=True)
        restart.demo = True
        restart.data_dir = tmp_path / "state"
        assert restart.agent == "assistant"
        assert not restart.unavailable_model_binding.configured
        async with client_app(workspace, controlled=False, config=restart) as (app, client, headers, _):
            harness = app.state.web.harness
            assert harness.config.agent == "mini"
            assert harness.config.unavailable_model_binding == missing
            assert isinstance(harness.agent.provider, UnavailableModelProvider)
            assert harness.agent.provider.binding == missing
            assert harness.agent.provider.model == missing.id
            before = (await client.get("/api/settings", headers=headers)).json()
            assert before["values"]["agent"] == "mini"
            assert before["effective_model"] == missing.id
            agent = (await client.get("/api/agent-models/mini", headers=headers)).json()
            assert agent["available"] is False
            assert agent["effective"] == missing.as_dict()
            repaired = await client.put(
                "/api/agent-models/mini",
                headers=headers,
                json={"revision": agent["revision"], "model": {"provider": "home", "id": "repaired-mini-model"}},
            )
            assert repaired.status_code == 200, repaired.text
            assert repaired.json()["available"] is True
            session = app.state.web.selected_session_id
            selection = (await client.get(f"/api/sessions/{session}/agent", headers=headers)).json()
            selected = await client.post(
                f"/api/sessions/{session}/agent",
                headers=headers,
                json={"revision": selection["revision"], "agent": "mini"},
            )
            assert selected.status_code == 200, selected.text
            async with asyncio.timeout(HANG_GUARD):
                events = await submit_and_wait(app, headers, session, "hello")
            assert any(event.get("event") == "text_done" for event in events)
            assert not any(event.get("event") == "error" for event in events)
            # An idle root without retained children releases its execution
            # lease after the response; its configured runtime remains usable.
            owner = app.state.web.harness
            assert owner.config.agent == "mini"
            assert owner.config.provider == "home"
            assert owner.agent.provider.model == "repaired-mini-model"
            assert not isinstance(owner.agent.provider, UnavailableModelProvider)
        network.assert_not_awaited()
