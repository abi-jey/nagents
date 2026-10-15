"""The running Team API observes definition edits and starts the new identity."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import runtime
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from tests.support.config import connection
from tests.support.providers import FakeProvider
from tests.support.web import client_app
from tests.web.test_web_subscription_lifecycle import until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message


@pytest.mark.requires_posix
def test_team_tracks_create_rename_and_starts_mini_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        native, foreign = workspace / ".ngn/agents", workspace / ".agents/agents"
        native.mkdir(parents=True)
        foreign.mkdir(parents=True)
        trusted = tmp_path / "operator.json"
        trusted.write_text(
            json.dumps(
                {
                    "agent_imports": [
                        {"path": str(foreign), "format": "antigravity"},
                        {"path": str(native), "format": "native"},
                    ]
                }
            )
        )
        config = load_config(workspace, trusted)
        config.data_dir = tmp_path / "data"
        config.model = "fake-model"
        config.provider = "fixture"
        config.providers = connection(name="fixture", auth="api-key")
        store = ScopedProviderRegistryStore(workspace).global_store
        store.save(ProviderRegistry(active="fixture", providers=config.providers), expected=store.load().revision)
        (workspace / "evidence.txt").write_text("Mini can use its native inherited file tools.")
        providers: list[FakeProvider] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.harness_config.agent == "mini" and len(provider.requests) == 1:
                assert "read_file" in provider.schemas[-1]
                yield ToolCallEvent(id="mini-evidence", name="read_file", arguments={"path": "evidence.txt"})
            else:
                yield TextDoneEvent(
                    text="Mini checked the evidence." if provider.harness_config.agent == "mini" else "Done."
                )

        def provider_factory(config: HarnessConfig, login_store: object = None) -> FakeProvider:
            instance = FakeProvider(config, len(providers), script)
            providers.append(instance)
            return instance

        monkeypatch.setattr(runtime, "HarnessProvider", provider_factory)
        monkeypatch.setattr(runtime, "build_provider", lambda profile, config, auth: provider_factory(config))
        monkeypatch.setenv("OPENAI_API_KEY", "test-only-scripted-provider-key")
        async with client_app(tmp_path, config=config, controlled=False) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            route = f"/api/sessions/{root}/tasks"
            initial = (await client.get(route, headers=headers)).json()
            assert {item["id"] for item in initial["profiles"]} == {"assistant"}
            revisions = {initial["profile_revision"]}
            previous = ""
            for name in ("mint", "min", "mini"):
                for directory in (foreign, native):
                    if previous:
                        (directory / f"{previous}.md").unlink()
                    (directory / f"{name}.md").write_text(
                        f"---\nname: {name}\ndescription: General coding agent\n---\nFollow the assigned task.\n"
                    )
                response = await client.get(route, headers=headers)
                assert response.status_code == 200, response.text
                catalog = response.json()
                profiles = {item["id"]: item for item in catalog["profiles"]}
                assert set(profiles) == {"assistant", name}
                assert profiles[name]["can_start"] and not profiles[name]["used"]
                assert profiles[name]["source"].endswith(f".ngn/agents/{name}.md")
                revisions.add(catalog["profile_revision"])
                selection = (await client.get(f"/api/sessions/{root}/agent", headers=headers)).json()
                assert {item["name"] for item in selection["profiles"]} == {"assistant", name}
                previous = name
            assert len(revisions) == 4
            started = await client.post(
                route,
                headers=headers,
                json={"message_id": str(uuid4()), "agent": "mini", "text": "Read evidence.txt and report."},
            )
            assert started.status_code == 200, started.text
            task_id = started.json()["task"]["id"]
            await until(lambda: state.run_for(root) is None)
            detail = (await client.get(f"{route}/{task_id}", headers=headers)).json()
            assert detail["task"]["profile"] == "mini"
            assert detail["task"]["status"] == "completed"
            assert "Mini checked the evidence" in json.dumps(detail["history"])
            used = (await client.get(route, headers=headers)).json()
            assert next(item for item in used["profiles"] if item["id"] == "mini")["used"]
            (native / "mini.md").unlink()
            (foreign / "mini.md").unlink()
            removed = (await client.get(route, headers=headers)).json()
            assert {item["id"] for item in removed["profiles"]} == {"assistant"}
            assert any(item["id"] == task_id for item in removed["tasks"])
        assert all(provider.closed for provider in providers)

    asyncio.run(scenario())
