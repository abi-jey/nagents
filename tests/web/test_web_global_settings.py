"""Global defaults are shared; workspace overrides and credentials stay local."""

import asyncio
from pathlib import Path

from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ScopedProviderRegistryStore
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io


def test_global_defaults_workspace_override_reset_and_cross_workspace_reload(tmp_path: Path) -> None:
    async def scenario() -> None:
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        data = tmp_path / "shared-data"
        config = HarnessConfig(first, data_dir=data, demo=True)
        async with client_app(first, config=config) as (_, client, headers, _):
            original = (await client.get("/api/settings/global", headers=headers)).json()
            assert original["scope"] == "global" and not original["persisted"]
            values = {
                **original["values"],
                "model": "global-model",
            }
            saved = await client.post(
                "/api/settings/global", headers=headers, json={"revision": original["revision"], "values": values}
            )
            assert saved.status_code == 200, saved.text
            global_revision = saved.json()["revision"]
            inherited = (await client.get("/api/settings", headers=headers)).json()
            assert inherited["values"]["model"] == "global-model" and not inherited["persisted"]
            project = await client.post(
                "/api/settings",
                headers=headers,
                json={"revision": inherited["revision"], "values": {**inherited["values"], "model": "project-model"}},
            )
            assert project.status_code == 200, project.text
            changed = await client.post(
                "/api/settings/global",
                headers=headers,
                json={"revision": global_revision, "values": {**values, "model": "new-global-model"}},
            )
            assert changed.status_code == 200, changed.text
            stale = await client.post(
                "/api/settings/global", headers=headers, json={"revision": global_revision, "values": values}
            )
            assert stale.status_code == 409
            current = (await client.get("/api/settings", headers=headers)).json()
            assert current["values"]["model"] == "project-model"
            assert current["defaults"]["model"] == "new-global-model"
            assert current["values"]["provider"] == "openai"
            inherited_limit = await client.post(
                "/api/settings/global",
                headers=headers,
                json={"revision": changed.json()["revision"], "values": {**values, "max_output": 65536}},
            )
            assert inherited_limit.status_code == 200, inherited_limit.text
            effective = (await client.get("/api/settings", headers=headers)).json()
            assert effective["values"]["model"] == "project-model"
            assert effective["values"]["max_output"] == 65536
            assert effective["revision"] != current["revision"]
            stale_workspace = await client.post(
                "/api/settings",
                headers=headers,
                json={"revision": current["revision"], "values": current["values"]},
            )
            assert stale_workspace.status_code == 409
            current = effective
            reset = await client.post("/api/settings/reset", headers=headers, json={"revision": current["revision"]})
            assert reset.status_code == 200, reset.text
            assert reset.json()["values"]["model"] == "global-model"
            denied = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": inherited_limit.json()["revision"],
                    "values": values,
                    "api_key": "must-not-be-shared",
                },
            )
            assert denied.status_code == 422
        async with client_app(second, config=HarnessConfig(second, data_dir=data, demo=True)) as (
            _,
            client,
            headers,
            _,
        ):
            inherited = (await client.get("/api/settings", headers=headers)).json()
            assert not inherited["persisted"]
            assert inherited["values"]["model"] == "global-model"
            assert inherited["values"]["provider"] == "openai"
            assert inherited["values"]["api"] == "auto"
            global_values = (await client.get("/api/settings/global", headers=headers)).json()
            reset = await client.post(
                "/api/settings/global/reset", headers=headers, json={"revision": global_values["revision"]}
            )
            assert reset.status_code == 200, reset.text
            assert not reset.json()["persisted"]
            assert reset.json()["values"]["provider"] == "openai"

    asyncio.run(scenario())


