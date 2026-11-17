"""Project instructions belong in the system prompt, not repeated tool results."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["read_file", "skill"])
async def test_repeated_tool_results_do_not_duplicate_instructions_in_model_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    root_marker = "UNIQUE_ROOT_PROJECT_GUIDANCE"
    nested_marker = "UNIQUE_NESTED_PROJECT_GUIDANCE"
    (tmp_path / "AGENTS.md").write_text(root_marker)
    folder = tmp_path / ".agents/skills/example"
    folder.mkdir(parents=True)
    (folder / "AGENTS.md").write_text(nested_marker)
    skill_text = "---\nname: example\ndescription: Example skill\n---\nUnique skill body.\n"
    (folder / "SKILL.md").write_text(skill_text)
    (folder / "data.txt").write_text("Unique file body.\n")
    arguments = {"path": ".agents/skills/example/data.txt"} if tool == "read_file" else {"name": "example"}

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        turn = len(provider.requests)
        if turn <= 2:
            yield ToolCallEvent(id=f"call-{turn}", name=tool, arguments=arguments)
        else:
            yield TextDoneEvent(text="Done")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize()
        await collect(harness, "Read the same document twice.")
        assert len(providers[0].requests) == 3
        for request in providers[0].requests[1:]:
            system = "\n".join(str(message.content) for message in request if message.role == "system")
            assert system.count(root_marker) == 1
            assert system.count(nested_marker) == 1
            results = [str(message.content) for message in request if message.role == "tool"]
            assert results
            for result in results:
                assert root_marker not in result
                assert nested_marker not in result
                assert "Applicable project context" not in result
                assert "Unique file body." in result if tool == "read_file" else "Unique skill body." in result
        assert harness.tools.read_hashes
    finally:
        await harness.close()
