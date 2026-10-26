"""Reusable agent model settings preserve running work and expose inheritance."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.model_bindings import ModelBinding
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.harness.runtime import core as runtime
from nagents.harness.tools.coding import CodingTools
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.web import LiveStream
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.support.web import submit_and_wait

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.providers.auth import OpenAIAuth
    from nagents.types import Message

EMPTY_REVISION = "0" * 64


def configuration(workspace: Path, *, trusted: bool = False) -> HarnessConfig:
    store = ScopedProviderRegistryStore(workspace)
    home = ProviderProfile(kind="openai", auth="api-key")
    work = ProviderProfile(kind="openai_compatible", auth="api-key", base_url="https://models.example.test/v1")
    store.global_store.save(ProviderRegistry(active="home", providers={"home": home}), expected=EMPTY_REVISION)
    store.workspace_store.save(ProviderRegistry(providers={"work": work}), expected=EMPTY_REVISION)
    store.model_store("global").save_binding(ModelBinding("home", "global-model"))
    store.model_store("workspace").save_binding(ModelBinding("home", "workspace-model"))
    return HarnessConfig(
        workspace,
        data_dir=workspace / "data",
        demo=True,
        trust_project=trusted,
        provider="home",
        providers={"home": home, "work": work},
        model="workspace-model",
        profiles={
            "mini": AgentProfile(
                instructions="Private definition instructions never belong in the settings response.",
                description="A small helper",
                model="definition-model",
                provider="home",
                source=".ngn/agents/mini.md",
                source_format="native",
            )
        },
    )


def test_assistant_model_can_be_saved_inherited_and_reset(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            initial = await client.get("/api/agent-models/assistant", headers=headers)
            assert initial.status_code == 200, initial.text
            before = initial.json()
            assert before["model"] is None and before["definition_model"] is None
            assert before["inherited"] == {"provider": "home", "id": "workspace-model"}
            assert before["effective"] == before["inherited"]
            assert before["source"] == "workspace"
            with patch.object(app.state.web.bus, "event") as published:
                changed = await client.put(
                    "/api/agent-models/assistant",
                    headers=headers,
                    json={"revision": before["revision"], "model": {"provider": "work", "id": "agent-model"}},
                )
            assert changed.status_code == 200, changed.text
            saved = changed.json()
            assert saved["model"] == saved["effective"] == {"provider": "work", "id": "agent-model"}
            assert saved["source"] == "agent" and saved["provider_origin"] == "workspace"
            assert saved["available"] and not saved["unavailable_reason"]
            assert app.state.web.harness.resolved_model_binding("assistant").binding == ModelBinding(
                "work", "agent-model"
            )
            assert any(call.args[0].get("event") == "agent_profiles_changed" for call in published.call_args_list)
            reset = await client.request(
                "DELETE",
                "/api/agent-models/assistant",
                headers=headers,
                json={"revision": saved["revision"]},
            )
            assert reset.status_code == 200, reset.text
            assert reset.json()["model"] is None
            assert reset.json()["effective"] == before["inherited"]
            assert reset.json()["revision"] == EMPTY_REVISION

    asyncio.run(scenario())


def test_unused_profile_model_delete_restores_its_definition(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            before = (await client.get("/api/agent-models/mini", headers=headers)).json()
            assert before["definition_model"] == before["effective"] == {"provider": "home", "id": "definition-model"}
            assert "Private definition instructions" not in str(before)
            reset = await client.request(
                "DELETE",
                "/api/agent-models/mini",
                headers=headers,
                json={"revision": before["revision"]},
            )
            assert reset.status_code == 200, reset.text
            assert reset.json()["source"] == "agent"
            assert reset.json()["model"] is None
            assert reset.json()["effective"] == before["effective"]
            assert app.state.web.harness.resolved_model_binding("mini").binding == ModelBinding(
                "home", "definition-model"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "model",
    [
        {"provider": "missing", "id": "a-model"},
        {"provider": "home", "id": ""},
        {"provider": "", "id": "a-model"},
        {"provider": "home"},
        {"provider": "home", "id": "a-model", "instructions": "not allowed"},
        {"provider": {"id": "home"}, "id": "a-model"},
        {"provider": "home", "id": "a-model\n"},
    ],
)
def test_model_edits_reject_unknown_connections_partial_pairs_and_other_configuration(
    tmp_path: Path, model: object
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            response = await client.put(
                "/api/agent-models/assistant", headers=headers, json={"revision": EMPTY_REVISION, "model": model}
            )
            assert response.status_code == 422, response.text
            assert app.state.web.harness.provider_store.agent_models.load("assistant").revision == EMPTY_REVISION

    asyncio.run(scenario())


def test_stale_edits_and_removed_profiles_do_not_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path, trusted=True)) as (app, client, headers, _):
            saved = await client.put(
                "/api/agent-models/assistant",
                headers=headers,
                json={"revision": EMPTY_REVISION, "model": {"provider": "work", "id": "first"}},
            )
            assert saved.status_code == 200, saved.text
            for method, body in (
                ("PUT", {"revision": EMPTY_REVISION, "model": {"provider": "home", "id": "stale"}}),
                ("DELETE", {"revision": EMPTY_REVISION}),
            ):
                stale = await client.request(method, "/api/agent-models/assistant", headers=headers, json=body)
                assert stale.status_code == 409, stale.text
            directory = tmp_path / ".ngn" / "agents"
            directory.mkdir(parents=True)
            source = directory / "fresh.md"
            source.write_text(
                "---\nname: fresh\ndescription: A newly created agent\nprompt: Help with the request.\n---\n"
            )
            discovered = await client.get("/api/agent-models/fresh", headers=headers)
            assert discovered.status_code == 200, discovered.text
            source.unlink()
            assert (await client.get("/api/agent-models/fresh", headers=headers)).status_code == 404
            removed = await client.put(
                "/api/agent-models/fresh",
                headers=headers,
                json={"revision": EMPTY_REVISION, "model": {"provider": "work", "id": "unused"}},
            )
            assert removed.status_code == 404, removed.text
            assert not app.state.web.harness.provider_store.agent_models.load("fresh").path.exists()

    asyncio.run(scenario())


def test_unavailable_saved_connection_can_be_inspected_and_repaired(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            app.state.web.harness.provider_store.agent_models.save(
                "mini", ModelBinding("removed", "keep-visible"), expected=EMPTY_REVISION
            )
            response = await client.get("/api/agent-models/mini", headers=headers)
            assert response.status_code == 200, response.text
            broken = response.json()
            assert not broken["available"]
            assert broken["effective"] == {"provider": "removed", "id": "keep-visible"}
            assert "removed" in broken["unavailable_reason"]
            fixed = await client.put(
                "/api/agent-models/mini",
                headers=headers,
                json={"revision": broken["revision"], "model": {"provider": "home", "id": "repaired"}},
            )
            assert fixed.status_code == 200, fixed.text
            assert fixed.json()["available"]

    asyncio.run(scenario())


def test_saving_agent_model_during_execution_preserves_accepted_route(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            stream = LiveStream(app, headers, harnesses[0].session_id, "wait")
            await stream.event("text_chunk")
            try:
                owner = app.state.web.running_harness
                provider, model = owner.agent.provider, owner.agent.provider.model
                response = await client.put(
                    "/api/agent-models/assistant",
                    headers=headers,
                    json={"revision": EMPTY_REVISION, "model": {"provider": "work", "id": "next-activation"}},
                )
                assert response.status_code == 200, response.text
                assert app.state.web.running_harness is owner
                assert owner.agent.provider is provider and provider.model == model
                assert not owner._closed
                assert app.state.web.harness is not owner
                assert app.state.web.harness.resolved_model_binding("assistant").binding == ModelBinding(
                    "work", "next-activation"
                )
            finally:
                await stream.disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize("during_execution", [False, True], ids=["idle-save", "save-during-activation"])
def test_next_real_response_uses_the_saved_provider_and_model_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_execution: bool
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        providers: list[FakeProvider] = []
        routes: dict[int, tuple[str, str]] = {}
        calls: list[tuple[str, str, str, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            prompt = str(next(message.content for message in reversed(messages) if message.role == "user"))
            connection, endpoint = routes[provider.index]
            calls.append((prompt, connection, endpoint, provider.model))
            if prompt == "before" and during_execution:
                entered.set()
                await release.wait()
                # Saving another route must not mutate this accepted request.
                assert provider.model == "workspace-model"
                assert routes[provider.index] == ("home", "")
            yield TextDoneEvent(text=f"Finished {prompt}")

        def build(profile: ProviderProfile, config: HarnessConfig, auth: OpenAIAuth) -> FakeProvider:
            provider = FakeProvider(config, len(providers), script)
            # Snapshot the connection actually used to construct the transport;
            # reading its mutable harness config later would miss a mixed route.
            routes[provider.index] = (config.provider, profile.base_url)
            providers.append(provider)
            return provider

        monkeypatch.setattr(runtime, "build_provider", build)
        with patch.object(CodingTools, "instructions", return_value=""), patch.object(CodingTools, "discover_skills"):
            async with client_app(tmp_path, config=configuration(tmp_path), controlled=False) as (
                app,
                client,
                headers,
                _,
            ):
                root = app.state.web.selected_session_id
                first = asyncio.create_task(submit_and_wait(app, headers, root, "before"))
                try:
                    if during_execution:
                        async with asyncio.timeout(HANG_GUARD):
                            await entered.wait()
                    else:
                        events = await first
                        assert not any(event.get("event") == "error" for event in events)
                    saved = await client.put(
                        "/api/agent-models/assistant",
                        headers=headers,
                        json={"revision": EMPTY_REVISION, "model": {"provider": "work", "id": "next-model"}},
                    )
                    assert saved.status_code == 200, saved.text
                    if during_execution:
                        active = app.state.web.run_for(root)
                        assert active is not None and active.harness is not None
                        assert active.harness.config.provider == "home"
                        assert active.harness.agent.provider.model == "workspace-model"
                finally:
                    release.set()
                    events = await first
                    assert not any(event.get("event") == "error" for event in events)
                events = await submit_and_wait(app, headers, root, "after")
                assert not any(event.get("event") == "error" for event in events)
                assert calls == [
                    ("before", "home", "", "workspace-model"),
                    ("after", "work", "https://models.example.test/v1", "next-model"),
                ]
                selection = (await client.get(f"/api/sessions/{root}/agent", headers=headers)).json()
                assert selection["effective"]["provider"] == "work"
                assert selection["effective"]["model"] == "next-model"

    asyncio.run(scenario())
