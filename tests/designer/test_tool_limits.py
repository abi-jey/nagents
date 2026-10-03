"""Explicit designed instructions and current built-in tool contracts coexist."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.designer.runtime import DesignedHarness
from nagents.designer.schema import STARTER
from nagents.designer.schema import AgentDefinition
from nagents.designer.schema import Instructions
from nagents.designer.schema import Invocation
from nagents.designer.schema import ToolSelection
from nagents.designer.schema import parse
from nagents.harness.config import HarnessConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_designed_root_and_defined_child_publish_limits_without_rewriting_explicit_instructions(
    tmp_path: Path,
) -> None:
    design = parse(STARTER)
    selected = [ToolSelection(ref="builtin.shell"), ToolSelection(ref="builtin.read_file")]
    root_text, child_text = "Explicit root instructions only.", "Explicit worker instructions only."
    design.agents["assistant"].instructions.text = root_text
    design.agents["assistant"].tools = selected
    design.agents["assistant"].invokes = [Invocation(agent="worker", description="Inspect a bounded task.")]
    design.agents["worker"] = AgentDefinition(instructions=Instructions(text=child_text), tools=selected)
    config = HarnessConfig(
        tmp_path, data_dir=tmp_path / "data", demo=True, shell_timeout=1.25, max_file_bytes=4096, max_output=2048
    )
    harness = DesignedHarness(config, design)
    child = harness.tasks._create_child("worker")
    try:
        expected_root = root_text + "\n\n" + harness.delegation_description()
        assert harness.agent.system_prompt == expected_root
        assert child.agent.system_prompt == child_text
        for owner in (harness, child):
            owner.refresh_instructions()
            shell, read = owner.agent.tool_registry.get("shell"), owner.agent.tool_registry.get("read_file")
            assert shell is not None and read is not None
            assert "1.25 seconds" in shell.description and "2048 bytes" in shell.description
            assert "configured default of 1.25" in shell.parameters["properties"]["timeout"]["description"]
            assert "4096 bytes" in read.description and "even when requesting a line slice" in read.description
            assert "1 through 1000" in read.parameters["properties"]["limit"]["description"]
        assert harness.agent.system_prompt == expected_root
        assert child.agent.system_prompt == child_text

        harness.config.shell_timeout = 2.5
        harness.config.max_file_bytes = 8192
        harness.refresh_instructions()
        shell, read = harness.agent.tool_registry.get("shell"), harness.agent.tool_registry.get("read_file")
        assert shell is not None and read is not None
        assert "2.5 seconds" in shell.description and "8192 bytes" in read.description
        assert harness.agent.system_prompt == expected_root
    finally:
        await child.close()
        await harness.close()
