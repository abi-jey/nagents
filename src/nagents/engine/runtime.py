"""The versioned engine API's adapter to the existing workspace execution owner."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException

from nagents._version import framework_version
from nagents.protocol import PROTOCOL_REVISION
from nagents.protocol import PROTOCOL_VERSION
from nagents.protocol.events import SessionUpdated
from nagents.protocol.models import Approval
from nagents.protocol.models import ApprovalReceipt
from nagents.protocol.models import EngineConfig
from nagents.protocol.models import EngineInfo
from nagents.protocol.models import EngineSnapshot
from nagents.protocol.models import InputReceipt
from nagents.protocol.models import Model
from nagents.protocol.models import Provider
from nagents.protocol.models import QueueEntry
from nagents.protocol.models import QueueState
from nagents.protocol.models import Run
from nagents.protocol.models import Session
from nagents.protocol.models import SteeringReceipt
from nagents.protocol.models import SteeringState
from nagents.protocol.models import ToolCall
from nagents.session.forks import SessionForkError
from nagents.session.forks import fork_in
from nagents.session.forks import rename_in
from nagents.session.forks import title_text
from nagents.web._async import finish_on_cancel
from nagents.web._async import join_owned
from nagents.web.execution.approvals import ToolApprovals
from nagents.web.execution.engine_inputs import ENGINE_QUEUE_LIMIT
from nagents.web.execution.engine_inputs import EngineAdmission
from nagents.web.execution.engine_inputs import EngineWork
from nagents.web.execution.engine_inputs import SteeringAdmission
from nagents.web.execution.engine_inputs import SteeringWork
from nagents.web.execution.engine_inputs import paused
from nagents.web.execution.engine_inputs import set_paused
from nagents.web.execution.owners import MAX_PARALLEL_RUNS
from nagents.web.http.routing import RoutingStore
from nagents.web.sessions.actions import quiescent

from . import archives
from .journal import EngineJournal
from .journal import append_in
from .journal import fork_display_in
from .task_projection import activation_in
from .task_projection import children_in
from .task_projection import task_run_in

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from fastapi import WebSocket

    from nagents.harness import Harness
    from nagents.harness.types import SessionInfo
    from nagents.protocol.events import History
    from nagents.protocol.models import ApprovalDecision
    from nagents.protocol.models import InputLookup
    from nagents.protocol.models import QueueUpdate
    from nagents.protocol.models import RunStart
    from nagents.protocol.models import SessionCreate
    from nagents.protocol.models import SessionFork
    from nagents.protocol.models import SessionRename
    from nagents.protocol.models import SteeringInput
    from nagents.protocol.models import TaskInfo
    from nagents.web.execution.service import Pending
    from nagents.web.execution.service import Run as WebRun
    from nagents.web.execution.service import WebState

    from .task_projection import ChildActivation

CAPABILITIES = (
    "sessions",
    "history",
    "runs",
    "queue",
    "steering",
    "approvals",
    "tasks",
    "events",
    "providers",
    "models",
    "config",
)
_STEERING_COLUMNS = ("input_id", "request_id", "run_id", "session_id", "id", "prompt", "status")
_STEERING_SELECT = (
    "SELECT engine_steer_id, engine_request_id, engine_steer_run, session_id, id, prompt, engine_steer_state "
    "FROM ngn_web_inbox "
)


def _timestamp(value: str) -> int:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)  # SQLite's CURRENT_TIMESTAMP is UTC.
    return int(stamp.timestamp() * 1000)


def _session(info: SessionInfo) -> Session:
    return Session(id=info.id, title=info.title, updated_at_ms=_timestamp(info.updated_at), archived=False)


def _title(value: str) -> str:
    try:
        return title_text(value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _session_in(db: sqlite3.Connection, session_id: str) -> Session:
    row = db.execute(
        "SELECT h.id, h.title, s.updated_at FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id WHERE h.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Session not found in this workspace.")
    return Session(id=row[0], title=row[1] or "New session", updated_at_ms=_timestamp(row[2]), archived=False)


def _read_run(db: sqlite3.Connection, run_id: str) -> Run:
    row = db.execute(
        "SELECT engine_run_id, session_id, engine_request_id, status FROM ngn_web_inbox "
        "WHERE engine_run_id = ? AND engine_run_id != ''",
        (run_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Engine run not found.")
    RoutingStore.root(db, row[1])
    return Run.model_validate(dict(zip(("id", "session_id", "request_id", "status"), row, strict=True)))


def _child_run(child: ChildActivation) -> Run:
    task = child.task
    return Run(id=task.run_id, session_id=task.session_id, request_id=child.request_id, status=task.status)


def _message_id(session_id: str, request_id: str) -> str:
    # Internally distinct from all browser UUIDs; external UUIDs are scoped
    # to a root and canonicalized only for comparison, not overwritten.
    return f"engine:{session_id}:{request_id.lower()}"


def _input_receipt(db: sqlite3.Connection, session_id: str, request_id: str, prompt: str) -> InputReceipt | None:
    RoutingStore.execution_root(db, session_id)
    row = db.execute(
        "SELECT engine_request_id, engine_run_id, status, prompt FROM ngn_web_inbox "
        "WHERE session_id = ? AND channel = '' AND message_id = ? AND engine_run_id != ''",
        (session_id, _message_id(session_id, request_id)),
    ).fetchone()
    if row is None:
        return None
    if row[3] != prompt:
        raise HTTPException(409, "Message ID already belongs to a different submission.")
    return InputReceipt(request_id=row[0], session_id=session_id, run_id=row[1], status=row[2])


def _queue(db: sqlite3.Connection, session_id: str) -> QueueState:
    if archives.get_in(db, session_id) is not None:
        return QueueState(session_id=session_id, paused=False, entries=[], limit=0)
    RoutingStore.root(db, session_id)
    return QueueState(
        session_id=session_id,
        paused=paused(db, session_id),
        entries=[
            QueueEntry(request_id=request_id, prompt=prompt, run_id=run_id)
            for request_id, prompt, run_id in db.execute(
                "SELECT engine_request_id, prompt, engine_run_id FROM ngn_web_inbox "
                "WHERE session_id = ? AND engine_run_id != '' AND status = 'queued' ORDER BY id",
                (session_id,),
            )
        ],
        limit=ENGINE_QUEUE_LIMIT,
    )


def _steering_receipt(row: tuple[object, ...]) -> SteeringReceipt:
    values = dict(zip(_STEERING_COLUMNS, row, strict=True))
    values["position"] = values.pop("id")  # The inbox's durable FIFO order.
    return SteeringReceipt.model_validate(values)


def _steering_receipts(db: sqlite3.Connection, run: Run) -> list[SteeringReceipt]:
    return [
        _steering_receipt(row)
        for row in db.execute(
            _STEERING_SELECT + "WHERE session_id = ? AND engine_steer_run = ? AND engine_steer_run != '' ORDER BY id",
            (run.session_id, run.id),
        )
    ]


def _steering_supported(state: WebState, harness: Harness) -> bool:
    return harness.agent.session is state.history and not harness._remote_profile.configured and not harness.agent.batch


def _steering_owner(state: WebState, run: WebRun | None) -> Harness | None:
    if (
        run is None
        or run.work is None
        or run.work.engine is None
        or run.finished
        or run.draining
        or run.source
        or run.task.done()
        or run.task.cancelling()
        or run.agent != state.session_agents.selected(run.session_id)
    ):
        return None
    harness = run.harness
    if harness is None or not _steering_supported(state, harness) or not harness.followups.accepting:
        return None
    return harness


def _provider_id(harness: Harness) -> str:
    if harness.config.demo:
        return "demo"
    if harness._remote_profile.configured:
        return "a2a"
    return harness.config.provider or harness.config.provider_profile().kind


def _provider_ids(harness: Harness) -> dict[str, str]:
    if harness.config.demo:
        return {"demo": "demo"}
    items = {key: profile.kind for key, profile in harness.config.providers.items() if key}
    active = _provider_id(harness)
    items.setdefault(active, "a2a" if harness._remote_profile.configured else harness.config.provider_profile().kind)
    return items


def _open_approval(run: WebRun) -> Pending | None:
    engine = run.work.engine if run.work is not None else None
    pending = run.pending
    if (
        engine is not None
        and engine.interactive
        and pending is not None
        and pending.engine_ready
        and pending.request is not None
        and pending.harness is not None
        and not pending.answer.done()
        and not run.finished
        and not run.task.done()
        and not run.task.cancelling()
        and ToolApprovals.active_request_owner(pending.harness, pending.request) is not None
    ):
        return pending
    return None


class EngineRuntime:
    """Translate the public protocol without owning a second agent, queue or provider."""

    def __init__(self, get_state: Callable[[], WebState]) -> None:
        self._get_state = get_state
        self._attached: tuple[WebState, EngineJournal] | None = None

    def _current(self) -> tuple[WebState, EngineJournal]:
        state = self._get_state()
        if self._attached is None or self._attached[0] is not state:
            journal = EngineJournal(state.channels.store)
            state.engine_event_sink = journal.capture
            self._attached = state, journal
        return self._attached

    async def info(self) -> EngineInfo:
        state = self._get_state()
        capabilities = list(CAPABILITIES)
        if state.harness.agent.session is not state.history:
            capabilities.remove("steering")
        return EngineInfo(
            protocol_version=PROTOCOL_VERSION,
            protocol_revision=PROTOCOL_REVISION,
            version=framework_version(),
            capabilities=capabilities,
        )

    async def config(self) -> EngineConfig:
        harness = self._get_state().harness
        return EngineConfig(
            workspace=str(harness.workspace),
            provider=_provider_id(harness),
            model=None if harness._remote_profile.configured else harness.agent.provider.model,
            read_only=harness.config.demo or harness.mode == "reviewer",
        )

    async def sessions(self) -> list[Session]:
        state, journal = self._current()
        active = [_session(info) for info in await state.list_sessions()]
        imported = await journal.read(archives.list_in)
        return active + sorted(imported, key=lambda item: item.updated_at_ms, reverse=True)

    async def session(self, session_id: str) -> Session:
        _, journal = self._current()
        return await journal.read(lambda db: archives.get_in(db, session_id) or _session_in(db, session_id))

    async def _edit_session(
        self, edit: Callable[[sqlite3.Connection], str], *, guard: Callable[[], None] | None = None
    ) -> Session:
        state, journal = self._current()

        def write(db: sqlite3.Connection) -> Session:
            updated = _session_in(db, edit(db))
            append_in(db, updated.id, "", SessionUpdated(session=updated))
            return updated

        async def change() -> Session:
            updated = await state.channels.store._transaction(write)
            state.session_revision += 1
            journal.changed()
            sessions = await state.list_sessions()
            state.bus.publish({"type": "sessions", "sessions": [asdict(info) for info in sessions]})
            return updated

        # The browser's separate selection/draft cleanup is not part of an
        # engine client mutation. It can keep watching and running any root.
        async with state.queued_mutation():
            if guard is not None:
                guard()
            return await finish_on_cancel(change())

    async def create_session(self, body: SessionCreate) -> Session:
        # An explicitly created engine session is durable even before its
        # first input, unlike the browser's disposable unnamed draft.
        title = _title(body.title)
        return await self._edit_session(lambda db: RoutingStore.new_root(db, title))

    async def rename_session(self, session_id: str, body: SessionRename) -> Session:
        title = _title(body.title)

        def rename(db: sqlite3.Connection) -> str:
            RoutingStore.root(db, session_id)
            rename_in(db, session_id, title)
            return session_id

        return await self._edit_session(rename)

    async def fork_session(self, session_id: str, body: SessionFork) -> Session:
        state = self._get_state()
        title = (
            _title(body.title)
            if body.title is not None
            else ("New session" if "title" in body.model_fields_set else "")
        )

        def fork(db: sqlite3.Connection) -> str:
            RoutingStore.root(db, session_id)
            try:
                target = fork_in(db, session_id, title)
            except SessionForkError as error:
                raise HTTPException(409, str(error)) from None
            fork_display_in(db, session_id, target)
            return target

        return await self._edit_session(fork, guard=lambda: quiescent(state, session_id))

    async def history(self, session_id: str, after: int, limit: int) -> History:
        _, journal = self._current()
        return await journal.history(session_id, after, limit)

    async def start_run(self, session_id: str, body: RunStart) -> InputReceipt:
        state, journal = self._current()

        async def accept() -> InputReceipt:
            async with state.queued_mutation():
                occupied = (
                    set(state.executions.runs) | set(state.channels.work_tasks) | set(state.channels.work_cleanup)
                )
                harness = state.harness
                profile = state.session_agents.selected(session_id)
                harness.refresh_agent_definitions()
                try:
                    available = (
                        harness.config.demo
                        or harness.config.profile(profile).a2a.configured
                        or bool(harness.resolved_model_binding(profile).binding.provider)
                    )
                except (OSError, ValueError):
                    available = False
                engine = EngineAdmission(
                    work=EngineWork(
                        request_id=body.request_id, run_id=f"run-{uuid4().hex}", interactive=body.interactive
                    ),
                    mode=body.mode,
                    lane_busy=session_id in occupied or len(occupied) >= MAX_PARALLEL_RUNS,
                    provider_unavailable=not available,
                )
                await state.channels.store.web(
                    session_id,
                    _message_id(session_id, body.request_id),
                    body.prompt,
                    delivery="queue",
                    engine_admission=engine,
                )
                receipt = await journal.read(lambda db: _input_receipt(db, session_id, body.request_id, body.prompt))
                assert receipt is not None
                state.channels.changed.set()
                journal.changed()
                return receipt

        return await finish_on_cancel(accept())

    async def input_receipt(self, session_id: str, body: InputLookup) -> InputReceipt | None:
        _, journal = self._current()
        return await journal.read(lambda db: _input_receipt(db, session_id, body.request_id, body.prompt))

    async def cancel_run(self, run_id: str) -> Run:
        state, journal = self._current()
        if run_id.startswith("task-"):
            child = await journal.read(lambda db: task_run_in(db, run_id))
            if child is None:
                raise HTTPException(404, "Child activation not found.")
            return await self._cancel_child(state, journal, child)

        async def cancel() -> Run:
            async with state.queued_mutation():
                live = state.run_by_id(run_id)
                if live is not None and (
                    live.work is None or live.work.engine is None or live.work.engine.run_id != run_id
                ):
                    live = None
                owner = state.channels.work_tasks.get(live.session_id) if live is not None else None

                def mark(db: sqlite3.Connection) -> Run:
                    current = _read_run(db, run_id)
                    if current.status == "queued":
                        db.execute(
                            "UPDATE ngn_web_inbox SET status = 'cancelled' WHERE engine_run_id = ? AND status = 'queued'",
                            (run_id,),
                        )
                        return current.model_copy(update={"status": "cancelled"})
                    if current.status == "running":
                        if live is None or live.session_id != current.session_id or owner is None:
                            raise HTTPException(
                                409, "This run is finishing. Retry shortly to read its durable outcome."
                            )
                        db.execute(
                            "UPDATE ngn_web_inbox SET engine_cancel = 1 WHERE engine_run_id = ? AND status = 'running'",
                            (run_id,),
                        )
                    return current

                current = await state.channels.store._transaction(mark)
                journal.changed()
            if current.status != "running":
                return current
            # Only the original claim, not a later run in this lane, may be
            # stopped or joined. Do not hold the workspace mutation guard while
            # this run closes provider/tool I/O and settles its inbox row.
            assert live is not None and owner is not None
            if state.run_by_id(run_id) is live:
                await state.stop(live)
            with suppress(asyncio.CancelledError):
                await join_owned(owner)
            journal.changed()
            return await journal.read(lambda db: _read_run(db, run_id))

        return await finish_on_cancel(cancel())

    async def _cancel_child(self, state: WebState, journal: EngineJournal, child: ChildActivation) -> Run:
        if child.task.status != "running":
            return _child_run(child)

        async def cancel() -> Run:
            root = state.run_by_id(child.task.root_run_id)
            if (
                root is None
                or root.finished
                or root.session_id != child.task.root_session_id
                or root.harness is None
                or root.work is None
                or root.work.engine is None
                or root.work.engine.run_id != child.task.root_run_id
            ):
                raise HTTPException(409, "The child's root is no longer running.")
            try:
                result = await root.harness.tasks.cancel_activation(child.actor, child.number)
            except (ValueError, RuntimeError) as error:
                raise HTTPException(409, str(error)) from None
            await journal.finish_child(child, result.status, result.error)
            final = await journal.read(lambda db: task_run_in(db, child.task.run_id))
            if final is None:
                raise HTTPException(404, "Child activation was removed.")
            return _child_run(final)

        return await finish_on_cancel(cancel())

    async def queue(self, session_id: str) -> QueueState:
        _, journal = self._current()
        return await journal.read(lambda db: _queue(db, session_id))

    async def control_queue(self, session_id: str, body: QueueUpdate) -> QueueState:
        state, journal = self._current()

        def control(db: sqlite3.Connection) -> QueueState:
            RoutingStore.execution_root(db, session_id)
            set_paused(db, session_id, body.action != "resume")
            if body.action == "clear":
                db.execute(
                    "UPDATE ngn_web_inbox SET status = 'cancelled' WHERE session_id = ? "
                    "AND engine_run_id != '' AND status = 'queued'",
                    (session_id,),
                )
            return _queue(db, session_id)

        async def change() -> QueueState:
            async with state.queued_mutation():
                result = await state.channels.store._transaction(control)
                state.channels.changed.set()
                journal.changed()
                return result

        return await finish_on_cancel(change())

    async def steering(self, run_id: str) -> SteeringState:
        state, journal = self._current()

        def fetch(db: sqlite3.Connection) -> tuple[Run, list[SteeringReceipt]]:
            run = _read_run(db, run_id)
            return run, _steering_receipts(db, run)

        run, receipts = await journal.read(fetch)
        live = state.run_by_id(run_id)
        harness = live.harness if live is not None and live.harness is not None else state.harness
        return SteeringState(
            run_id=run.id,
            session_id=run.session_id,
            run_status=run.status,
            supported=_steering_supported(state, harness) or bool(receipts),
            accepting=_steering_owner(state, live) is not None,
            receipts=receipts,
        )

    async def steer(self, run_id: str, body: SteeringInput) -> SteeringReceipt:
        state, journal = self._current()

        async def accept() -> SteeringReceipt:
            async with state.queued_mutation():
                run = await journal.read(lambda db: _read_run(db, run_id))
                live = state.run_by_id(run_id)
                owner = _steering_owner(state, live)
                message_id = f"engine-steer:{run_id}:{body.request_id.lower()}"
                _, admitted = await state.channels.store.web(
                    run.session_id,
                    message_id,
                    body.prompt,
                    delivery="steer",
                    steering_admission=SteeringAdmission(
                        work=SteeringWork(body.request_id, run_id, f"input-{uuid4().hex}"),
                        accepting=owner is not None,
                    ),
                )
                if admitted and owner is not None and _steering_owner(state, state.run_by_id(run_id)) is owner:
                    owner.followups.changed()
                    owner.tasks._changed.set()
                journal.changed()

                def receipt(db: sqlite3.Connection) -> SteeringReceipt:
                    row = db.execute(
                        _STEERING_SELECT
                        + "WHERE session_id = ? AND engine_steer_run = ? AND channel = '' AND message_id = ?",
                        (run.session_id, run_id, message_id),
                    ).fetchone()
                    assert row is not None
                    return _steering_receipt(row)

                return await journal.read(receipt)

        return await finish_on_cancel(accept())

    async def tasks(self, session_id: str) -> list[TaskInfo]:
        _, journal = self._current()

        def tasks_in(db: sqlite3.Connection) -> list[TaskInfo]:
            if archives.get_in(db, session_id) is not None:
                return []
            RoutingStore.root(db, session_id)
            return children_in(db, session_id)

        return await journal.read(tasks_in)

    async def snapshot(self) -> EngineSnapshot:
        state, journal = self._current()
        runs: list[Run] = []
        approvals: list[Approval] = []
        for live in tuple(state.executions.runs.values()):
            engine = live.work.engine if live.work is not None else None
            if engine is None or live.finished:
                continue
            runs.append(Run(id=live.id, session_id=live.session_id, request_id=engine.request_id, status="running"))
            pending = _open_approval(live)
            if pending is None or pending.request is None:
                continue
            request = pending.request
            child = (
                await journal.read(
                    partial(activation_in, root_run=live.id, actor=request.session_id, activation=request.activation)
                )
                if request.session_id not in ("", live.session_id)
                else None
            )
            if _open_approval(live) is not pending:
                continue
            if child is not None and child.task.status != "running":
                continue
            approvals.append(
                Approval(
                    id=pending.id,
                    run_id=child.task.run_id if child is not None else live.id,
                    session_id=child.task.session_id if child is not None else request.session_id or live.session_id,
                    root_session_id=live.session_id,
                    call=ToolCall.model_validate(
                        {"call_id": request.id, "name": request.tool, "arguments": request.arguments}
                    ),
                    preview=request.preview,
                    agent_title=request.title,
                    depth=request.depth,
                )
            )
        return EngineSnapshot(runs=runs, approvals=approvals)

    async def decide_approval(self, approval_id: str, body: ApprovalDecision) -> ApprovalReceipt:
        state, journal = self._current()
        child = await journal.read(lambda db: task_run_in(db, body.run_id)) if body.run_id.startswith("task-") else None
        run = state.run_by_id(child.task.root_run_id if child is not None else body.run_id)
        pending = _open_approval(run) if run is not None else None
        if run is None or pending is None or pending.request is None:
            raise HTTPException(409, "Approval is stale, unknown, or already decided.")
        request = pending.request
        if child is not None:
            matches = (
                child.task.status == "running"
                and child.task.root_session_id == run.session_id
                and child.actor == request.session_id
                and child.number == request.activation
            )
        else:
            # For a child with a frozen public ID, the root ID alone cannot
            # authorize its operation. Unprojected host approvals retain the
            # exact root/nonce/call correlation.
            bound = await journal.read(lambda db: activation_in(db, run.id, request.session_id, request.activation))
            matches = bound is None and _open_approval(run) is pending
        if not matches or pending.id != approval_id or pending.call_id != body.call_id:
            raise HTTPException(409, "Approval is stale, unknown, or already decided.")
        # No suspension between checking the nonce/call/run and setting the
        # executor's actual future. This grant is never persisted or reused.
        pending.decision = "allow" if body.allow else "deny"
        pending.answer.set_result(body.allow)
        return ApprovalReceipt(approval_id=approval_id, allowed=body.allow)

    async def providers(self) -> list[Provider]:
        harness = self._get_state().harness
        return [Provider(id=name, kind=kind) for name, kind in sorted(_provider_ids(harness).items())]

    async def models(self, provider_id: str) -> list[Model]:
        harness = self._get_state().harness
        if provider_id not in _provider_ids(harness):
            raise HTTPException(404, "Provider connection not found.")
        # Only configured metadata, never credentials or a remote provider
        # query on behalf of a read-only client.
        models: set[str] = set()
        if not harness._remote_profile.configured and provider_id == _provider_id(harness):
            models.add(harness.agent.provider.model)
        profile = harness.config.providers.get(provider_id)
        if profile is not None:
            models.update(profile.model_context_windows)
        if not harness.config.demo:
            for name in harness.config.profile_names:
                with suppress(OSError, ValueError):
                    binding = harness.resolved_model_binding(name).binding
                    if binding.provider == provider_id:
                        models.add(binding.id)
        return [Model(id=model, provider_id=provider_id, label=model) for model in sorted(models) if model]

    async def events(self, socket: WebSocket, after: int, session_id: str) -> None:
        _, journal = self._current()
        await journal.events(socket, after, session_id, await self.info())
