"""Agent Markdown files share one revisioned store across editors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from nagents.harness.agent_documents import agent_document_store
from nagents.harness.agent_documents import bundled_assistant
from nagents.harness.agent_documents import ensure_agent_documents
from nagents.harness.model_bindings import AgentModelStore
from nagents.harness.providers import ScopedProviderRegistryStore

if TYPE_CHECKING:
    from pathlib import Path


def provider_store(tmp_path: Path) -> ScopedProviderRegistryStore:
    return ScopedProviderRegistryStore(
        tmp_path / "workspace",
        paths={
            "global": tmp_path / "global" / "providers.json",
            "workspace": tmp_path / "workspace-config" / "providers.json",
        },
    )


def test_startup_materializes_missing_global_assistant_from_bundle(tmp_path: Path) -> None:
    providers = provider_store(tmp_path)
    expected_preferences, expected_revision = bundled_assistant()

    ensure_agent_documents(providers)

    saved = agent_document_store(providers, "global").load("assistant")
    assert saved.preferences["default_revision"] == expected_revision
    assert {key: value for key, value in saved.preferences.items() if key != "default_revision"} == expected_preferences
    assert saved.path == providers.model_store("global").path.parent / "assistant.md"


def test_changed_bundle_replaces_entire_auto_updated_global_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import nagents.harness.agent_documents as documents

    providers = provider_store(tmp_path)
    store = agent_document_store(providers, "global")
    AgentModelStore(store.directory).patch(
        "assistant", {"prompt": "old managed prompt", "auto_update": True, "tools": ["read_file"]}, expected="0" * 64
    )
    monkeypatch.setattr(
        documents, "bundled_assistant", lambda: ({"prompt": "new bundled prompt", "tools": []}, "a" * 64)
    )

    with caplog.at_level(logging.WARNING, logger=documents.__name__):
        ensure_agent_documents(providers)

    saved = store.load("assistant")
    assert saved.preferences == {"prompt": "new bundled prompt", "tools": [], "default_revision": "a" * 64}
    assert "replacing auto_update-enabled global assistant file" in caplog.text


def test_changed_bundle_preserves_global_customization_when_auto_update_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nagents.harness.agent_documents as documents

    providers = provider_store(tmp_path)
    store = agent_document_store(providers, "global")
    store.patch("assistant", {"prompt": "keep this", "auto_update": False, "tools": ["search"]}, expected="0" * 64)
    monkeypatch.setattr(
        documents, "bundled_assistant", lambda: ({"prompt": "new bundled prompt", "tools": []}, "b" * 64)
    )

    ensure_agent_documents(providers)

    assert store.load("assistant").preferences == {"prompt": "keep this", "auto_update": False, "tools": ["search"]}


def test_global_ui_style_save_disables_auto_update(tmp_path: Path) -> None:
    providers = provider_store(tmp_path)
    store = AgentModelStore(providers.model_store("global").path.parent, disable_auto_update=True)
    saved = store.patch("assistant", {"prompt": "edited"}, expected="0" * 64)

    assert saved.preferences == {"prompt": "edited", "auto_update": False}


def test_live_patch_preserves_prompt_model_and_tools_in_same_document(tmp_path: Path) -> None:
    providers = provider_store(tmp_path)
    store = providers.agent_models
    original = store.patch(
        "assistant",
        {
            "prompt": "main prompt",
            "model": {"provider": "home", "id": "chosen"},
            "tools": ["read_file"],
            "live": {"voice": "sol", "prompt": "voice prompt"},
        },
        expected="0" * 64,
    )

    updated = store.patch_live("assistant", {"voice": "marin"}, expected=original.revision)

    assert updated.path == original.path
    assert updated.preferences == {
        "prompt": "main prompt",
        "model": {"provider": "home", "id": "chosen"},
        "tools": ["read_file"],
        "live": {"voice": "marin", "prompt": "voice prompt"},
    }


def test_stale_cross_editor_save_is_rejected_without_losing_first_write(tmp_path: Path) -> None:
    store = provider_store(tmp_path).agent_models
    original = store.patch("assistant", {"prompt": "main"}, expected="0" * 64)
    live_edit = store.patch_live("assistant", {"voice": "sol"}, expected=original.revision)

    with pytest.raises(ValueError, match="changed"):
        store.replace_fields("assistant", ("prompt", "tools"), {"prompt": "stale"}, expected=original.revision)

    assert store.load("assistant").preferences == {"prompt": "main", "live": {"voice": "sol"}}
    assert live_edit.revision == store.load("assistant").revision


def test_explicit_empty_main_prompt_replaces_inherited_value(tmp_path: Path) -> None:
    providers = provider_store(tmp_path)
    global_store = agent_document_store(providers, "global")
    workspace_store = agent_document_store(providers, "workspace")
    global_store.patch("assistant", {"prompt": "global prompt"}, expected="0" * 64)
    saved = workspace_store.patch("assistant", {"prompt": ""}, expected="0" * 64)

    assert saved.preferences["prompt"] == ""
    assert "prompt" in saved.preferences
