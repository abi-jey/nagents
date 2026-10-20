"""Concurrent Markdown edits cannot be overwritten by stale web settings saves."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING
from typing import Literal
from unittest.mock import patch

import aiosqlite
import pytest

from nagents.harness.agent_documents import agent_document_store
from nagents.harness.agent_documents import bundled_assistant
from nagents.harness.model_bindings import AgentModelStore
from nagents.harness.model_bindings import ModelBinding
from nagents.harness.providers import ScopedProviderRegistryStore
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.web.test_model_binding_settings import configuration

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.settings import SettingsValues


_EMPTY_MODEL_BINDING = ModelBinding()


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_external_model_and_prompt_edit_conflicts_with_settings_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: Literal["global", "workspace"]
) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            settings = app.state.web.settings
            settings_path = "/api/settings/global" if scope == "global" else "/api/settings"
            before = (await client.get(settings_path, headers=headers)).json()
            harness = app.state.web.harness
            runtime_before = (
                harness.config.model,
                harness.config.submit_mode,
                harness._selected_model,
                harness.agent.provider.model,
            )
            document = agent_document_store(harness.provider_store, scope)
            original_validation = settings.validate_reasoning
            changed = False

            async def external_edit(
                values: SettingsValues,
                *,
                scope: str = "workspace",
                model_binding: ModelBinding = _EMPTY_MODEL_BINDING,
                catalog_scope: str = "",
            ) -> None:
                nonlocal changed
                await original_validation(values, scope=scope, model_binding=model_binding, catalog_scope=catalog_scope)
                if changed:
                    return
                changed = True
                current = document.load("assistant")
                live = current.preferences.get("live", {})
                assert isinstance(live, dict)
                external_binding = ModelBinding("home" if scope == "global" else "work", f"external-{scope}-model")
                document.patch(
                    "assistant",
                    {
                        "model": external_binding.as_dict(),
                        "live": {**live, "prompt": f"external {scope} prompt"},
                    },
                    expected=current.revision,
                )

            monkeypatch.setattr(settings, "validate_reasoning", external_edit)
            requested_model = f"requested-{scope}-model"
            requested_provider = "home" if scope == "global" else "work"
            response = await client.post(
                settings_path,
                headers=headers,
                json={
                    "revision": before["revision"],
                    "values": {
                        **before["values"],
                        "model": requested_model,
                        "submit_mode": "interrupt",
                    },
                    "model_binding": {"provider": requested_provider, "id": requested_model},
                },
            )

            assert changed
            assert response.status_code == 409, response.text
            saved_document = document.load("assistant")
            assert saved_document.binding == ModelBinding(
                "home" if scope == "global" else "work", f"external-{scope}-model"
            )
            saved_live = saved_document.preferences.get("live", {})
            assert isinstance(saved_live, dict)
            assert saved_live["prompt"] == f"external {scope} prompt"
            after = (await client.get(settings_path, headers=headers)).json()
            assert after["values"]["submit_mode"] == before["values"]["submit_mode"]
            assert after["persisted"] == before["persisted"]
            assert (
                harness.config.model,
                harness.config.submit_mode,
                harness._selected_model,
                harness.agent.provider.model,
            ) == runtime_before

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_commit_failure_restores_model_document_and_runtime(
    tmp_path: Path, scope: Literal["global", "workspace"]
) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        providers = ScopedProviderRegistryStore(tmp_path)
        document = agent_document_store(providers, scope)
        setup_document = AgentModelStore(document.directory)
        initial = setup_document.load("assistant")
        live = initial.preferences.get("live", {})
        assert isinstance(live, dict)
        changes: dict[str, object] = {
            "live": {**live, "prompt": f"PRESERVED_{scope}_PROMPT"},
            "auto_update": True,
        }
        if scope == "workspace":
            changes["model"] = ModelBinding("work", "saved-work-model").as_dict()
        else:
            _, default_revision = bundled_assistant()
            changes["default_revision"] = default_revision
        setup_document.patch("assistant", changes, expected=initial.revision)

        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            settings_path = "/api/settings/global" if scope == "global" else "/api/settings"
            before = (await client.get(settings_path, headers=headers)).json()
            harness = app.state.web.harness
            document_before = document.load("assistant")
            runtime_before = (
                harness.config.model,
                harness.config.submit_mode,
                harness._selected_model,
                harness.agent.provider.model,
            )
            body = {
                "revision": before["revision"],
                "values": {**before["values"], "model": "failed-commit-model", "submit_mode": "interrupt"},
                "model_binding": {
                    "provider": "home" if scope == "global" else "work",
                    "id": "failed-commit-model",
                },
            }

            commit = aiosqlite.Connection.commit
            commit_count = 0

            async def fail_commit_after_model_write(db: aiosqlite.Connection) -> None:
                nonlocal commit_count
                commit_count += 1
                if scope == "global" and commit_count == 1:
                    await commit(db)
                    return
                raise sqlite3.OperationalError("SECRET-commit-failure")

            with patch.object(aiosqlite.Connection, "commit", fail_commit_after_model_write):
                if scope == "global":
                    with pytest.raises(sqlite3.OperationalError):
                        await client.post(settings_path, headers=headers, json=body)
                else:
                    response = await client.post(settings_path, headers=headers, json=body)
                    assert response.status_code == 500
                    assert "SECRET" not in response.text

            assert commit_count == (2 if scope == "global" else 1)
            restored = document.load("assistant")
            assert restored.binding == document_before.binding
            restored_live = restored.preferences.get("live", {})
            assert isinstance(restored_live, dict)
            assert restored_live["prompt"] == f"PRESERVED_{scope}_PROMPT"
            assert restored.preferences["auto_update"] is True
            after = (await client.get(settings_path, headers=headers)).json()
            assert after["values"] == before["values"]
            assert after["persisted"] == before["persisted"]
            assert (
                harness.config.model,
                harness.config.submit_mode,
                harness._selected_model,
                harness.agent.provider.model,
            ) == runtime_before

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_settings_reads_keep_committed_snapshot_until_model_document_commit(
    tmp_path: Path, scope: Literal["global", "workspace"]
) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            workspace_before = (await client.get("/api/settings", headers=headers)).json()
            global_before = (await client.get("/api/settings/global", headers=headers)).json()
            settings_path = "/api/settings/global" if scope == "global" else "/api/settings"
            before = global_before if scope == "global" else workspace_before
            requested_model = f"committed-{scope}-model"
            body = {
                "revision": before["revision"],
                "values": {
                    **before["values"],
                    "model": requested_model,
                    "submit_mode": "interrupt",
                },
                "model_binding": {
                    "provider": "home" if scope == "global" else "work",
                    "id": requested_model,
                },
            }
            entered, release = asyncio.Event(), asyncio.Event()
            commit = aiosqlite.Connection.commit
            commit_count = 0

            async def delayed_commit(db: aiosqlite.Connection) -> None:
                nonlocal commit_count
                commit_count += 1
                if scope == "global" and commit_count == 1:
                    await commit(db)
                    return
                entered.set()
                await release.wait()
                await commit(db)

            with patch.object(aiosqlite.Connection, "commit", delayed_commit):
                request = asyncio.create_task(client.post(settings_path, headers=headers, json=body))
                try:
                    await asyncio.wait_for(entered.wait(), HANG_GUARD)
                    assert commit_count == (2 if scope == "global" else 1)
                    assert (await client.get("/api/settings", headers=headers)).json() == workspace_before
                    assert (await client.get("/api/settings/global", headers=headers)).json() == global_before
                except BaseException:
                    release.set()
                    await request
                    raise
                finally:
                    release.set()
                response = await asyncio.wait_for(request, HANG_GUARD)

            assert response.status_code == 200, response.text
            # Global reloads are read-only now; the save still performs its
            # schema setup commit and one final settings transaction.
            assert commit_count == (2 if scope == "global" else 1)
            workspace_after = (await client.get("/api/settings", headers=headers)).json()
            global_after = (await client.get("/api/settings/global", headers=headers)).json()
            if scope == "global":
                assert global_after["model_binding"] == {"provider": "home", "id": requested_model}
                assert workspace_after["model_binding"] == global_after["model_binding"]
            else:
                assert workspace_after["model_binding"] == {"provider": "work", "id": requested_model}
                assert global_after["model_binding"] == global_before["model_binding"]

    asyncio.run(scenario())
