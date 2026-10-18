"""Model references keep their connection, inheritance, and operator provenance."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.harness.model_bindings import AgentModelStore
from nagents.harness.model_bindings import ModelBinding
from nagents.harness.model_bindings import resolve_model_binding
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore

if TYPE_CHECKING:
    from pathlib import Path

EMPTY_REVISION = "0" * 64


def test_resolution_keeps_pairs_and_records_the_winning_layer() -> None:
    providers = {name: ProviderProfile(kind="openai") for name in ("home", "work", "agent", "once")}
    global_default = ModelBinding("home", "global-model")
    workspace = ModelBinding("work", "workspace-model")
    agent = ModelBinding("agent", "agent-model")
    invocation = ModelBinding("once", "one-model")
    origins = {"home": "global", "work": "workspace", "agent": "global", "once": "workspace"}
    chosen = resolve_model_binding(
        providers=providers,
        global_default=global_default,
        workspace=workspace,
        agent=agent,
        invocation=invocation,
        origins=origins,
        sources={"invocation": "delegation", "agent": "/config/agent-models/mini.md"},
    )
    assert (chosen.binding, chosen.layer, chosen.source, chosen.provider_origin) == (
        invocation,
        "invocation",
        "delegation",
        "workspace",
    )
    assert (
        resolve_model_binding(
            providers=providers, global_default=global_default, workspace=workspace, agent=agent
        ).binding
        == agent
    )
    assert (
        resolve_model_binding(providers=providers, global_default=global_default, workspace=workspace).binding
        == workspace
    )
    assert resolve_model_binding(providers=providers, global_default=global_default).binding == global_default


def test_missing_binding_connection_does_not_fall_back_to_valid_defaults() -> None:
    with pytest.raises(ValueError, match="unavailable provider connection 'deleted'"):
        resolve_model_binding(
            providers={"home": ProviderProfile(kind="openai")},
            agent=ModelBinding("deleted", "agent-model"),
            global_default=ModelBinding("home", "global-model"),
        )


def test_unnamed_builtin_is_effective_only_not_an_explicit_model(tmp_path: Path) -> None:
    binding = ModelBinding("", "demo-model")
    assert resolve_model_binding(providers={}, builtin=binding).binding == binding
    with pytest.raises(ValueError, match="unavailable provider"):
        resolve_model_binding(providers={}, agent=binding)
    with pytest.raises(ValueError, match="provider"):
        ModelBinding.parse(binding.as_dict())
    with pytest.raises(ValueError, match="configured provider"):
        AgentModelStore(tmp_path / "agents").save("assistant", binding, expected=EMPTY_REVISION)


@pytest.mark.parametrize(
    "value",
    [
        {"provider": "home"},
        {"id": "model"},
        {"provider": "home", "id": "model", "api_key": "not-accepted"},
        {"provider": "home", "id": ""},
        {"provider": "", "id": ""},
        {"provider": "../home", "id": "model"},
        {"provider": "home", "id": "model\n"},
        {"provider": "home", "id": 42},
    ],
)
def test_public_model_binding_is_a_strict_complete_reference(value: object) -> None:
    with pytest.raises(ValueError):
        ModelBinding.parse(value)


def test_agent_preferences_round_trip_without_creating_an_agent_definition(tmp_path: Path) -> None:
    store = AgentModelStore(tmp_path / "operator" / "agent-models")
    initial = store.load("assistant")
    assert initial.revision == EMPTY_REVISION
    assert not initial.binding.configured
    assert not initial.path.exists()
    selected = store.save("assistant", ModelBinding("home", "gpt-model"), expected=initial.revision)
    assert selected == store.load("assistant")
    assert selected.path.read_text().startswith("---\nmodel:\n")
    assert "instructions" not in selected.path.read_text()
    if os.name != "nt":
        assert selected.path.stat().st_mode & 0o777 == 0o600
    inherited = store.save("assistant", ModelBinding(), expected=selected.revision)
    assert inherited.revision == EMPTY_REVISION
    assert not inherited.binding.configured
    assert not inherited.path.exists()
    reset = store.reset("assistant", expected=inherited.revision)
    assert reset == initial
    assert not initial.path.exists()


def test_agent_preference_stale_writes_and_resets_do_not_overwrite(tmp_path: Path) -> None:
    store = AgentModelStore(tmp_path / "agents")
    selected = store.save("mini", ModelBinding("home", "first"), expected=EMPTY_REVISION)
    with pytest.raises(ValueError, match="changed"):
        store.save("mini", ModelBinding("home", "second"), expected=EMPTY_REVISION)
    with pytest.raises(ValueError, match="changed"):
        store.reset("mini", expected=EMPTY_REVISION)
    assert store.load("mini") == selected


def test_concurrent_agent_preference_writes_have_one_winner(tmp_path: Path) -> None:
    directory = tmp_path / "agents"
    ready = Barrier(2)

    def write(identifier: str) -> str:
        ready.wait(timeout=10)
        try:
            return (
                AgentModelStore(directory)
                .save("mini", ModelBinding("home", identifier), expected=EMPTY_REVISION)
                .binding.id
            )
        except ValueError as error:
            assert "changed" in str(error)
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ("first", "second")))
    assert results.count("conflict") == 1
    assert AgentModelStore(directory).load("mini").binding.id in set(results) - {"conflict"}


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "---\nmodel: {provider: home, id: okay}\n---\nInstructions are not allowed.\n",
        "---\nmodel: {provider: home, id: okay}\npermissions: [shell]\n---\n",
        "---\nmodel: {provider: home, id: first, id: second}\n---\n",
        "---\nmodel: &reference {provider: home, id: okay}\n---\n",
        "---\nmodel: {provider: home, id: 123}\n---\n",
        "---\nmodel: {provider: home, id: {nested: nope}}\n---\n",
        "x" * 16385,
    ],
)
def test_agent_model_files_cannot_smuggle_definitions_or_unbounded_data(tmp_path: Path, contents: str) -> None:
    store = AgentModelStore(tmp_path)
    (tmp_path / "mini.md").write_text(contents)
    with pytest.raises(ValueError):
        store.load("mini")


def test_cached_agent_preferences_isolate_nested_values_and_reload_changed_or_invalid_bytes(tmp_path: Path) -> None:
    store = AgentModelStore(tmp_path)
    path = tmp_path / "assistant.md"
    path.write_text(
        "---\nmodel:\n  provider: home\n  id: first-model\n"
        "live:\n  prompt: First prompt\n  voice: sol\ntools:\n  - read_file\n---\n",
        encoding="utf-8",
    )

    first = store.load("assistant")
    first_model = first.preferences["model"]
    first_live = first.preferences["live"]
    first_tools = first.preferences["tools"]
    assert isinstance(first_model, dict) and isinstance(first_live, dict) and isinstance(first_tools, list)
    cast("dict[str, object]", first_model)["id"] = "mutated-model"
    cast("dict[str, object]", first_live)["prompt"] = "Mutated prompt"
    cast("list[str]", first_tools).append("shell")

    repeated = store.load("assistant")
    assert repeated.binding == ModelBinding("home", "first-model")
    assert repeated.preferences["model"] == {"provider": "home", "id": "first-model"}
    assert repeated.preferences["live"] == {"prompt": "First prompt", "voice": "sol"}
    assert repeated.preferences["tools"] == ["read_file"]

    path.write_text(
        "---\nmodel:\n  provider: home\n  id: second-model\n"
        "live:\n  prompt: Second prompt\n  voice: cedar\ntools:\n  - search\n---\n",
        encoding="utf-8",
    )
    changed = store.load("assistant")
    assert changed.binding == ModelBinding("home", "second-model")
    assert changed.preferences["live"] == {"prompt": "Second prompt", "voice": "cedar"}
    assert changed.preferences["tools"] == ["search"]

    path.write_text("---\nmodel: [broken\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid agent preference frontmatter"):
        store.load("assistant")


def test_agent_model_paths_reject_traversal_and_symlinks(tmp_path: Path) -> None:
    store = AgentModelStore(tmp_path / "agents")
    with pytest.raises(ValueError, match="agent name"):
        store.load("../assistant")
    target = tmp_path / "outside.md"
    target.write_text("---\nmodel: {provider: home, id: preserved}\n---\n")
    store.directory.mkdir()
    try:
        (store.directory / "mini.md").symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(ValueError, match="regular file"):
        store.load("mini")
    with pytest.raises(ValueError):
        store.save("mini", ModelBinding("home", "changed"), expected=EMPTY_REVISION)
    assert "preserved" in target.read_text()


def test_scoped_model_bindings_keep_scope_selection_and_allow_cross_scope_pairs(tmp_path: Path) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    store.global_store.save(
        ProviderRegistry(active="home", providers={"home": ProviderProfile(kind="openai")}), expected=EMPTY_REVISION
    )
    store.workspace_store.save(
        ProviderRegistry(active="work", providers={"work": ProviderProfile(kind="openai")}), expected=EMPTY_REVISION
    )
    store.model_store("global").save_binding(ModelBinding("home", "global-id"))
    store.model_store("workspace").save_binding(ModelBinding("work", "workspace-id"))
    assert store.model_binding("global") == ModelBinding("home", "global-id")
    assert store.model_binding("workspace") == ModelBinding("work", "workspace-id")
    store.model_store("global").save_binding(ModelBinding("work", "workspace-hosted-model"))
    assert store.model_binding("global") == ModelBinding("work", "workspace-hosted-model")
    resolved = resolve_model_binding(providers=store.load().providers, global_default=store.model_binding("global"))
    assert resolved.binding == ModelBinding("work", "workspace-hosted-model")
    assert store.agent_models.directory == store.model_store("workspace").path.parent / "agents"
