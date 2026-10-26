"""Agent Live layers remain independent of normal model routing and active calls."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from nagents.harness.agents.catalog import AgentCatalog
from nagents.harness.agents.definitions import discover_agents
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.providers.live_profiles import LiveProfile
from nagents.harness.providers.model_bindings import AgentModelStore
from nagents.harness.providers.model_bindings import ModelBinding
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.web.live.settings import AgentVoiceInput
from nagents.web.live.settings import LiveSettings
from nagents.web.sessions.agents import Choice
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path

EMPTY = "0" * 64


def test_native_and_json_live_profiles_preserve_explicit_empty_and_false(tmp_path: Path) -> None:
    directory = tmp_path / ".ngn" / "agents"
    directory.mkdir(parents=True)
    path = directory / "reader.md"
    path.write_text(
        "---\ndescription: Reader\nmodel: {provider: text, id: normal}\n"
        "live:\n  enabled: false\n  model: {provider: voice, id: gpt-live-1}\n"
        "  prompt: ''\n  context_mode: none\n---\n"
    )
    parsed = discover_agents(tmp_path)
    assert not parsed.failed and not parsed.diagnostics
    profile = parsed.profiles[0]
    assert profile.provider == "text" and profile.model == "normal"
    assert profile.live.selected() == {
        "enabled": False,
        "model": {"provider": "voice", "id": "gpt-live-1"},
        "prompt": "",
        "context_mode": "none",
    }
    source = tmp_path / "profiles.json"
    source.write_text(json.dumps({"profiles": {"json-reader": {"live": profile.live.selected()}}}))
    config = load_config(tmp_path, source)
    assert config.profile("json-reader").live == profile.live


@pytest.mark.parametrize(
    "invalid",
    [
        {"model": {"provider": "voice"}},
        {"model": {"id": "gpt-live-1"}},
        {"model": {"provider": "voice", "id": "gpt-live-1", "api_key": "secret"}},
        {"provider": "voice"},
        {"enabled": "false"},
        {"instructions": None},
        {"context_mode": "full"},
        {"voice": "unknown"},
        {"prompt": "x" * 12001},
    ],
)
def test_live_profile_rejects_partial_connections_and_invalid_fields(invalid: object) -> None:
    with pytest.raises(ValueError):
        LiveProfile.parse(invalid)


def test_invalid_live_reload_keeps_accepted_catalog_and_deletion_removes_agent(tmp_path: Path) -> None:
    directory = tmp_path / ".ngn" / "agents"
    directory.mkdir(parents=True)
    path = directory / "reader.md"
    valid = "---\ndescription: Reader\nlive:\n  voice: cedar\nprompt: Read.\n---\n"
    path.write_text(valid)
    config = load_config(tmp_path, trust_project=True)
    catalog = AgentCatalog(config)
    revision = catalog.revision
    path.write_text(valid.replace("voice: cedar", "model: {provider: voice}"))
    assert not catalog.refresh(config)
    assert catalog.revision == revision
    assert config.profile("reader").live.selected() == {"voice": "cedar"}
    assert config.agent_diagnostics
    path.write_text(valid.replace("voice: cedar", "voice: marin"))
    assert catalog.refresh(config)
    assert config.profile("reader").live.selected() == {"voice": "marin"}
    path.unlink()
    assert catalog.refresh(config)
    assert "reader" not in config.profile_names


def test_shared_overlay_preserves_sections_and_model_presence(tmp_path: Path) -> None:
    store = AgentModelStore(tmp_path / "preferences")
    live = store.patch("reader", {"live": {"enabled": False, "prompt": ""}}, expected=EMPTY)
    assert not live.model_present and live.revision != EMPTY
    model = store.save("reader", ModelBinding("text", "normal"), expected=live.revision)
    assert model.model_present and model.preferences["live"] == live.preferences["live"]
    behavior = store.replace_fields(
        "reader", ("prompt", "tools"), {"prompt": "Read", "tools": []}, expected=model.revision
    )
    reset = store.reset("reader", expected=behavior.revision)
    assert not reset.model_present and reset.preferences == {
        "live": {"enabled": False, "prompt": ""},
        "prompt": "Read",
        "tools": [],
    }
    with pytest.raises(ValueError, match="changed"):
        store.remove("reader", ("live",), expected=model.revision)
    remaining = store.remove("reader", ("live",), expected=reset.revision)
    assert remaining.preferences == {"prompt": "Read", "tools": []}


def test_shared_overlay_instruction_markdown_delimiters_round_trip(tmp_path: Path) -> None:
    store = AgentModelStore(tmp_path)
    instructions = "First line\n\n---\n\nKeep this section."
    saved = store.patch("reader", {"live": {"prompt": instructions}, "prompt": instructions}, expected=EMPTY)
    assert store.load("reader") == saved
    assert saved.preferences["live"] == {"prompt": instructions}


def test_invalid_json_live_reload_keeps_accepted_catalog(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"profiles": {"reader": {"live": {"voice": "cedar"}}}}))
    config = load_config(tmp_path, path)
    catalog = AgentCatalog(config)
    revision = catalog.revision
    path.write_text(json.dumps({"profiles": {"reader": {"live": {"enabled": "false"}}}}))
    assert not catalog.refresh(config)
    assert catalog.revision == revision
    assert config.profile("reader").live.selected() == {"voice": "cedar"}


def configuration(workspace: Path) -> HarnessConfig:
    voice = ProviderProfile(kind="openai", auth="api-key")
    text = ProviderProfile(kind="anthropic", auth="api-key")
    store = ScopedProviderRegistryStore(workspace)
    store.global_store.save(ProviderRegistry(active="voice", providers={"voice": voice, "text": text}), expected=EMPTY)
    return HarnessConfig(
        workspace,
        data_dir=workspace / "data",
        demo=True,
        agent="reader",
        provider="text",
        providers={"voice": voice, "text": text},
        profiles={
            "reader": AgentProfile(
                provider="text",
                model="normal-model",
                live=LiveProfile.parse({"voice": "cedar", "prompt": "Source voice"}),
            ),
            "writer": AgentProfile(live=LiveProfile.parse({"voice": "marin"})),
        },
    )


def test_live_settings_layer_reset_atomic_model_and_normal_model_independence(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            source = (await client.get("/api/live/settings?scope=agent&agent=reader", headers=headers)).json()
            assert source["effective"]["voice"] == "cedar" and source["origins"]["voice"] == "definition"
            assert source["effective"]["model"]["provider"] == "voice"
            assert not source["saved_live_present"]
            empty = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "reader",
                    "revision": source["revision"],
                    "overrides": {},
                },
            )
            assert empty.status_code == 200, empty.text
            inherited = empty.json()
            assert not inherited["saved_live_present"] and inherited["effective"]["voice"] == "cedar"
            assert inherited["effective"]["instructions"] == "Source voice"
            assert harness.resolved_model_binding("reader").binding == ModelBinding("text", "normal-model")
            settings_snapshot = (await client.get("/api/settings", headers=headers)).json()
            assert settings_snapshot["effective_model"] == "normal-model"
            for query in (
                "scope=agent&agent=reader&token=secret",
                "scope=agent&agent=reader&agent=writer",
                "scope=agent&agent=../reader",
            ):
                assert (await client.get("/api/live/settings?" + query, headers=headers)).status_code == 400
            assert (await client.get("/api/live/settings?agent=reader&scope=agent", headers=headers)).status_code == 200
            saved = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "reader",
                    "revision": inherited["revision"],
                    "overrides": {
                        "enabled": False,
                        "model": {"provider": "voice", "id": "voice-custom"},
                    },
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["effective"]["model"] == {"provider": "voice", "id": "voice-custom"}
            assert saved.json()["origins"]["enabled"] == "agent"
            stale = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "reader",
                    "revision": inherited["revision"],
                    "overrides": {"voice": "cedar"},
                },
            )
            assert stale.status_code == 409
            restored = await client.request(
                "DELETE",
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "reader",
                    "revision": saved.json()["revision"],
                },
            )
            assert restored.status_code == 200, restored.text
            assert not restored.json()["saved_live_present"] and restored.json()["effective"]["voice"] == "cedar"

    asyncio.run(scenario())


def test_admission_binds_chat_agent_generation_and_captures_immutable_profile(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, _, _, harnesses):
            harness = harnesses[0]
            host = app.state.web
            settings: LiveSettings = app.state.live_settings
            session = harness.session_id
            host.session_agents.choices[session] = Choice("reader", "reader-choice")
            reader = await settings.connection()
            assert reader.values.voice == "cedar"
            host.session_agents.choices[session] = Choice("writer", "writer-choice")
            with pytest.raises(HTTPException) as stale:
                async with settings.admit(reader.revision, session):
                    pytest.fail("Cross-agent admission must fail")
            assert stale.value.status_code == 409
            host.session_agents.choices[session] = Choice("reader", "next-reader-choice")
            released = asyncio.Event()

            async def active_voice() -> str:
                await released.wait()
                return settings.admitted().values.voice

            async with settings.admit(reader.revision, session) as admitted:
                task = asyncio.create_task(active_voice())
                assert admitted.values.voice == "cedar"
            changed = await settings.change_agent(
                AgentVoiceInput(
                    scope="agent",
                    agent="reader",
                    revision=reader.revision,
                    overrides={"voice": "marin"},
                )
            )
            assert changed["revision"] != reader.revision
            host.session_agents.choices[session] = Choice("writer", "later-writer-choice")
            released.set()
            assert await task == "cedar"
            host.session_agents.choices[session] = Choice("reader", "last-reader-choice")
            fresh = await settings.connection()
            harness.config.profiles["reader"].description = "Updated definition"
            with pytest.raises(HTTPException) as changed_definition:
                async with settings.admit(fresh.revision, session):
                    pytest.fail("Changed definition generation must fail")
            assert changed_definition.value.status_code == 409

    asyncio.run(scenario())


def test_agent_voice_rejects_escaped_credential_text_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "fixture-key-with-'\"\\-characters"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            store = harnesses[0].provider_store.agent_models
            before = store.load("reader")
            snapshot = (await client.get("/api/live/instructions?scope=agent&agent=reader", headers=headers)).json()
            response = await client.put(
                "/api/live/instructions",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "reader",
                    "revision": snapshot["revision"],
                    "prompt": "Accidentally pasted " + secret,
                },
            )
            assert response.status_code == 422
            assert response.json() == {"detail": "Keep API keys out of Voice instructions."}
            assert store.load("reader") == before
            assert not before.path.exists()

    asyncio.run(scenario())
