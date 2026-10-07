"""JSON config references keep provider updates in the selected registry file."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ProviderRegistryStore
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.tools import CodingTools
from tests.support.web import client_app


def _write(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_config_json_references_scope_files_and_preserves_workspace_model(tmp_path: Path) -> None:
    user = tmp_path / "user-config/ngn/config.json"
    global_providers = user.parent / "shared-providers.json"
    _write(user, {"providers": "shared-providers.json", "model": "global-model"})
    _write(
        global_providers,
        {
            "version": 2,
            "revision": "0" * 64,
            "active": "shared",
            "providers": {"shared": {"kind": "openai", "auth": "codex"}},
        },
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    local_providers = workspace / "team-providers.json"
    explicit = workspace / "config.json"
    _write(explicit, {"providers": {"workspace": "team-providers.json"}})
    _write(
        local_providers,
        {
            "version": 2,
            "revision": "0" * 64,
            "active": "team",
            "providers": {"team": {"kind": "anthropic", "auth": "api-key"}},
        },
    )
    scoped_model = user.parent / "workspaces" / hashlib.sha256(str(workspace).encode()).hexdigest()[:32]
    _write(scoped_model / "config.json", {"model": "workspace-model"})

    config = load_config(workspace, explicit)
    assert config.provider_paths == {"global": global_providers, "workspace": local_providers}
    assert config.provider == "team"
    assert config.model == "workspace-model"
    assert set(config.providers) == {"shared", "team"}
    assert config.config_paths == (user, explicit)
    assert {
        "provider_id",
        "connection",
        "provider_registry",
        "auth",
        "api",
        "api_key_env",
        "api_version",
        "base_url",
    }.isdisjoint(dir(config))
    assert config.provider == "team" and config.providers[config.provider].kind == "anthropic"
    assert (
        ScopedProviderRegistryStore(workspace, paths=config.provider_paths).model_store("workspace").path
        == scoped_model / "config.json"
    )


def test_ui_edits_referenced_json_without_rewriting_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    providers = tmp_path / "team-providers.json"
    _write(config_file, {"providers": "team-providers.json", "demo": True})
    config = load_config(tmp_path, config_file)
    config.data_dir = tmp_path / "data"

    async def check() -> None:
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            initial = (await client.get("/api/provider-scopes/workspace/providers", headers=headers)).json()
            assert initial["path"] == str(providers)
            saved = await client.put(
                "/api/provider-scopes/workspace/providers/team",
                headers=headers,
                json={"revision": initial["revision"], "profile": {"kind": "openai", "auth": "codex"}},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["active"] == "team"
            effective = harnesses[0].config
            assert effective.provider == "team"
            assert effective.providers[effective.provider].auth == "codex"

    asyncio.run(check())
    assert config_file.read_text() == json.dumps({"providers": "team-providers.json", "demo": True}, indent=2) + "\n"
    document = json.loads(providers.read_text())
    assert document["providers"]["team"]["auth"] == "codex"
    assert providers.read_text() == json.dumps(document, indent=2, sort_keys=True) + "\n"
    with pytest.raises(ValueError, match="changed"):
        ProviderRegistryStore(providers, allow_external_active=True).save(ProviderRegistry(), expected="0" * 64)


def test_missing_reference_uses_default_json_and_bad_explicit_files_fail(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    _write(config_file, {"model": "configured-model"})
    config = load_config(tmp_path, config_file)
    assert config.provider_paths == {}
    assert ScopedProviderRegistryStore(tmp_path).workspace_store.path.name == "providers.json"
    config_file.write_text('{"providers": "other.yaml"}\n')
    with pytest.raises(ValueError, match=r"separate \.json"):
        load_config(tmp_path, config_file)
    (tmp_path / "config.yaml").write_text("{}")
    with pytest.raises(ValueError, match=r"must be \.json"):
        load_config(tmp_path, tmp_path / "config.yaml")


@pytest.mark.parametrize("field", ["provider", "auth", "api", "base_url", "api_key_env", "api_version"])
def test_connection_fields_are_rejected_in_general_config(tmp_path: Path, field: str) -> None:
    path = tmp_path / "config.json"
    _write(path, {field: "untrusted"})
    with pytest.raises(ValueError, match=r"configure connections in providers\.json"):
        load_config(tmp_path, path)


def test_unconfigured_web_starts_for_provider_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Provider setup does not exercise the POSIX-only workspace discovery tools.
    monkeypatch.setattr(CodingTools, "instructions", Mock(return_value=""))
    monkeypatch.setattr(CodingTools, "discover_skills", Mock())

    async def check() -> None:
        async with client_app(
            tmp_path, config=HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data"), controlled=False
        ) as (_, client, headers, _):
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["provider_setup"]["configured"] is False
            session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            denied = await client.post(
                "/api/messages",
                headers=headers,
                json={"session_id": session, "prompt": "hello", "message_id": "11111111-1111-4111-8111-111111111111"},
            )
            assert denied.status_code == 409
            registry = (await client.get("/api/provider-scopes/workspace/providers", headers=headers)).json()
            saved = await client.put(
                "/api/provider-scopes/workspace/providers/team",
                headers=headers,
                json={"revision": registry["revision"], "profile": {"kind": "openai", "auth": "api-key"}},
            )
            assert saved.status_code == 200
            assert saved.json()["active"] == "team"

    asyncio.run(check())


@pytest.mark.parametrize(
    ("kind", "api"),
    [("anthropic", "messages"), ("openai", "responses"), ("openai", "chat_completions")],
)
def test_selected_provider_exposes_explicit_api(tmp_path: Path, kind: str, api: str) -> None:
    config_file = tmp_path / "config.json"
    _write(config_file, {"providers": "providers.json", "provider_id": "chosen", "model": "test-model"})
    _write(
        tmp_path / "providers.json",
        {
            "version": 2,
            "revision": "0" * 64,
            "active": "chosen",
            "providers": {"chosen": {"kind": kind, "auth": "api-key", "api": api}},
        },
    )
    config = load_config(tmp_path, config_file)
    assert config.provider == "chosen"
    assert config.providers[config.provider].api == api
    assert config.model == "test-model"
    assert "connection" not in dir(config) and "provider_registry" not in dir(config)


def test_model_edit_keeps_other_config_fields_and_rejects_provider_collision(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    _write(config_file, {"providers": "team.json", "theme": "ocean"})
    model = ScopedProviderRegistryStore(tmp_path).model_store("global")
    _write(model.path, {"providers": "team.json", "theme": "ocean"})
    model.save("new-model")
    assert json.loads(model.path.read_text()) == {"providers": "team.json", "theme": "ocean", "model": "new-model"}
    assert model.path.read_text() == json.dumps(json.loads(model.path.read_text()), indent=2, sort_keys=True) + "\n"
    _write(config_file, {"providers": {"global": str(model.path)}})
    with pytest.raises(ValueError, match="cannot overwrite"):
        load_config(tmp_path, config_file)
