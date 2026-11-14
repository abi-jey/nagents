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
from nagents.harness.runtime.task_activity import ChildActivity
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskDeliveryWarning
from nagents.harness.types import TaskNotification
from tests.support.child_failures import ChildFailureScenario
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.extensions import RunContext


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_parent", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_child_budget_failure_cancels_descendant_without_poisoning_recovered_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_parent: bool, cleanup_failure: bool
) -> None:
    scenario = ChildFailureScenario(monkeypatch, wrong_parent=wrong_parent)
    after_run = ChildActivity.after_run
    cleanup_failures: list[str] = []

    async def failing_after_run(activity: ChildActivity, context: RunContext) -> None:
        await after_run(activity, context)
        if cleanup_failure and activity.info.depth == 1 and activity.info.error:
            cleanup_failures.append(activity.info.session_id)
            raise RuntimeError("PRIVATE_CLEANUP_DETAIL")

    monkeypatch.setattr(ChildActivity, "after_run", failing_after_run)
    harness = Harness(
        HarnessConfig(tmp_path, data_dir=tmp_path / "state", providers=connection(auth="api-key"), max_tool_rounds=6)
    )
    try:
        async with asyncio.timeout(HANG_GUARD):
            events = await collect(harness)
        scenario.assert_recovered()
        assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "ROOT_RECOVERED"
        completions = [event for event in events if isinstance(event, TaskCompleted)]
        failed = next(event for event in completions if event.depth == 1 and event.status == "failed")
        assert "tool-round limit" in failed.error
        assert cleanup_failures == ([failed.session_id] if cleanup_failure else [])
        assert "PRIVATE_CLEANUP_DETAIL" not in repr(events) + repr(harness.tasks.list())
        assert sorted((event.depth, event.status) for event in completions) == [
            (1, "completed"),
            (1, "failed"),
            (2, "cancelled"),
        ]
        descendant = next(event for event in completions if event.depth == 2)
        assert descendant.result == "" and "cancelled" in descendant.error
        assert all(
            event.source_session_id != descendant.session_id for event in events if isinstance(event, TaskNotification)
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
            assert error.session_id == descendant.session_id
            assert error.parent_session_id == descendant.parent_session_id
            assert error.root_session_id == descendant.root_session_id
            assert error.activation == descendant.activation and error.followup == descendant.followup
            cloned = replace(error)
            assert type(cloned) is TaskDeliveryWarning and cloned.recoverable and cloned.code == error.code
            record = _event_record(cloned)
            assert record["event"] == "error" and record["session_id"] == descendant.session_id
    finally:
        await harness.close()
    assert all(provider.closed for provider in scenario.providers)


@pytest.mark.asyncio
async def test_descendant_initialization_failure_releases_barrier_and_reports_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = ChildFailureScenario(monkeypatch, fail_descendant_initialization=True)
    harness = Harness(
        HarnessConfig(tmp_path, data_dir=tmp_path / "state", providers=connection(auth="api-key"), max_tool_rounds=6)
    )
    try:
        async with asyncio.timeout(HANG_GUARD):
            await collect(harness)
        assert scenario.descendant_started_or_terminal.is_set()
        assert not scenario.grandchild_started.is_set()
        with pytest.raises(
            AssertionError,
            match=r"descendant terminated before provider startup.*injected descendant provider initialization failure",
        ):
            scenario.assert_recovered()
    finally:
        await harness.close()
    assert all(provider.closed for provider in scenario.providers)
    task_infos = harness.tasks.list()
    assert task_infos
    assert all(info.status in {"completed", "failed", "cancelled"} for info in task_infos)


@pytest.mark.asyncio
async def test_root_cancellation_still_cancels_and_joins_the_whole_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = ChildFailureScenario(monkeypatch, hold_child=True)
    harness = Harness(
        HarnessConfig(tmp_path, data_dir=tmp_path / "state", providers=connection(auth="api-key"), max_tool_rounds=6)
    )
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
