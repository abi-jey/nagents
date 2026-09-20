"""Global defaults are shared; workspace overrides and credentials stay local."""

import asyncio
from pathlib import Path

from nagents.harness.config import HarnessConfig
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
                "provider": "openrouter",
                "api": "chat_completions",
                "api_key_env": "OPENROUTER_API_KEY",
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
            reset = await client.post("/api/settings/reset", headers=headers, json={"revision": current["revision"]})
            assert reset.status_code == 200, reset.text
            assert reset.json()["values"]["model"] == "new-global-model"
            denied = await client.post(
                "/api/settings/global",
                headers=headers,
                json={"revision": changed.json()["revision"], "values": values, "api_key": "must-not-be-shared"},
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
            assert inherited["values"]["model"] == "new-global-model"
            assert inherited["values"]["provider"] == "openrouter"
            assert inherited["values"]["api"] == "chat_completions"
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