def test_global_settings_preserve_submit_mode_when_omitted(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = tmp_path / "first"
        first.mkdir()
        data = tmp_path / "shared-data"
        config = HarnessConfig(first, data_dir=data, demo=True)
        async with client_app(first, config=config) as (_, client, headers, _):
            original = (await client.get("/api/settings/global", headers=headers)).json()
            assert original["values"]["submit_mode"] == "queue"
            values = {**original["values"], "submit_mode": "interrupt"}
            saved = await client.post(
                "/api/settings/global", headers=headers, json={"revision": original["revision"], "values": values}
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["values"]["submit_mode"] == "interrupt"
            # An older client omits submit_mode; the global default is preserved.
            omitted = {key: value for key, value in saved.json()["values"].items() if key != "submit_mode"}
            again = await client.post(
                "/api/settings/global",
                headers=headers,
                json={"revision": saved.json()["revision"], "values": omitted},
            )
            assert again.status_code == 200, again.text
            assert again.json()["values"]["submit_mode"] == "interrupt"

    asyncio.run(scenario())


def test_explicit_startup_model_overrides_saved_global_and_workspace_choices(tmp_path: Path) -> None:
    async def scenario() -> None:
        data_dir = tmp_path / "data"
        store = ScopedProviderRegistryStore(tmp_path)
        store.model_store("global").save("old-global")
        store.model_store("workspace").save("old-workspace")
        async with client_app(tmp_path, config=HarnessConfig(tmp_path, data_dir=data_dir, demo=True)) as (
            _,
            client,
            headers,
            _,
        ):
            global_before = (await client.get("/api/settings/global", headers=headers)).json()
            global_saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": global_before["revision"],
                    "values": {**global_before["values"], "model": "saved-global"},
                },
            )
            assert global_saved.status_code == 200, global_saved.text
            workspace_before = (await client.get("/api/settings", headers=headers)).json()
            workspace_saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": workspace_before["revision"],
                    "values": {**workspace_before["values"], "model": "saved-workspace"},
                },
            )
            assert workspace_saved.status_code == 200, workspace_saved.text

        pinned = HarnessConfig(
            tmp_path,
            data_dir=data_dir,
            demo=True,
            model="cli-model",
            global_model_default="cli-model",
            model_explicit=True,
            model_config_explicit=True,
        )
        async with client_app(tmp_path, config=pinned) as (_, client, headers, harnesses):
            global_current = (await client.get("/api/settings/global", headers=headers)).json()
            assert global_current["values"]["model"] == "cli-model"
            global_changed = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": global_current["revision"],
                    "values": {**global_current["values"], "max_output": 65536},
                },
            )
            assert global_changed.status_code == 200, global_changed.text
            workspace_current = (await client.get("/api/settings", headers=headers)).json()
            assert workspace_current["values"]["model"] == "cli-model"
            workspace_changed = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": workspace_current["revision"],
                    "values": {**workspace_current["values"], "max_tool_rounds": 9},
                },
            )
            assert workspace_changed.status_code == 200, workspace_changed.text
            assert (await client.get("/api/bootstrap")).json()["model"] == "cli-model"
            assert harnesses[0].agent.provider.model == "cli-model"
        assert store.model_store("global").load() == "saved-global"
        assert store.model_store("workspace").load() == "saved-workspace"

    asyncio.run(scenario())


def test_global_read_only_false_cannot_lower_startup_floor(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = tmp_path / "first"
        first.mkdir()
        data = tmp_path / "shared-data"
        config = HarnessConfig(first, data_dir=data, demo=True, read_only=True)
        async with client_app(first, config=config) as (_, client, headers, harnesses):
            original = (await client.get("/api/settings/global", headers=headers)).json()
            assert original["values"]["read_only"] is True
            values = {**original["values"], "read_only": False}
            saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={"revision": original["revision"], "values": values},
            )
            assert saved.status_code == 200, saved.text
            # The stored global preference may be False, but the startup admin
            # floor still binds the live harness.
            assert saved.json()["values"]["read_only"] is False
            assert harnesses[0].config.read_only is True
            assert harnesses[0].mode == "reviewer"

    asyncio.run(scenario())
