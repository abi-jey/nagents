"""Explicit definition directories do not grant broader workspace trust."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from nagents.harness import agent_definitions
from nagents.harness.agent_definitions import discover_agents
from nagents.harness.agent_definitions import import_agents
from nagents.harness.config import load_config

if TYPE_CHECKING:
    from pathlib import Path


def write_agent(path: Path, header: str = "description: A coding assistant") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{header}\n---\nFollow the assigned task and existing permission rules.\n", encoding="utf-8")


def test_imported_agent_directories_are_explicit_and_keep_native_precedence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    write_agent(workspace / ".agents/agents/mini.md", "name: mini\ndescription: Foreign definition")
    native = workspace / ".ngn/agents/mini.md"
    write_agent(native)
    write_agent(workspace / ".claude/agents/other.md", "name: other\ndescription: Unselected definition")
    (workspace / ".ngn/config.json").write_text(json.dumps({"plugins": ["unapproved.py:setup"]}))
    trusted = tmp_path / "host.json"
    trusted.write_text(
        json.dumps(
            {
                "agent_imports": [
                    {"path": "workspace/.agents/agents", "format": "antigravity"},
                    {"path": "workspace/.ngn/agents", "format": "native"},
                ]
            }
        )
    )
    with pytest.warns(UserWarning, match="untrusted project config"):
        config = load_config(workspace, trusted)
    assert not config.trust_project
    assert not config.plugins
    assert config.profile_names == ("assistant", "mini")
    assert config.profile("mini").source == str(native)
    assert config.profile("mini").source_format == "native"
    assert config.profile("mini").tools is None


def test_directory_import_skips_invalid_neighbors_and_arbitrary_subtrees(tmp_path: Path) -> None:
    write_agent(tmp_path / "agents/a.md")
    write_agent(tmp_path / "agents/b/agent.md")
    write_agent(tmp_path / "agents/nested/deeper/hidden.md")
    (tmp_path / "agents/bad.md").write_text("Not a manifest")
    batch = import_agents([{"path": "agents", "format": "native"}], tmp_path)
    assert [profile.name for profile in batch.profiles] == ["a", "b"]
    assert len(batch.diagnostics) == 1 and "bad.md" in batch.diagnostics[0]


def test_directory_import_budget_applies_across_all_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_definitions, "MAX_AGENT_FILES", 2)
    for name in ("a", "b"):
        write_agent(tmp_path / "first" / f"{name}.md")
    write_agent(tmp_path / "second/c.md")
    batch = import_agents(
        [
            {"path": "first", "format": "native"},
            {"path": "second", "format": "native"},
        ],
        tmp_path,
    )
    assert [profile.name for profile in batch.profiles] == ["a", "b"]
    assert any("exceeded 2 files" in note for note in batch.diagnostics)


@pytest.mark.requires_posix
def test_directory_import_rejects_symlinked_roots(tmp_path: Path) -> None:
    write_agent(tmp_path / "outside/mini.md")
    (tmp_path / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    batch = import_agents([{"path": "linked", "format": "native"}], tmp_path)
    assert not batch.profiles
    assert any("symlink" in note for note in batch.diagnostics)


def test_native_model_reference_keeps_provider_and_id_together(tmp_path: Path) -> None:
    write_agent(tmp_path / ".ngn/agents/mini.md", "description: Coding\nmodel:\n  provider: work\n  id: gpt-6.1-sol")
    batch = discover_agents(tmp_path)
    assert not batch.diagnostics
    assert (batch.profiles[0].provider, batch.profiles[0].model) == ("work", "gpt-6.1-sol")
    assert batch.profiles[0].tools is None


@pytest.mark.parametrize(
    "model",
    [
        "{provider: work}",
        "{id: selected}",
        "{provider: '', id: ''}",
        "{provider: work, id: selected, endpoint: 'https://example.com'}",
    ],
)
def test_native_model_reference_rejects_partial_or_extra_fields(tmp_path: Path, model: str) -> None:
    write_agent(tmp_path / ".ngn/agents/mini.md", f"description: Coding\nmodel: {model}")
    batch = discover_agents(tmp_path)
    assert not batch.profiles and batch.diagnostics


def test_native_model_reference_rejects_separate_provider(tmp_path: Path) -> None:
    write_agent(
        tmp_path / ".ngn/agents/mini.md", "description: Coding\nmodel: {provider: work, id: selected}\nprovider: other"
    )
    batch = discover_agents(tmp_path)
    assert not batch.profiles
    assert "inside model" in batch.diagnostics[0]
