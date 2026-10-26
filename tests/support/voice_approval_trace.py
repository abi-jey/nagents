"""Failure-only voice approval state, without request data or task locals."""

from __future__ import annotations

import json
from types import CoroutineType
from types import GeneratorType
from typing import TYPE_CHECKING
from typing import Literal

if TYPE_CHECKING:
    import asyncio

    from nagents.web.execution.service import Run
    from nagents.web.execution.service import WebState

Stage = Literal["admission", "run_started", "model_request", "scripted_generate", "tool_call", "approve", "pending"]
_EVENT_STAGES: dict[str, Stage] = {
    "input_admitted": "admission",
    "run_started": "run_started",
    "model_request": "model_request",
    "approval": "pending",
}
_STATUSES = frozenset({"queued", "working", "completed", "failed", "cancelled"})


def _task(task: asyncio.Task[object]) -> dict[str, object]:
    chain: list[str] = []
    current: object = task.get_coro()
    for _ in range(6):
        if isinstance(current, CoroutineType):
            chain.append(current.cr_code.co_name[:48])
            current = current.cr_await
        elif isinstance(current, GeneratorType):
            chain.append(current.gi_code.co_name[:48])
            current = current.gi_yieldfrom
        else:
            break
    result: dict[str, object] = {
        "state": "cancelled" if task.cancelled() else "done" if task.done() else "pending",
        "cancelling": bool(task.cancelling()),
        "await_chain": chain,
    }
    if task.done() and not task.cancelled() and (error := task.exception()) is not None:
        result["exception_type"] = type(error).__name__[:48]
    return result


class VoiceApprovalTrace:
    def __init__(self) -> None:
        self.stages: set[Stage] = set()
        self.statuses: set[str] = set()

    def mark(self, stage: Stage) -> None:
        self.stages.add(stage)

    def report(self, update: dict[str, object]) -> None:
        status = update.get("status")
        if isinstance(status, str) and status in _STATUSES:
            self.statuses.add(status)

    def observe(self, run: Run | None, record: dict[str, object]) -> None:
        del run
        event = record.get("event")
        if isinstance(event, str) and event in _EVENT_STAGES:
            self.mark(_EVENT_STAGES[event])

    def note(self, error: TimeoutError, state: WebState, bridge: asyncio.Task[object]) -> None:
        run = state.active
        snapshot: dict[str, object] = {
            "stages": sorted(self.stages),
            "bridge_reports": sorted(self.statuses),
            "bridge_task": _task(bridge),
            "run_active": run is not None,
            "work_count": len(state.channels.work_tasks),
            "cleanup_count": len(state.channels.work_cleanup),
            "inbox_workers": [_task(task) for task in state.channels.tasks[:1]],
            "work_tasks": [_task(task) for task in tuple(state.channels.work_tasks.values())[:2]],
        }
        if run is not None:
            snapshot["run"] = {
                "pending_approval": run.pending is not None,
                "finished": run.finished,
                "executing": run.executing,
                "outcome": run.outcome if run.outcome in _STATUSES else "other",
                "task": _task(run.task),
            }
            if run.harness is not None and run.harness._worker is not None:
                snapshot["producer"] = _task(run.harness._worker)
        error.add_note("VOICE_APPROVAL_TRACE " + json.dumps(snapshot, separators=(",", ":")))
