"""Native search and wakeup contracts agree with actual guarded Harness calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.types import ToolCall
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
@pytest.mark.asyncio
@pytest.mark.parametrize("absolute", [False, True])
async def test_actual_harness_search_accepts_a_file_and_preserves_limits_and_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool
) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "AGENTS.md").write_text("Single-file search fixture instructions.")
    target = folder / "sample.txt"
    target.write_text("first\nneedle in target\nneedle later\n")
    (folder / "sibling.txt").write_text("needle in sibling\n")
    path = str(target) if absolute else "src/sample.txt"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="search", name="search", arguments={"query": "needle", "path": path, "limit": 1})
        else:
            assert "src/sample.txt:2: needle in target" in str(messages[-1].content)
            yield TextDoneEvent(text="Found the requested literal in the specified file.")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        events = await collect(harness, "Search only src/sample.txt for needle.")
        result = next(event for event in events if isinstance(event, ToolResultEvent))
        assert not result.error
        assert result.result == {
            "matches": ["src/sample.txt:2: needle in target"],
            "truncated": True,
            "skipped_files": 0,
        }
        assert "Single-file search fixture instructions." in str(harness.agent.system_prompt)
        assert len(providers[0].requests) == 2 and isinstance(events[-1], DoneEvent)
        definition = harness.agent.tool_registry.get("search")
        assert definition is not None and "file or directory" in definition.description
        assert "1 through 1000" in definition.parameters["properties"]["limit"]["description"]
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["ignored-file", "ignored-parent", "pruned-parent", "symlink", "protected", "binary", "oversized", "hardlink"],
)
async def test_direct_file_search_keeps_discovery_and_snapshot_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    target = tmp_path / "target.txt"
    target.write_text("needle")
    if case == "ignored-file":
        (tmp_path / ".gitignore").write_text("target.txt\n")
    elif case in {"ignored-parent", "pruned-parent"}:
        folder = tmp_path / ("hidden" if case == "ignored-parent" else ".venv")
        folder.mkdir()
        target = folder / "target.txt"
        target.write_text("needle")
        if case == "ignored-parent":
            (tmp_path / ".gitignore").write_text("hidden/\n")
            (folder / ".gitignore").write_text("!target.txt\n")
            (folder / "AGENTS.md").write_text("Ignored instructions must stay undiscovered.")
    elif case == "symlink":
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        target = link
    elif case == "protected":
        target = tmp_path / ".env"
        target.write_text("needle")
    elif case == "binary":
        target.write_bytes(b"needle\x00")
    elif case == "oversized":
        target.write_text("needle" + "x" * harness.config.max_file_bytes)
    elif case == "hardlink":
        (tmp_path / "alias.txt").hardlink_to(target)
    try:
        await harness.initialize()
        result = await harness.agent.tool_executor.execute(
            ToolCall("search", "search", {"query": "needle", "path": target.relative_to(tmp_path).as_posix()})
        )
        if case in {"symlink", "protected"}:
            assert result.error and result.result is None
        else:
            assert not result.error
            assert result.result == {
                "matches": [],
                "truncated": False,
                "skipped_files": int(case in {"binary", "oversized", "hardlink"}),
            }
        assert "Ignored instructions must stay undiscovered." not in str(harness.agent.system_prompt)
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["schedule_wakeup", "wake_up_in"])
async def test_actual_harness_wakeup_requires_advertised_reason_and_acknowledges_valid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    scheduled: list[tuple[str, float, str]] = []

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="missing", name=alias, arguments={"seconds": 1})
        elif len(provider.requests) == 2:
            assert "missing reason" in str(messages[-1].content)
            assert "reason: string (required)" in str(messages[-1].content)
            yield ToolCallEvent(
                id="valid", name=alias, arguments={"seconds": 0.5, "minutes": 1, "reason": " Check requested work "}
            )
        else:
            yield TextDoneEvent(text="The requested wakeup was scheduled.")

    async def schedule(owner: str, delay: float, reason: str) -> dict[str, str]:
        scheduled.append((owner, delay, reason))
        return {"status": "scheduled", "wakeup_id": "fixture"}

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.wakeup_handler = schedule
    try:
        definition = harness.agent.tool_registry.get(alias)
        assert definition is not None and definition.parameters["required"] == ["reason"]
        properties = definition.parameters["properties"]
        assert "default" not in properties["reason"]
        assert "nonblank" in properties["reason"]["description"] and "2000" in properties["reason"]["description"]
        assert "additive" in definition.description and "604800" in definition.description
        assert "greater than zero" in definition.description
        for unit in ("seconds", "minutes", "hours", "days"):
            assert "nonnegative" in properties[unit]["description"]
        events = await collect(harness, "Schedule the requested follow-up.")
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert results[0].error and not results[1].error
        assert results[1].result == {"status": "scheduled", "wakeup_id": "fixture"}
        assert scheduled == [("", 60.5, "Check requested work")]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_wakeup_metadata_is_inherited_by_native_children_without_rewriting_custom_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Unused")

    async def custom_schedule(reminder: str) -> str:
        """Custom reminder contract."""
        return reminder

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    child = harness.tasks._create_child("assistant")
    try:
        for owner in (harness, child):
            for alias in ("schedule_wakeup", "wake_up_in"):
                definition = owner.agent.tool_registry.get(alias)
                assert definition is not None and definition.parameters["required"] == ["reason"]
        replacement = harness.agent.register_tool(custom_schedule, name="schedule_wakeup")
        harness.refresh_instructions()
        assert replacement.parameters["required"] == ["reminder"]
        assert set(replacement.parameters["properties"]) == {"reminder"}
        assert replacement.description == "Custom reminder contract."
    finally:
        await child.close()
        await harness.close()
