"""Content-keyed native definition parsing must not weaken discovery checks."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.harness.agents.definitions import MAX_HEADER_BYTES
from nagents.harness.agents.definitions import _yaml_header
from nagents.harness.agents.definitions import discover_agents

if TYPE_CHECKING:
    from pathlib import Path


def native_agent(description: str, tools: str = "read_file") -> str:
    return f"---\nname: mini\ndescription: {description}\ntools: [{tools}]\n---\n"


def test_same_size_same_mtime_edit_is_discovered_from_new_bytes(tmp_path: Path) -> None:
    path = tmp_path / ".ngn/agents/mini/agent.md"
    path.parent.mkdir(parents=True)
    before = native_agent("Mini A")
    after = native_agent("Mini B")
    assert len(before.encode()) == len(after.encode())
    path.write_text(before, encoding="utf-8")
    original_stat = path.stat()
    first = discover_agents(tmp_path)
    assert first.failed is False
    assert first.profiles[0].description == "Mini A"

    path.write_text(after, encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    edited_stat = path.stat()
    assert edited_stat.st_size == original_stat.st_size
    assert edited_stat.st_mtime_ns == original_stat.st_mtime_ns

    refreshed = discover_agents(tmp_path)
    assert refreshed.failed is False
    assert refreshed.profiles[0].description == "Mini B"
    assert refreshed.profiles[0].tools == first.profiles[0].tools


def test_discovery_still_finds_new_files_and_removals_after_cache_warmup(tmp_path: Path) -> None:
    agents = tmp_path / ".ngn/agents"
    mini = agents / "mini/agent.md"
    mini.parent.mkdir(parents=True)
    mini.write_text(native_agent("Mini"), encoding="utf-8")
    assert [profile.name for profile in discover_agents(tmp_path).profiles] == ["mini"]

    helper = agents / "helper/agent.md"
    helper.parent.mkdir(parents=True)
    helper.write_text("---\nname: helper\ndescription: Helper\n---\n", encoding="utf-8")
    assert {profile.name for profile in discover_agents(tmp_path).profiles} == {"mini", "helper"}

    mini.unlink()
    assert [profile.name for profile in discover_agents(tmp_path).profiles] == ["helper"]


def test_yaml_cache_returns_deep_copies_for_nested_tool_policy() -> None:
    source = "tools:\n  - read_file\n  - list_files\ndisabled: false\n"
    first = _yaml_header(source)
    assert first["tools"] == ["read_file", "list_files"]
    cast("list[str]", first["tools"]).append("shell")
    second = _yaml_header(source)
    assert second["tools"] == ["read_file", "list_files"]


def test_yaml_cache_key_keeps_header_size_limit() -> None:
    source = "description: Mini\n"
    assert _yaml_header(source, max_bytes=MAX_HEADER_BYTES)["description"] == "Mini"
    with pytest.raises(ValueError, match="exceeds"):
        _yaml_header(source, max_bytes=1)


@pytest.mark.requires_posix
def test_cached_content_does_not_bypass_symlink_rejection(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(native_agent("Mini"), encoding="utf-8")
    path = tmp_path / ".ngn/agents/mini/agent.md"
    path.parent.mkdir(parents=True)
    path.write_text(native_agent("Mini"), encoding="utf-8")
    assert discover_agents(tmp_path).failed is False

    path.unlink()
    path.symlink_to(outside)
    rejected = discover_agents(tmp_path)
    assert rejected.failed is True
    assert not rejected.profiles
    assert any("symlink" in note for note in rejected.diagnostics)
