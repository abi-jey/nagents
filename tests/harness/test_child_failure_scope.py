"""Child failure stays observable without becoming a fatal root error."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.cli import _event_record
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskDeliveryWarning
from nagents.harness.types import TaskNotification
from tests.support.child_failures import ChildFailureScenario
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_parent", [False, True])
async def test_child_budget_failure_cancels_descendant_without_poisoning_recovered_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_parent: bool
) -> None:
    scenario = ChildFailureScenario(monkeypatch, wrong_parent=wrong_parent)
    harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "state", auth="api-key", max_tool_rounds=6))
    try:
        async with asyncio.timeout(HANG_GUARD):
            events = await collect(harness)
        scenario.assert_recovered()
        assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "ROOT_RECOVERED"
        completions = [event for event in events if isinstance(event, TaskCompleted)]
        assert sorted((event.depth, event.status) for event in completions) == [
            (1, "completed"),
            (1, "failed"),
            (2, "cancelled"),
        ]
        descendant = next(event for event in completions if event.depth == 2)
        assert descendant.result == "" and "cancelled" in descendant.error
        assert all(
            event.source_task_id != descendant.task_id for event in events if isinstance(event, TaskNotification)
        )
        errors = [event for event in events if isinstance(event, ErrorEvent)]
        assert len(errors) == 1
        error = errors[0]
        if wrong_parent:
            assert type(error) is ErrorEvent and not error.recoverable
            assert "parent is unavailable" in error.message
        else:
            assert isinstance(error, TaskDeliveryWarning) and error.recoverable
            assert error.code == "TASK_DELIVERY_SKIPPED"
            assert error.task_id == descendant.task_id
            assert error.parent_task_id == descendant.parent_task_id
            assert error.parent_session_id == descendant.parent_session_id
            assert error.child_session_id == descendant.child_session_id
            assert error.activation == descendant.activation and error.followup == descendant.followup
            cloned = replace(error)
            assert type(cloned) is TaskDeliveryWarning and cloned.recoverable and cloned.code == error.code
            record = _event_record(cloned)
            assert record["event"] == "error" and record["task_id"] == descendant.task_id
    finally:
        await harness.close()
    assert all(provider.closed for provider in scenario.providers)


@pytest.mark.asyncio
async def test_root_cancellation_still_cancels_and_joins_the_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = ChildFailureScenario(monkeypatch, hold_child=True)
    harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "state", auth="api-key", max_tool_rounds=6))
    task = asyncio.create_task(collect(harness))
    try:
        async with asyncio.timeout(HANG_GUARD):
            await scenario.grandchild_started.wait()
            await scenario.root_preview.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert {info.status for info in harness.tasks.list()} == {"cancelled"}
        assert len(scenario.providers) == 3
        assert all(provider.closed for provider in scenario.providers[1:])
        assert "DESCENDANT_PRIVATE" not in str(scenario.providers[0].requests)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await harness.close()
