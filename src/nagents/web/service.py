"""One Harness execution owner shared by HTTP streaming, queued sources and timers."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from contextlib import aclosing
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
from starlette.responses import StreamingResponse

from nagents.cli import _event_record
from nagents.cli import _json_default
from nagents.compaction import estimate_tokens
from nagents.context_stats import ContextComponent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.execution import bind_channel_send
from nagents.harness.execution import host_run
from nagents.harness.runtime import _HarnessSession
from nagents.harness.types import TaskDeliveryWarning
from nagents.observation import scope as observation_scope

from ._async import finish_on_cancel
from ._async import join_owned as _join
from .channel_host import ChannelHost
from .channel_notices import ChannelNotices
from .channel_replies import automatic_reply
from .delivery_transcript import DeliveryTranscript
from .design_channels import DesignedChannels
from .history import WebHistory
from .live_inspection import model_requests
from .provider_setup import provider_error
from .provider_setup import provider_setup
from .queued_inputs import QueuedInputs
from .replay import RunReplay
from .subscriptions import EventBus
from .tool_approvals import ToolApprovals
from .tool_approvals import ToolBinding
from .trash import SessionTrash
from .uploads import Uploads
from .wakeups import Chain
from .wakeups import Wakeups

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterator

    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from nagents.harness import Harness
    from nagents.harness.followups import RunTurn
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import SessionInfo
    from nagents.types import ContentPart

    from .routing import Work
    from .settings import WebSettings
    from .wakeups import Wakeup

APPROVAL_TIMEOUT = 300
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
    queue: asyncio.Queue[dict[str, object]] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    task: asyncio.Task[None] = field(init=False)
    pending: Pending | None = None
    notices: ChannelNotices | None = field(default=None, repr=False)
    outcome: str = "completed"
    final_text: str = ""
    finished: bool = False
    background: bool = False
    server_owned: bool = False
    voice: bool = False
    work: Work | None = field(default=None, repr=False)
    message_id: str = ""
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
            f"{record.get('task_id', extra.get('task_id', ''))}:{record.get('activation', extra.get('activation', 0))}"
            f":{record.get('followup', extra.get('followup', 0))}"
        )
        generation = record.get("generation_id", extra.get("generation_id", ""))
        index = record.get("index", extra.get("index", ""))
        progress_key = f"{scope}:progress:{generation}:{index}"
        correlated = isinstance(generation, str) and bool(generation) and type(index) is int and 0 <= index < 1024
        call_id = record.get("call_id", record.get("id", ""))
        tool_slot = json.dumps([call_id, generation if correlated else "", index if correlated else -1])
        if not record.get("task_id", extra.get("task_id", "")):
            if event == "text_chunk" and not any(key.startswith(f"{scope}:call:") for key in self.drafts):
                self._context_reply_live = True
            elif event in {"text_done", "tool_call_progress", "tool_call", "tool_result", "done", "compaction_started"}:
                self._context_reply_live = False
        if event in {"text_chunk", "reasoning_chunk"}:
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

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.running_harness = harness
        self.approval_timeout: Callable[[], float] = lambda: APPROVAL_TIMEOUT
        self.selected_session_id = harness.session_id
        self.session_revision = 0
        self.active: Run | None = None
        self.mutating = False
        self.bus = EventBus()
        self.run_observers: set[Callable[[Run | None, dict[str, object]], None]] = set()
        self.history = WebHistory(harness.agent.session.db_path, self.user_message)
        self.queued_inputs = QueuedInputs(self)
        self.tool_approvals = ToolApprovals(
            harness.agent.session.db_path.with_name("tool-approvals.db"), harness.workspace
        )
        # Preserve explicit custom persistence adapters. Their unlinked rows still
        # get stable SQLite history IDs, without fabricated ingress annotations.
        if type(harness.agent.session) is _HarnessSession:
            harness.agent.session = self.history
        self.wakeups = Wakeups(
            lambda: self.active is None and not self.mutating, self.wake, observer=self.activity_event
        )
        self.channels = ChannelHost(self)
        self.designed_channels = DesignedChannels(self)
        self.uploads = Uploads(self)
        self.trash = SessionTrash(self)
        harness.approval_handler = self.approve
        harness.wakeup_handler = self.schedule

    def changed(self) -> None:
        self.wakeups.changed.set()
        self.channels.changed.set()

    @contextmanager
    def idle(self, *, allow_running: bool = False) -> Iterator[None]:
        if (self.active is not None and not allow_running) or self.mutating:
            raise HTTPException(409, "Harness busy. Cancel or finish the active operation first.")
        self.mutating = True
        try:
            yield
        finally:
            self.mutating = False
            self.changed()

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
        active = self.active
        return {
            "session_id": session_id,
            "activity_cursor": self.wakeups.cursor,
            "retained_tasks": [
                asdict(info) for info in self.harness.tasks._infos.values() if info.session_id == session_id
            ],
            "sessions": [asdict(session) for session in sessions],
            "history": history,
            "active_run": active.snapshot() if active is not None and active.session_id == session_id else None,
            "active_session_id": active.session_id if active else "",
            "active_run_id": active.id if active else "",
        }

    async def context_stats(self, session_id: str) -> dict[str, object]:
        """Read-only estimated context breakdown for a root session.

        Reads persisted history and the active agent's configuration, plus its
        bounded uncommitted reply estimate. Safe while a run is active.
        """
        if session_id not in {session.id for session in await self.list_sessions()}:
            raise HTTPException(404, "Session not found in this workspace.")
        active = self.active
        if active is not None and active.session_id == session_id and self.running_harness.session_id == session_id:
            stats = await self.running_harness.agent.context_stats(session_id)
        else:
            stats = await self.designed_channels.context_stats(session_id)
        if active is not None and self.active is active and active.session_id == session_id and active.context_reply:
            tokens = estimate_tokens(active.context_reply) + 4
            stats = replace(
                stats,
                components=(
                    *stats.components,
                    ContextComponent("streaming_reply", "Streaming reply (not yet saved)", tokens),
                ),
                total_tokens=stats.total_tokens + tokens,
                remaining_tokens=stats.remaining_tokens - tokens if stats.remaining_tokens is not None else None,
            )
        return stats.as_dict()

    def user_message(self, run_id: str, record: dict[str, object]) -> None:
        run = self.active
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
                            "session_id": session_id,
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
        run = self.active
        if (
            run is not None
            and (run.server_owned or run.background)
            and not self.bus.listening(run.session_id)
            and run.pending is not None
            and not run.pending.answer.done()
            and not run.pending.in_chat
        ):
            run.pending.answer.set_result(False)

    async def send(self, run: Run, record: dict[str, object]) -> None:
        self.publish(run, record)
        if not run.background and not run.server_owned:
            await run.queue.put(record)

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
        binding = self.tool_approvals.requested_binding(self.running_harness, request)
        try:
            allowed = binding is not None and self.tool_approvals.allowed(binding)
        except sqlite3.Error:
            allowed = False  # Unreadable saved policy requires an explicit decision.
        if allowed:
            await self.send(
                run,
                {
                    "event": "notice",
                    "automatic": True,
                    "policy": "workspace_tool_allow",
                    **identity,
                    "text": f"{request.tool} is always allowed in this workspace.",
                    "task_id": request.task_id,
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
                    "task_id": request.task_id,
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
                        "task_id": request.task_id,
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
            if not run.task.cancelling():
                await self.send(
                    run,
                    {
                        "event": "approval_closed",
                        **identity,
                        "approval_id": pending.id,
                        "task_id": request.task_id,
                        "activation": request.activation,
                        "call_id": request.id,
                        "decision": (pending.decision or "allow") if approved else "deny",
                        "expired": expired,
                    },
                )

    async def schedule(self, task_id: str, delay: float, reason: str) -> dict[str, str]:
        run = self.active
        if run is None or run.task.cancelling() or run.task.done() or run.session_id != self.harness.session_id:
            raise RuntimeError("Wakeups require an active run in their originating session")
        return self.wakeups.schedule(run.session_id, run.id, run.chain, task_id, delay, reason)

    def publish(self, run: Run, record: dict[str, object]) -> None:
        record = {**record, "schema_version": 1, "run_id": run.id, "session_id": run.session_id}
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
        run = self.active
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
            }
        )

    async def produce_session(self, run: Run, prompt: str | list[ContentPart]) -> None:
        try:
            async with self.designed_channels.execution(run) as harness:
                self.running_harness = harness
                await harness.resume(run.session_id)
                await self.produce(run, prompt)
        finally:
            self.running_harness = self.harness

    async def produce(self, run: Run, prompt: str | list[ContentPart], *, task_id: str = "") -> None:
        notices = run.notices = ChannelNotices(self, run)
        transcript = DeliveryTranscript(
            capture=type(self.running_harness.agent.session) in {WebHistory, _HarnessSession}
        )
        try:
            await self.channels.activity(run.session_id, True, run.source)
            await notices.start(live=True)
            if run.background:
                if not isinstance(prompt, str):
                    raise ValueError("Background wake prompts must be text")
                source = self.running_harness.wake(prompt, task_id=task_id)
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
                    run, {"event": "root_turn", "session_id": run.session_id, "run_id": run.id, "_turn": turn}
                )

            with (
                binding,
                host_run(self.running_harness, run.id),
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
                            "session_id": run.session_id,
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
                        if isinstance(event, DoneEvent) and not event.extra.get("task_id"):
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
                                    "task_id": event.task_id,
                                    "task_name": event.task_name,
                                    "parent_task_id": event.parent_task_id,
                                    "parent_session_id": event.parent_session_id,
                                    "child_session_id": event.child_session_id,
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
                            logger.warning(
                                "Run error: session=%s run=%s reason=%s recoverable=%s",
                                run.session_id,
                                run.id,
                                reason,
                                event.recoverable,
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
        except Exception:
            run.outcome = "failed"
            # Readiness is advisory: an injected/scripted provider can run without
            # the configured connection's key. When the real provider fails, give
            # the same local-only setup guidance without exposing its exception.
            setup = provider_setup(self.running_harness)
            reason = "missing_credentials" if not setup["configured"] else "run_failed"
            logger.warning("Run producer failed: session=%s run=%s reason=%s", run.session_id, run.id, reason)
            await self.send(
                run,
                {
                    "event": "error",
                    "message": setup["message"] or "Run failed. Completed actions were not rolled back.",
                },
            )
        finally:
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
                await _join(asyncio.create_task(self.queued_inputs.finish(run)))
                await _join(asyncio.create_task(self.channels.activity(run.session_id, False)))

    async def execute_work(self, work: Work) -> str:
        await self.channels.store.validate_work(work)
        # Shutdown can pass its active-run check while owner validation awaits
        # SQLite. Recheck before publishing/owning a producer, with no intervening
        # await. The inbox worker returns this unstarted claim to queued.
        if self.channels.closed:
            return "queued"
        run = Run(
            work.session_id, server_owned=True, message_id=work.message_id, voice=bool(work.voice_session_id), work=work
        )
        if work.channel:
            run.source = {"channel": work.channel, "conversation_id": work.conversation_id, "thread_id": work.thread_id}
        self.active = run
        self.queued_inputs.admitted(run, work, initial=True)
        logger.info(
            "Queued run started: session=%s run=%s message=%s channel=%s",
            work.session_id,
            run.id,
            work.message_id,
            work.channel or "web",
        )
        self.publish(run, {"event": "run_started", "message_id": work.message_id, "channel": work.channel})
        self.status()

        async def execute() -> None:
            try:
                async with self.designed_channels.execution(run) as harness:
                    self.running_harness = harness
                    await harness.resume(run.session_id)
                    async with self.queued_inputs.context(run, work):
                        prompt: str | list[ContentPart] = (
                            await self.channels.inbound_content(work.channel, work.prompt)
                            if work.channel
                            else await self.uploads.content(work, harness)
                        )
                        await self.produce(run, prompt)
            except asyncio.CancelledError:
                run.outcome = "cancelled"
                raise
            except Exception:
                run.outcome = "failed"
                self.publish(
                    run, {"event": "error", "message": "Queued run failed. Completed actions were not rolled back."}
                )
            finally:
                # A UI selection made during this run takes effect only after
                # producer cleanup, never underneath the shared Harness.
                self.harness.session_id = self.selected_session_id
                self.harness.tools.read_hashes.clear()
                self.running_harness = self.harness

        run.task = asyncio.create_task(execute(), name=f"ngn-web-{run.id}")
        try:
            await _join(run.task)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
        finally:
            self.finish(run)
        return "interrupted" if run.outcome == "cancelled" else run.outcome

    async def compact_work(self, work: Work) -> str:
        """Compact the chat's bound session on demand from an explicit command."""
        await self.channels.store.validate_work(work)
        if self.channels.closed:
            return "queued"
        run = Run(work.session_id, server_owned=True, message_id=work.message_id)
        if work.channel:
            run.source = {"channel": work.channel, "conversation_id": work.conversation_id, "thread_id": work.thread_id}
        self.active = run
        self.publish(run, {"event": "run_started", "message_id": work.message_id, "channel": work.channel})
        self.status()

        async def execute() -> None:
            note = "Compaction failed. Context was not changed."
            try:
                async with self.designed_channels.execution(run) as harness:
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
            finally:
                # A UI selection made during this run takes effect only after
                # producer cleanup, never underneath the shared Harness.
                self.harness.session_id = self.selected_session_id
                self.harness.tools.read_hashes.clear()
                self.running_harness = self.harness
            if work.channel:
                await self.channels.reply(work, note)

        run.task = asyncio.create_task(execute(), name=f"ngn-web-{run.id}")
        try:
            await _join(run.task)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
        finally:
            self.finish(run)
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
        if self.active is run:
            self.active = None
        self.status()
        self.changed()

    async def wake(self, wakeup: Wakeup) -> None:
        run = Run(wakeup.session_id, background=True, chain=wakeup.chain)
        self.active = run
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

    async def _wake(self, run: Run, wakeup: Wakeup) -> None:
        previous = self.harness.session_id
        try:
            await self.harness.resume(wakeup.session_id)
            self.wakeups.lifecycle(wakeup, "fired", run_id=run.id)
            await self.produce(run, wakeup.reason, task_id=wakeup.task_id)
        except Exception:
            run.outcome = "failed"
            self.publish(run, {"event": "error", "message": "Wakeup failed. Completed actions were not rolled back."})
        finally:
            self.harness.session_id = previous
            self.harness.tools.read_hashes.clear()

    async def stop(self, run: Run, *, cancel: bool = True) -> None:
        with anyio.CancelScope(shield=True):
            if cancel:
                self.wakeups.cancel(run.chain)
            if not run.task.done() and not run.task.cancelling():
                run.outcome = "cancelled"
                run.task.cancel()
            with suppress(asyncio.CancelledError):
                await _join(run.task)
            if not run.server_owned and not run.background:
                self.finish(run)
            elif self.active is run:
                self.active = None
            self.changed()


class RunResponse(StreamingResponse):
    def __init__(self, state: WebState, run: Run) -> None:
        self.state = state
        self.run = run
        self.finished = False
        super().__init__(self.events(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})

    async def events(self) -> AsyncIterator[str]:
        run = self.run

        def line(record: dict[str, object]) -> str:
            return (
                json.dumps(
                    {**record, "schema_version": 1, "run_id": run.id, "session_id": run.session_id},
                    default=_json_default,
                    ensure_ascii=True,
                )
                + "\n"
            )

        yield line({"event": "run_started", "session_id": run.session_id})
        while not run.task.done() or not run.queue.empty():
            try:
                record = await asyncio.wait_for(run.queue.get(), timeout=1)
            except TimeoutError:
                yield line({"event": "heartbeat"})
            else:
                yield line(record)
        yield line({"event": "run_finished", "status": run.outcome, "session_id": run.session_id})
        self.finished = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.state.stop(self.run, cancel=not self.finished)
