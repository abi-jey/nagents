"""Durable display-only generation snapshots, including interrupted responses."""

from __future__ import annotations

from dataclasses import asdict
from functools import partial
from time import monotonic
from time import time
from typing import TYPE_CHECKING

from nagents.harness.execution import _is_owned_adapter
from nagents.session import SessionManager
from nagents.session import response_metadata as storage

from ._async import finish_on_cancel

if TYPE_CHECKING:
    import sqlite3

    from nagents.events import ResponseMetadataEvent

    from .service import Run
    from .service import WebState

_FIELDS = (
    "response_id",
    "response_session_id",
    "revision",
    "summary",
    "summary_truncated",
    "summary_expired",
    "usage_reported",
    "usage",
    "history_id",
    "round_number",
    "status",
)


class Responses:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.pending: dict[str, dict[str, dict[str, object]]] = {}
        self.captured: dict[str, dict[str, dict[str, object]]] = {}
        self.checkpoints: dict[tuple[str, str], float] = {}
        self.totals: dict[tuple[str, str], dict[str, object]] = {}

    def capture(self, run: Run, adapter: object, event: ResponseMetadataEvent) -> None:
        """Remember producer evidence before a queued stream can be cancelled."""
        if not isinstance(adapter, SessionManager) or not _is_owned_adapter(adapter):
            return
        payload = {key: value for key, value in asdict(event).items() if key in _FIELDS}
        payload.update(event="response_metadata", run_id=run.id)
        owner = run.harness or self.state.running_harness
        if event.response_session_id == run.session_id:
            payload["turn_id"] = owner.followups.current.id
        else:
            info = next(
                (task for task in owner.tasks.list() if task.child_session_id == event.response_session_id), None
            )
            if info is not None:
                child_owner = owner.tasks._children.get(info.id)
                payload.update(
                    task_id=info.id,
                    activation=info.activation,
                    followup=info.followups,
                    turn_id=child_owner.followups.current.id if child_owner is not None else "",
                )
        responses = self.captured.setdefault(run.id, {})
        responses[event.response_id] = payload
        same_session = [
            key for key, value in responses.items() if value.get("response_session_id") == event.response_session_id
        ]
        for key in same_session[: -storage.SUMMARY_RETENTION]:
            responses[key].update(summary="", summary_expired=True)

    async def initialize(self) -> None:
        # Only startup invokes this. A previous server's partial responses have
        # no live provider stream to resume; retain the evidence as interrupted.
        await self.state.history.store._transaction(
            lambda db: db.execute(
                "UPDATE ngn_response_metadata SET status = 'cancelled', revision = revision + 1 "
                "WHERE status IN ('thinking', 'responding')"
            )
        )

    async def record(self, run: Run, record: dict[str, object]) -> None:
        child = record.get("payload") if record.get("event") == "task_activity" else None
        payload = child if isinstance(child, dict) else record
        if payload.get("type" if child is not None else "event") != "response_metadata":
            return
        if not _is_owned_adapter(self.state.running_harness.agent.session):
            return
        response, session = payload.get("response_id"), payload.get("response_session_id")
        if not isinstance(response, str) or not response or not isinstance(session, str) or not session:
            return
        if child is None and session != run.session_id:
            return
        if child is not None and session != record.get("child_session_id"):
            return
        canonical = {key: payload[key] for key in _FIELDS if key in payload}
        canonical.update(event="response_metadata", run_id=run.id)
        captured = self.captured.get(run.id, {}).get(response, {})
        canonical.update(
            {key: captured[key] for key in ("task_id", "activation", "followup", "turn_id") if key in captured}
        )
        canonical.update(
            {key: record[key] for key in ("task_id", "activation", "followup", "turn_id") if key in record}
        )
        pending = self.pending.setdefault(run.id, {})
        previous = pending.get(response, {})
        previous_payload = previous.get("payload", previous)
        previous_payload = previous_payload if isinstance(previous_payload, dict) else {}
        # Preserve the newest snapshot before awaiting storage. Cancellation
        # cleanup checkpoints it even when a provider chunk races that await.
        pending[response] = {**record, "payload": dict(canonical)} if child is not None else dict(canonical)
        checkpoint_key = (run.id, response)
        now = monotonic()
        checkpoint = (
            not previous
            or canonical.get("status") in {"completed", "cancelled", "failed"}
            or canonical.get("usage") != previous_payload.get("usage")
            or canonical.get("status") != previous_payload.get("status")
            or now - self.checkpoints.get(checkpoint_key, 0) >= 0.1
        )
        if checkpoint:
            saved = await finish_on_cancel(self.state.history.store._transaction(partial(_save, record=canonical)))
            self.checkpoints[checkpoint_key] = now
            aggregate = saved.get("run_usage")
            if isinstance(aggregate, dict):
                self.totals[(run.id, session)] = aggregate
        else:
            saved = {**canonical, "run_usage": self.totals.get((run.id, session), {})}
        # The producer may have committed an answer while this older snapshot
        # waited in the Harness queue. Storage returns the newer durable record
        # to protect it, but publishing that record here would announce completion
        # again when the real terminal event arrives. Preserve the queued event's
        # revision and phase; cancellation finalization still reads durable state.
        published = {**canonical, "run_usage": saved.get("run_usage", {})}
        payload.update(published)
        if child is not None:
            payload["type"] = "response_metadata"
        if published.get("status") in {"completed", "cancelled", "failed"}:
            pending.pop(response, None)
            self.checkpoints.pop(checkpoint_key, None)
            captured_responses = self.captured.get(run.id, {})
            if int(str(captured_responses.get(response, {}).get("revision", 0))) <= int(
                str(published.get("revision", 0))
            ):
                captured_responses.pop(response, None)
        else:
            pending[response] = {**record, "payload": dict(payload)} if child is not None else dict(payload)

    async def finish(self, run: Run, task_id: str = "", status: str = "") -> None:
        pending = self.pending.setdefault(run.id, {})
        for response, payload in list(self.captured.get(run.id, {}).items()):
            previous = pending.get(response, {})
            body = previous.get("payload", previous)
            if isinstance(body, dict) and int(str(body.get("revision", 0))) >= int(str(payload["revision"])):
                continue
            if payload["response_session_id"] == run.session_id:
                pending[response] = dict(payload)
                continue
            owner = run.harness or self.state.running_harness
            info = next(
                (task for task in owner.tasks.list() if task.child_session_id == payload["response_session_id"]), None
            )
            if info is None or (task_id and info.id != task_id):
                continue
            child_owner = owner.tasks._children.get(info.id)
            pending[response] = {
                "event": "task_activity",
                "task_id": info.id,
                "root_session_id": run.session_id,
                "parent_task_id": info.parent_task_id,
                "child_session_id": info.child_session_id,
                "depth": info.depth,
                "activation": payload.get("activation", info.activation),
                "turn_id": payload.get("turn_id", child_owner.followups.current.id if child_owner is not None else ""),
                "payload": {**payload, "type": "response_metadata", "task_id": info.id},
            }
        for response, record in list(pending.items()):
            child = record.get("payload") if record.get("event") == "task_activity" else None
            payload = dict(child) if isinstance(child, dict) else dict(record)
            if task_id and record.get("task_id") != task_id:
                continue
            payload.update(
                status=payload["status"]
                if payload["status"] in {"completed", "failed", "cancelled"}
                else status or ("cancelled" if run.outcome == "cancelled" else "failed"),
                revision=int(str(payload["revision"])) + 1,
            )
            canonical = {
                key: payload[key]
                for key in (*_FIELDS, "event", "run_id", "task_id", "activation", "followup", "turn_id")
                if key in payload
            }
            saved = await self.state.history.store._transaction(partial(_save, record=canonical))
            payload.update(saved)
            if child is not None:
                # Both captured-only and already-published thinking snapshots
                # need a fresh canonical cursor for this synthetic terminal.
                # Reusing the pending envelope's cursor makes clients drop it.
                owner = run.harness or self.state.running_harness
                info = owner.tasks._lookup(str(record["task_id"]))
                info.activity_sequence += 1
                payload["type"] = "response_metadata"
                final = {**record, "payload": payload, "sequence": info.activity_sequence, "timestamp": time()}
            else:
                final = payload
            pending.pop(response, None)
            self.captured.get(run.id, {}).pop(response, None)
            self.checkpoints.pop((run.id, response), None)
            self.state.publish(run, final)
        if not pending:
            self.pending.pop(run.id, None)
            if not self.captured.get(run.id):
                self.captured.pop(run.id, None)
            for key in list(self.totals):
                if key[0] == run.id:
                    self.totals.pop(key, None)


def _save(db: sqlite3.Connection, *, record: dict[str, object]) -> dict[str, object]:
    saved = storage.save(db, record)
    if saved.get("run_id"):
        saved["run_usage"] = storage.run_totals(db, str(saved["response_session_id"]), str(saved["run_id"]))
    return saved
