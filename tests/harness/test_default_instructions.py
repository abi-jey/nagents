"""The packaged coding policy composes with guarded workspace guidance."""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.default_instructions import load_default_instructions
from nagents.harness.runtime.core import Harness

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_programmatic_harness_uses_the_bundled_markdown_without_creating_workspace_files(tmp_path: Path) -> None:
    bundled = load_default_instructions()
    assert bundled
    harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True))
    try:
        prompt = harness.agent.system_prompt or ""
        assert prompt.count(bundled) == 1
        assert str(tmp_path) in prompt
        assert "active profile assistant" in prompt
        harness.refresh_instructions()
        assert harness.agent.system_prompt == prompt
        assert not (tmp_path / "AGENTS.md").exists()
        assert not harness.instructions
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_workspace_and_nested_instructions_compose_once_for_parent_and_children(tmp_path: Path) -> None:
    root_guidance = "Use the repository's task-specific verification checklist."
    nested_guidance = "For the nested module, use its more specific verification checklist."
    workspace_file = tmp_path / "AGENTS.md"
    workspace_file.write_text(root_guidance, encoding="utf-8")
    nested = tmp_path / "module"
    nested.mkdir()
    (nested / "AGENTS.md").write_text(nested_guidance, encoding="utf-8")
    (nested / "example.txt").write_text("content", encoding="utf-8")
    bundled = load_default_instructions()
    harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True))
    try:
        await harness.initialize(create_session=False)
        initial = harness.agent.system_prompt or ""
        assert initial.count(bundled) == 1
        assert initial.count(root_guidance) == 1
        assert nested_guidance not in initial
        await harness.tools.read_file("module/example.txt")
        child = harness.tasks._create_child("assistant")
        try:
            await child.initialize(create_session=False)
            for current in (harness, child):
                current.refresh_instructions()
                prompt = current.agent.system_prompt or ""
                assert prompt.count(bundled) == 1
                assert prompt.count(root_guidance) == 1
                assert prompt.count(nested_guidance) == 1
                assert prompt.index(bundled) < prompt.index(root_guidance) < prompt.index(nested_guidance)
                assert current.instructions == {"AGENTS.md": root_guidance, "module/AGENTS.md": nested_guidance}
        finally:
            await child.close()
        assert workspace_file.read_text(encoding="utf-8") == root_guidance
    finally:
        await harness.close()


@pytest.mark.parametrize("resource", ["missing", "empty", "invalid-encoding"])
def test_missing_or_invalid_bundled_instructions_fail_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    directory = tmp_path / "instructions"
    directory.mkdir()
    if resource == "empty":
        (directory / "assistant.md").write_text(" \n", encoding="utf-8")
    elif resource == "invalid-encoding":
        (directory / "assistant.md").write_bytes(b"\xff")
    monkeypatch.setattr(resources, "files", lambda anchor: tmp_path)
    with pytest.raises(RuntimeError, match="Reinstall nagents or restore") as failure:
        load_default_instructions()
    assert "nagents/harness/instructions/assistant.md" in str(failure.value)
