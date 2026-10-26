"""One Harness execution owner shared by HTTP streaming, queued sources and timers."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from contextlib import aclosing
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextlib import nullcontext
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException

from nagents.cli import _event_record
from nagents.cli import _json_default
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.execution import bind_channel_send
from nagents.harness.execution import host_run
from nagents.harness.runtime import _HarnessSession
from nagents.harness.types import TaskDeliveryWarning
from nagents.observation import scope as observation_scope
from nagents.response_metadata import observe_responses

from .._async import finish_on_cancel
from .._async import join_owned as _join
from ..channels.host import ChannelHost
from ..channels.notices import ChannelNotices
from ..channels.replies import automatic_reply
from ..channels.uploads import Uploads
from ..diagnostics import exception_diagnostic
from ..history import WebHistory
from ..live.inspection import model_requests
from ..session_agents import SessionAgents
from ..settings.provider_setup import provider_error
from ..settings.provider_setup import provider_error_diagnostic
from ..settings.provider_setup import provider_setup
from ..settings.remote_metadata import RemoteMetadataRefresh
from ..settings.subagents import WebSubagentSettings
from ..tasks.routes import Tasks
from ..trash import SessionTrash
from .approvals import ToolApprovals
from .approvals import ToolBinding
from .delivery_transcript import DeliveryTranscript
from .owners import MAX_PARALLEL_RUNS
from .owners import Executions
from .queued_inputs import QueuedInputs
from .queued_inputs import SkillInvocationError
from .replay import RunReplay
from .response_metadata import Responses
from .subscriptions import EventBus
from .wakeups import Chain
from .wakeups import Wakeups

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterator

    from nagents.harness import Harness
    from nagents.harness.config import HarnessConfig
    from nagents.harness.followups import RunInput
    from nagents.harness.followups import RunTurn
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import SessionInfo
    from nagents.types import ContentPart

    from ..routing import Work
    from ..settings.preferences import WebSettings
    from .wakeups import Wakeup

APPROVAL_TIMEOUT = 300
MUTATION_WAIT_TIMEOUT = 5
logger = logging.getLogger("uvicorn.error")


@dataclass
class Pending:
    id: str
    call_id: str
    answer: asyncio.Future[bool]
    record: dict[str, object] = field(default_factory=dict)
    in_chat: bool = False
    binding: ToolBinding | None = None
    request: ApprovalRequest | None = None
    harness: Harness | None = None
    decision: str = ""


@dataclass
class Run:
    session_id: str
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    task: asyncio.Task[None] = field(init=False)
    pending: Pending | None = None
    notices: ChannelNotices | None = field(default=None, repr=False)
    outcome: str = "completed"
    final_text: str = ""
    finished: bool = False
    executing: bool = False
    draining: bool = False
    background: bool = False
    server_owned: bool = False
    voice: bool = False
    work: Work | None = field(default=None, repr=False)
    wakeup_id: str = ""
    harness: Harness | None = field(default=None, repr=False)
    message_id: str = ""
    task_message_id: str = ""
    agent: str = ""
    read_only: bool = False
    source: dict[str, str] = field(default_factory=dict)
    chain: Chain = field(default_factory=Chain)
    drafts: dict[str, dict[str, object]] = field(default_factory=dict)
    draft_sizes: dict[str, int] = field(default_factory=dict)
    draft_bytes: int = 0
    _context_reply_live: bool = False
    replay: RunReplay = field(default_factory=RunReplay)

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "status": "approval" if self.pending else "running",
            "background": self.background,
            "server_owned": self.server_owned,
            "message_id": self.message_id,
            "approval": self.pending.record if self.pending else {},
            "records": list(self.drafts.values()),
            **self.replay.snapshot(),
        }

    @property
    def context_reply(self) -> str:
        if self._context_reply_live:
            for key, record in reversed(self.drafts.items()):
                if key.startswith(":") and key.endswith(":text_chunk"):
                    return str(record.get("chunk", ""))
        return ""

    def remember(self, record: dict[str, object]) -> None:
        self.replay.append(record)
        event = record.get("event")
        extra = record.get("extra", {})
        extra = extra if isinstance(extra, dict) else {}
        scope = (
            f"{record.get('session_id', extra.get('session_id', ''))}:{record.get('activation', extra.get('activation', 0))}"
            f":{record.get('followup', extra.get('followup', 0))}"
        )
        generation = record.get("generation_id", extra.get("generation_id", ""))
        index = record.get("index", extra.get("index", ""))
        progress_key = f"{scope}:progress:{generation}:{index}"
        correlated = isinstance(generation, str) and bool(generation) and type(index) is int and 0 <= index < 1024
        call_id = record.get("call_id", record.get("id", ""))
        tool_slot = json.dumps([call_id, generation if correlated else "", index if correlated else -1])
        if not record.get("session_id", extra.get("session_id", "")):
            if event == "text_chunk" and not any(key.startswith(f"{scope}:call:") for key in self.drafts):
                self._context_reply_live = True
            elif event in {"text_done", "tool_call_progress", "tool_call", "tool_result", "done", "compaction_started"}:
                self._context_reply_live = False
        if event == "response_metadata":
            self.retain(f"response:{record.get('response_id', '')}", record)
        elif event in {"text_chunk", "reasoning_chunk"}:
            key = f"{scope}:{event}"
            old = self.drafts.get(key, {})
            self.retain(key, {**record, "chunk": (str(old.get("chunk", "")) + str(record.get("chunk", "")))[-262144:]})
        elif event == "tool_call_progress":
            if record.get("status") == "abandoned":
                self.forget(progress_key)
            else:
                self.retain(progress_key, record)
        elif event == "tool_call":
            self.forget(progress_key)
            self.retain(f"{scope}:call:{tool_slot}", record)
        elif event == "tool_execution_started":
            self.retain(f"{scope}:started:{tool_slot}", record)
        elif event == "transcript_anchor":
            calls = record.get("calls", [])
            if isinstance(calls, list):
                for key, draft in list(self.drafts.items()):
                    if (
                        draft.get("event") != "tool_call"
                        or not draft.get("transcript_event_id")
                        or not key.startswith(f"{scope}:")
                    ):
                        continue
                    for call in calls:
                        if isinstance(call, dict) and call.get("event_id") == draft.get("transcript_event_id"):
                            self.retain(
                                key,
                                {
                                    **draft,
                                    "history_id": record.get("history_id", ""),
                                    "call_position": call.get("call_position"),
                                },
                            )
        elif event == "tool_output":
            key = f"{scope}:output:{tool_slot}"
            old = self.drafts.get(key, {})
            self.retain(key, {**record, "text": (str(old.get("text", "")) + str(record.get("text", "")))[-262144:]})
        elif event == "tool_result":
            if correlated:
                for kind in ("call", "started", "output"):
                    self.forget(f"{scope}:{kind}:{tool_slot}")
            else:
                # Legacy uncorrelated results may clear one unambiguous call,
                # never a newer generation that happens to reuse the same ID.
                matches: list[str] = []
                identities: set[str] = set()
                for key, draft in self.drafts.items():
                    if (
                        not key.startswith(f"{scope}:")
                        or draft.get("event") not in {"tool_call", "tool_execution_started", "tool_output"}
                        or draft.get("call_id", draft.get("id", "")) != call_id
                    ):
                        continue
                    metadata = draft.get("extra", {})
                    metadata = metadata if isinstance(metadata, dict) else {}
                    identity = draft.get("generation_id", metadata.get("generation_id", ""))
                    if identity:
                        identities.add(str(identity))
                    matches.append(key)
                if len(identities) <= 1:
                    for key in matches:
                        self.forget(key)
        elif event in {"text_done", "done", "task_completed"}:
            self.forget(f"{scope}:text_chunk")
            self.forget(f"{scope}:reasoning_chunk")
        if event in {"done", "error", "task_completed", "run_finished"}:
            for key in list(self.drafts):
                if ":progress:" in key and (event == "run_finished" or key.startswith(f"{scope}:")):
                    self.forget(key)

    def forget(self, key: str) -> None:
        self.drafts.pop(key, None)
        self.draft_bytes -= self.draft_sizes.pop(key, 0)

    def retain(self, key: str, record: dict[str, object]) -> None:
        self.draft_bytes -= self.draft_sizes.get(key, 0)
        self.drafts[key] = record
        self.draft_sizes[key] = len(json.dumps(record, default=_json_default, ensure_ascii=True))
        self.draft_bytes += self.draft_sizes[key]
        while len(self.drafts) > 64 or self.draft_bytes > 1024 * 1024:
            self.forget(next(iter(self.drafts)))


class WebState:
    settings: WebSettings

    def __init__(self, harness: Harness, factory: Callable[[HarnessConfig], Harness] | None = None) -> None:
        self.harness = harness
        self.remote_metadata_refresh = RemoteMetadataRefresh()
        self.subagent_settings = WebSubagentSettings(self)
        harness.tasks.settings_host = self.subagent_settings
        self._running_harness = harness
        self.approval_timeout: Callable[[], float] = lambda: APPROVAL_TIMEOUT
        self.selected_session_id = harness.session_id
        self.session_revision = 0
        self.executions = Executions(self, factory or type(harness))
        self.mutating = False
        self.mutation_released = asyncio.Event()
        self.mutation_released.set()
        self.bus = EventBus()
        self.run_observers: set[Callable[[Run | None, dict[str, object]], None]] = set()
        self.history = WebHistory(harness.agent.session.db_path, self.user_message)
        self.tasks = Tasks(self)
        self.queued_inputs = QueuedInputs(self)
        self.tool_approvals = ToolApprovals(
            harness.agent.session.db_path.with_name("tool-approvals.db"), harness.workspace
        )
        # Preserve explicit custom persistence adapters. Their unlinked rows still
        # get stable SQLite history IDs, without fabricated ingress annotations.
        if type(harness.agent.session) is _HarnessSession:
            harness.agent.session = self.history
        self.wakeups = Wakeups(
            lambda: not self.mutating,
            self.wake,
            observer=self.activity_event,
            available=lambda session_id: (
                session_id not in self.executions.runs
                and session_id not in self.channels.work_tasks
                and len(set(self.executions.runs) | set(self.channels.work_tasks)) < MAX_PARALLEL_RUNS
            ),
            parallel=True,
            reserve=self.reserve_wakeup,
        )
        self.channels = ChannelHost(self)
        self.session_agents = SessionAgents(self)
        self.responses = Responses(self)
        self.uploads = Uploads(self)
        self.trash = SessionTrash(self)
        harness.approval_handler = self.approve
        harness.wakeup_handler = self.schedule

    @property
    def active(self) -> Run | None:
        return self.executions.active()

    @active.setter
    def active(self, run: Run | None) -> None:
        if run is not None:
            self.executions.reserve(run)
        elif (current := self.active) is not None:
            self.executions.release(current)

    @property
    def running_harness(self) -> Harness:
        current = self.executions.current.get()
        if current is not None and current.harness is not None:
            return current.harness
        active = self.active
        return active.harness if active is not None and active.harness is not None else self._running_harness

    @running_harness.setter
    def running_harness(self, harness: Harness) -> None:
        current = self.executions.current.get()
        if current is not None:
            current.harness = harness
        else:
            self._running_harness = harness

    def run_for(self, session_id: str) -> Run | None:
        return self.executions.runs.get(session_id)

    def run_by_id(self, run_id: str) -> Run | None:
        return next((run for run in self.executions.runs.values() if run.id == run_id), None)

    def active_runs(self) -> list[dict[str, str]]:
        return [
            {"id": run.id, "session_id": run.session_id, "status": "approval" if run.pending else "running"}
            for run in self.executions.runs.values()
        ]

    def harness_for(self, session_id: str) -> Harness:
        run = self.run_for(session_id)
        return (run.harness if run is not None else None) or self.executions.owners.get(session_id, self.harness)

    def reserve_work(self, work: Work) -> Run:
        run = Run(
            work.session_id, server_owned=True, message_id=work.message_id, voice=bool(work.voice_session_id), work=work
        )
        if work.channel:
            run.source = {"channel": work.channel, "conversation_id": work.conversation_id, "thread_id": work.thread_id}
        self.executions.reserve(run)
        self.queued_inputs.admitted(run, work, initial=True)
        self.publish(run, {"event": "run_started", "message_id": work.message_id, "channel": work.channel})
        return run

    def reserve_wakeup(self, wakeup: Wakeup, task: asyncio.Task[None]) -> bool:
        if (
            self.mutating
            or self.executions.closed
            or wakeup.root_session_id in self.channels.work_tasks
            or wakeup.root_session_id in self.executions.runs
            or len(set(self.executions.runs) | set(self.channels.work_tasks)) >= MAX_PARALLEL_RUNS
        ):
            return False
        run = Run(wakeup.root_session_id, background=True, chain=wakeup.chain, wakeup_id=wakeup.id)
        run.task = task
        self.executions.reserve(run)
        return True

    def changed(self) -> None:
        self.wakeups.changed.set()
        self.channels.changed.set()

    @contextmanager
    def idle(self, *, allow_running: bool = False) -> Iterator[None]:
        if (self.executions.runs and not allow_running) or self.mutating:
            raise HTTPException(409, "Harness busy. Cancel or finish the active operation first.")
        self.mutating = True
        self.mutation_released.clear()
        try:
            yield
        finally:
            self.mutating = False
            self.mutation_released.set()
            self.changed()

    @asynccontextmanager
    async def queued_mutation(self, *, allow_running: bool = True) -> AsyncIterator[None]:
        """Wait briefly for bookkeeping without waiting for model execution.

        Claiming inbox work and empty trash sweeps can briefly own the same
        mutation guard as preference writes and session deletion. Keep their
        exclusion intact without turning bookkeeping into a visible conflict.
        Callers that require quiescence recheck active runs after waiting.
        """
        try:
            async with asyncio.timeout(MUTATION_WAIT_TIMEOUT):
                while self.mutating:
                    await self.mutation_released.wait()
        except TimeoutError:
            raise HTTPException(409, "Workspace update is still in progress. Try again shortly.") from None
        if self.executions.closed:
            raise HTTPException(503, "The server is shutting down. Try again after it restarts.")
        # No await between checking the guard and acquiring it. A competing
        # waiter must observe the new owner before it can enter this block.
        with self.idle(allow_running=allow_running):
            yield

    @asynccontextmanager
    async def configure(self) -> AsyncIterator[None]:
        """Serialize preference writes while allowing existing runs to continue."""
        async with self.queued_mutation():
            await _join(asyncio.create_task(self.executions.configuration_owner()))
            yield

    async def list_sessions(self) -> list[SessionInfo]:
        sessions: list[SessionInfo] = []

        async def read() -> None:
            # Cancellation between aiosqlite execute() returning its raw cursor
            # and the caller consuming it can pin an unread SELECT in the task's
            # cancellation traceback, retaining a reader lock even after close().
            # Let fetch/close finish in an owned task before cancellation escapes.
            sessions.extend(await self.harness.list_sessions())

        while True:
            revision = self.session_revision
            sessions.clear()
            await _join(asyncio.create_task(read()))
            if revision == self.session_revision:
                return sessions

    async def snapshot(self, session_id: str = "") -> dict[str, object]:
        session_id = session_id or self.selected_session_id
        try:
            _, data = await self.bus.checkpoint(session_id, self._snapshot)
            return data
        except TimeoutError:
            raise HTTPException(503, "Session snapshot is busy. Retry shortly.") from None

    async def _snapshot(self, session_id: str) -> dict[str, object]:
        sessions = await self.list_sessions()
        if session_id not in {session.id for session in sessions}:
            raise HTTPException(404, "Session not found in this workspace.")
        history = await self.history.snapshot(session_id)
        active = self.run_for(session_id)
        task_index = await self.tasks.index(
            session_id, history_ids=frozenset(str(row["history_id"]) for row in history if row.get("history_id"))
        )
        return {
            "session_id": session_id,
            "activity_cursor": self.wakeups.cursor,
            "retained_tasks": task_index["tasks"],
            "retained_activations": task_index["activations"],
            "retained_activations_omitted": task_index["activations_omitted"],
            "sessions": [asdict(session) for session in sessions],
            "history": history,
            "agent_selection": await self.session_agents.snapshot(session_id),
            "active_run": active.snapshot() if active is not None and active.session_id == session_id else None,
            "active_session_id": active.session_id if active else "",
            "active_run_id": active.id if active else "",
            "active_runs": self.active_runs(),
        }

    async def context_stats(self, session_id: str) -> dict[str, object]:
        """Read the library's provider-owned root context snapshot.

        Reads persisted history and the active agent's configuration, plus its
        bounded uncommitted reply estimate. Safe while a run is active; this
        host does not implement a separate token or reasoning calculation.
        """
        if session_id not in {session.id for session in await self.list_sessions()}:
            raise HTTPException(404, "Session not found in this workspace.")
        active = self.run_for(session_id)
        if active is not None and self.harness_for(session_id).session_id == session_id:
            stats = await self.harness_for(session_id).context_stats(session_id)
        else:
            async with self.session_agents.view(session_id) as harness:
                stats = await harness.context_stats(session_id)
        return stats.as_dict()

    def user_message(self, run_id: str, record: dict[str, object]) -> None:
        run = self.run_by_id(run_id)
        if run is not None and run.id == run_id:
            self.publish(run, {**record, "event": "user_message", "text": record["content"]})

    def live_captions(self, session_id: str, voice_session_id: str) -> Callable[[dict[str, object]], Awaitable[None]]:
        """Freeze the admitted root and publish each caption only after commit."""

        async def observe(event: dict[str, object]) -> None:
            async def persist() -> None:
                record = await self.history.add_live_caption(session_id, voice_session_id, event)
                if record:
                    self.bus.event(
                        {
                            **record,
                            "event": "live_caption",
                            "text": record["content"],
                            "root_session_id": session_id,
                            "schema_version": 1,
                        }
                    )

            await finish_on_cancel(persist())

        return observe

    async def bind_live_captions(
        self, session_id: str, voice_session_id: str
    ) -> Callable[[dict[str, object]], Awaitable[None]]:
        await self.history.register_live_call(session_id, voice_session_id)
        return self.live_captions(session_id, voice_session_id)

    def disconnected(self) -> None:
        # Durable runs and their bounded pending decisions belong to their
        # chats, not to whichever browser subscription happens to be visible.
        # HTTP approval still requires a live subscriber and the exact nonce.
        pass

    async def send(self, run: Run, record: dict[str, object]) -> None:
        await self.responses.record(run, record)
        if record.get("event") == "task_completed":
            await self.responses.finish(run, str(record.get("session_id", "")), str(record.get("status", "failed")))
        await self.tasks.record(run, record)
        self.publish(run, record)

    async def approve(self, request: ApprovalRequest) -> bool:
        run = self.active
        if run is None or run.pending is not None or run.finished or run.task.done() or run.task.cancelling():
            return False
        # Display correlation only. The nonce and the real executor request
        # remain the authority for approving an operation.
        identity: dict[str, object] = {}
        invocation = observation_scope.get()
        generation = invocation.get("tool_generation_id")
        index = invocation.get("tool_index")
        if (
            invocation.get("tool_call_id") == request.id
            and invocation.get("tool_name") == request.tool
            and isinstance(generation, str)
            and generation
            and type(index) is int
            and 0 <= index < 1024
        ):
            identity = {"generation_id": generation, "index": index}
        harness = self.running_harness
        binding = None if request.mandatory else self.tool_approvals.requested_binding(harness, request)
        try:
            allowed = binding is not None and await self.tool_approvals.check_allowed(binding)
        except sqlite3.Error:
            allowed = False  # Unreadable saved policy requires an explicit decision.
        if (
            self.active is not run
            or self.running_harness is not harness
            or run.pending is not None
            or run.finished
            or run.task.done()
            or run.task.cancelling()
            or (None if request.mandatory else self.tool_approvals.requested_binding(harness, request)) != binding
        ):
            return False
        if allowed:
            await self.send(
                run,
                {
                    "event": "notice",
                    "automatic": True,
                    "policy": "workspace_tool_allow",
                    **identity,
                    "text": f"{request.tool} is always allowed in this workspace.",
                    "session_id": request.session_id,
                    "activation": request.activation,
                    "call_id": request.id,
                    "tool": request.tool,
                },
            )
            return True
        if await automatic_reply(self, run, request):
            self.publish(
                run,
                {
                    "event": "notice",
                    "automatic": True,
                    "policy": "channel_auto_reply",
                    **identity,
                    "text": "Automatic approval for this session's permanently owned chat under its connection's automatic-message policy.",
                    "session_id": request.session_id,
                    "activation": request.activation,
                    "call_id": request.id,
                    "tool": request.tool,
                },
            )
            return True
        in_chat = False
        if (run.background or run.server_owned) and not self.bus.listening(run.session_id):
            # A browser subscriber is normally required. An opted-in owning chat
            # may instead decide the approval from the channel itself.
            in_chat = await self.channels.in_chat_approvals(run.session_id)
            if not in_chat:
                self.publish(
                    run,
                    {
                        "event": "notice",
                        "text": "Unattended approval was denied; no action was taken.",
                        "session_id": request.session_id,
                        "activation": request.activation,
                        "call_id": request.id,
                        "tool": request.tool,
                    },
                )
                return False
        if self.active is not run or run.finished or run.task.done() or run.task.cancelling():
            return False
        pending = Pending(
            secrets.token_urlsafe(24), request.id, asyncio.get_running_loop().create_future(), in_chat=in_chat
        )
        pending.binding = binding
        pending.request = replace(request)
        pending.harness = self.running_harness
        pending.record = {
            "event": "approval",
            **asdict(request),
            **identity,
            "approval_id": pending.id,
            "run_id": run.id,
            "allow_tool": binding is not None,
            "allow_tool_persistent": binding.persistent if binding else False,
        }
        run.pending = pending
        self.status()
        approved = False
        expired = False
        try:
            await self.send(run, pending.record)
            if run.notices is not None:
                await run.notices.waiting_for_approval(request, live=True)
            approved = await asyncio.wait_for(pending.answer, timeout=self.approval_timeout())
            return approved
        except TimeoutError:
            expired = True
            await self.send(run, {"event": "notice", "text": "Approval expired and was denied."})
            return False
        finally:
            if not pending.answer.done():
                pending.answer.set_result(False)
            run.pending = None
            self.status()
            if not run.task.cancelling():
                await self.send(
                    run,
                    {
                        "event": "approval_closed",
                        **identity,
                        "approval_id": pending.id,
                        "session_id": request.session_id,
                        "activation": request.activation,
                        "call_id": request.id,
                        "decision": (pending.decision or "allow") if approved else "deny",
                        "expired": expired,
                    },
                )

    async def schedule(self, agent_session_id: str, delay: float, reason: str) -> dict[str, str]:
        run = self.active
        if run is None or run.task.cancelling() or run.task.done() or run.session_id != self.running_harness.session_id:
            raise RuntimeError("Wakeups require an active run in their originating session")
        return self.wakeups.schedule(run.session_id, run.id, run.chain, agent_session_id, delay, reason)

    def publish(self, run: Run, record: dict[str, object]) -> None:
        record = {**record, "schema_version": 1, "run_id": run.id, "root_session_id": run.session_id}
        if run.background:
            self.wakeups.publish(record)  # Observer forwards scheduled work to the same WS bus.
        else:
            run.remember(record)
            self.bus.event(record)
            self.observe_run(run, record)

    def observe_run(self, run: Run | None, record: dict[str, object]) -> None:
        """Notify attached application consumers without giving them run ownership."""
        for observer in tuple(self.run_observers):
            try:
                observer(run, record)
            except Exception:
                logger.warning("An attached run observer could not process an update.")

    def activity_event(self, record: dict[str, object]) -> None:
        # This also captures lifecycle records emitted directly by the scheduler,
        # such as scheduled/fired, without duplicating background model events.
        run = self.run_by_id(str(record.get("run_id", "")))
        if run is not None and record.get("run_id") == run.id:
            run.remember(record)
        self.bus.event(record)
        observed = record
        if record.get("event") == "wakeup" and record.get("status") == "scheduled":
            observed = {**record, "_turn": self.running_harness.followups.current}
        self.observe_run(run if run is not None and record.get("run_id") == run.id else None, observed)

    def status(self) -> None:
        active = self.active
        self.bus.publish(
            {
                "type": "status",
                "active_session_id": active.session_id if active else "",
                "active_run_id": active.id if active else "",
                "active_runs": self.active_runs(),
            }
        )

    async def produce(
        self,
        run: Run,
        prompt: str | list[ContentPart],
        *,
        agent_session_id: str = "",
        task_agent: str = "",
        task_input: RunInput | None = None,
        task_title: str = "",
    ) -> None:
        notices = run.notices = ChannelNotices(self, run)
        transcript = DeliveryTranscript(
            capture=type(self.running_harness.agent.session) in {WebHistory, _HarnessSession}
        )
        try:
            await self.channels.activity(run.session_id, True, run.source)
            await notices.start(live=True)
            if task_input is not None:
                assert isinstance(prompt, str)
                source = (
                    self.running_harness.start_task(prompt, task_agent, title=task_title, message=task_input)
                    if task_agent
                    else self.running_harness.continue_task(agent_session_id, prompt, message=task_input)
                )
            elif run.background:
                if not isinstance(prompt, str):
                    raise ValueError("Background wake prompts must be text")
                source = self.running_harness.wake(prompt, session_id=agent_session_id)
            else:
                source = self.running_harness.run(prompt)
            definition = self.running_harness.agent.tool_registry.get("channel_send")
            binding = (
                bind_channel_send(self.running_harness, definition)
                if definition is not None and any(definition is tool for tool in self.channels.tools)
                else nullcontext()
            )

            def turn_started(turn: RunTurn) -> None:
                notices.turn(turn)
                self.observe_run(
                    run, {"event": "root_turn", "root_session_id": run.session_id, "run_id": run.id, "_turn": turn}
                )

            with (
                binding,
                host_run(self.running_harness, run.id),
                observe_responses(lambda adapter, event: self.responses.capture(run, adapter, event)),
                self.running_harness.followups.source(
                    lambda: self.queued_inputs.pull(run),
                    (run.work.voice_session_id, run.work.voice_delegation_id) if run.work is not None else ("", ""),
                ),
                self.running_harness.followups.observe(turn_started),
                model_requests(
                    run.session_id,
                    self.running_harness.agent.provider,
                    lambda request: self.observe_run(
                        run,
                        {
                            "event": "model_request",
                            "root_session_id": run.session_id,
                            "run_id": run.id,
                            "request": request,
                            "_turn": self.running_harness.followups.current,
                        },
                    ),
                    enabled=lambda: bool(self.run_observers),
                ),
            ):
                async with aclosing(source) as events:
                    async for event in events:
                        if isinstance(event, DoneEvent) and not event.extra.get("session_id"):
                            run.final_text = event.final_text
                        if isinstance(event, ErrorEvent):
                            if type(event) is TaskDeliveryWarning:
                                # Only this host-owned type carries trusted task
                                # scope. Provider codes and arbitrary extra fields
                                # cannot impersonate it or leak into web records.
                                reason, message = "task_delivery_skipped", TaskDeliveryWarning.message
                                record = {
                                    "event": "error",
                                    "message": message,
                                    "code": "TASK_DELIVERY_SKIPPED",
                                    "recoverable": True,
                                    "session_id": event.session_id,
                                    "title": event.title,
                                    "parent_session_id": event.parent_session_id,
                                    "root_session_id": event.root_session_id,
                                    "depth": event.depth,
                                    "activation": event.activation,
                                    "followup": event.followup,
                                }
                            else:
                                reason, message = provider_error(event)
                                record = {
                                    "event": "error",
                                    "message": message,
                                    "recoverable": event.recoverable,
                                }
                            log_error = logger.warning if event.recoverable else logger.error
                            log_error(
                                "Run error: session=%s run=%s reason=%s recoverable=%s diagnostic=%s",
                                run.session_id,
                                run.id,
                                reason,
                                event.recoverable,
                                provider_error_diagnostic(event),
                            )
                            if not event.recoverable:
                                run.outcome = "failed"
                        else:
                            if isinstance(event, ToolCallEvent):
                                known = (
                                    event.name
                                    if event.name in self.running_harness.agent.tool_registry.names()
                                    else "(unregistered)"
                                )
                                logger.info("Tool requested: session=%s run=%s tool=%s", run.session_id, run.id, known)
                            elif isinstance(event, ToolResultEvent):
                                known = (
                                    event.name
                                    if event.name in self.running_harness.agent.tool_registry.names()
                                    else "(unregistered)"
                                )
                                logger.info(
                                    "Tool completed: session=%s run=%s tool=%s failed=%s",
                                    run.session_id,
                                    run.id,
                                    known,
                                    bool(event.error),
                                )
                            record = transcript.record(event)
                        await notices.observe(record, live=True)
                        await self.send(run, record)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            logger.info("Run producer cancelled: session=%s run=%s", run.session_id, run.id)
            self.wakeups.cancel(run.chain)
            raise
        except Exception as error:
            task = asyncio.current_task()
            exception, sqlite_code, nested = exception_diagnostic(error)
            if task is not None and task.cancelling():
                # An owned operation can finish with its own error while Stop
                # is joining it. Keep the stop outcome, as with child workers.
                run.outcome = "cancelled"
                logger.warning(
                    "Run producer failed during cancellation: session=%s run=%s reason=cleanup_failed exception=%s sqlite_code=%s nested=%s",
                    run.session_id,
                    run.id,
                    exception,
                    sqlite_code,
                    nested,
                )
                self.wakeups.cancel(run.chain)
                raise asyncio.CancelledError from None
            run.outcome = "failed"
            # Readiness is advisory: an injected/scripted provider can run without
            # the configured connection's key. When the real provider fails, give
            # the same local-only setup guidance without exposing its exception.
            setup = provider_setup(self.running_harness)
            reason = "missing_credentials" if not setup["configured"] else "run_failed"
            logger.warning(
                "Run producer failed: session=%s run=%s reason=%s exception=%s sqlite_code=%s nested=%s",
                run.session_id,
                run.id,
                reason,
                exception,
                sqlite_code,
                nested,
            )
            await self.send(
                run,
                {
                    "event": "error",
                    "message": setup["message"] or "Run failed. Completed actions were not rolled back.",
                },
            )
        finally:
            # The core source has closed; remaining awaits only publish and
            # settle this run. A child input may wait for this owner to release,
            # but must not revive its inactive task group.
            run.draining = True
            if run.pending is not None and not run.pending.answer.done():
                run.pending.answer.set_result(False)
            run.pending = None
            try:
                if run.outcome == "cancelled":
                    await notices.finish("cancelled")
                elif run.outcome == "failed":
                    await notices.finish("failed")
                else:
                    await notices.finish("completed")
            finally:
                await _join(asyncio.create_task(self.responses.finish(run)))
                await _join(asyncio.create_task(self.tasks.finish(run)))
                await _join(asyncio.create_task(self.queued_inputs.finish(run)))
                await _join(asyncio.create_task(self.channels.activity(run.session_id, False)))

    async def execute_work(self, work: Work) -> str:
        await self.channels.store.validate_work(work)
        # Shutdown can pass its active-run check while owner validation awaits
        # SQLite. Recheck before publishing/owning a producer, with no intervening
        # await. The inbox worker returns this unstarted claim to queued.
        if self.channels.closed:
            return "queued"
        reserved = self.run_for(work.session_id)
        run = reserved if reserved is not None and reserved.work is work else self.reserve_work(work)
        logger.info(
            "Queued run started: session=%s run=%s message=%s channel=%s",
            work.session_id,
            run.id,
            work.message_id,
            work.channel or "web",
        )
        self.status()

        async def execute() -> None:
            try:
                with self.executions.scope(run):
                    await execute_owned()
            except asyncio.CancelledError:
                run.outcome = "cancelled"
                raise
            except SkillInvocationError as error:
                run.outcome = "failed"
                self.publish(run, {"event": "error", "message": str(error), "message_id": work.message_id})
            except Exception:
                run.outcome = "failed"
                self.publish(
                    run, {"event": "error", "message": "Queued run failed. Completed actions were not rolled back."}
                )

        async def execute_owned() -> None:
            run.executing = True
            async with self.executions.harness(run) as harness:
                self.running_harness = harness
                await harness.resume(run.session_id)
                async with self.queued_inputs.context(run, work):
                    prompt: str | list[ContentPart] = (
                        await self.channels.inbound_content(work.channel, work.prompt)
                        if work.channel
                        else await self.queued_inputs.content(work, harness)
                    )
                    await self.produce(run, prompt)

        run.task = asyncio.create_task(execute(), name=f"ngn-web-{run.id}")
        try:
            await _join(run.task)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
        finally:
            self.finish(run)
            await self.executions.release_idle(run.session_id)
        return "interrupted" if run.outcome == "cancelled" else run.outcome

    async def compact_work(self, work: Work) -> str:
        """Compact the chat's bound session on demand from an explicit command."""
        await self.channels.store.validate_work(work)
        if self.channels.closed:
            return "queued"
        reserved = self.run_for(work.session_id)
        run = reserved if reserved is not None and reserved.work is work else self.reserve_work(work)
        self.status()

        async def execute() -> None:
            note = "Compaction failed. Context was not changed."
            try:
                run.executing = True
                with self.executions.scope(run):
                    async with self.executions.harness(run) as harness:
                        self.running_harness = harness
                        await harness.resume(run.session_id)
                        done = await harness.compact()
                run.outcome = "completed"
                await self.send(run, _event_record(done))
                note = f"Context compacted: {done.original_message_count} messages summarized into {done.new_message_count}."
            except asyncio.CancelledError:
                run.outcome = "cancelled"
                raise
            except Exception:
                run.outcome = "failed"
                await self.send(
                    run, {"event": "error", "message": "Compaction failed. Session history was not changed."}
                )
            if work.channel:
                await self.channels.reply(work, note)

        run.task = asyncio.create_task(execute(), name=f"ngn-web-{run.id}")
        try:
            await _join(run.task)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
        finally:
            self.finish(run)
            await self.executions.release_idle(run.session_id)
        return "interrupted" if run.outcome == "cancelled" else run.outcome

    def finish(self, run: Run) -> None:
        if run.finished:
            return
        run.finished = True
        logger.info(
            "Run finished: session=%s run=%s status=%s background=%s",
            run.session_id,
            run.id,
            run.outcome,
            run.background,
        )
        self.channels.management.run_finished(run)
        self.publish(run, {"event": "run_finished", "status": run.outcome})
        self.executions.release(run)
        self.status()
        self.changed()

    async def wake(self, wakeup: Wakeup) -> None:
        run = self.run_for(wakeup.root_session_id)
        if run is None or run.wakeup_id != wakeup.id:
            task = asyncio.current_task()
            if task is None or not self.reserve_wakeup(wakeup, task):
                raise RuntimeError("Wakeup root is already owned")
            run = self.run_for(wakeup.root_session_id)
            assert run is not None
        self.publish(run, {"event": "run_started"})
        self.status()
        run.task = asyncio.create_task(self._wake(run, wakeup), name=f"ngn-web-{run.id}")
        try:
            await _join(run.task)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            self.wakeups.cancel(run.chain)
        finally:
            if run.outcome != "completed":
                self.wakeups.lifecycle(wakeup, "cancelled" if run.outcome == "cancelled" else "failed", run_id=run.id)
            self.finish(run)
            await self.executions.release_idle(run.session_id)

    async def _wake(self, run: Run, wakeup: Wakeup) -> None:
        try:
            with self.executions.scope(run):
                async with self.executions.harness(run) as harness:
                    self.running_harness = harness
                    await harness.resume(wakeup.root_session_id)
                    self.wakeups.lifecycle(wakeup, "fired", run_id=run.id)
                    await self.produce(run, wakeup.reason, agent_session_id=wakeup.session_id)
        except Exception:
            run.outcome = "failed"
            self.publish(run, {"event": "error", "message": "Wakeup failed. Completed actions were not rolled back."})

    async def stop(self, run: Run, *, cancel: bool = True) -> None:
        with anyio.CancelScope(shield=True):
            if cancel:
                self.wakeups.cancel(run.chain)
            if not run.task.done():
                run.outcome = "cancelled"
                # The event consumer may be joining an accepted journal write.
                # Stop model/tool work now; its normal cleanup remains owned by
                # that consumer and still joins the write before returning.
                harness = run.harness
                if (
                    self.run_for(run.session_id) is run
                    and harness is not None
                    and harness.session_id == run.session_id
                    and (worker := harness._worker) is not None
                    and not worker.done()
                    and not worker.cancelling()
                ):
                    worker.cancel()
                if not run.task.cancelling():
                    run.task.cancel()
            with suppress(asyncio.CancelledError):
                await _join(run.task)
            await self.channels.join_claim_cleanup(run.session_id)
            if not run.finished:
                self.finish(run)
            self.changed()
            await self.executions.release_idle(run.session_id)
