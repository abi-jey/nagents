"""One Harness execution owner shared by HTTP streaming, queued sources and timers."""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import aclosing
from contextlib import contextmanager
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
from nagents.events import ErrorEvent
from nagents.harness.execution import host_run
from nagents.harness.runtime import _HarnessSession

from ._async import join_owned as _join
from .channel_host import ChannelHost
from .channel_notices import ChannelNotices
from .channel_replies import automatic_reply
from .design_channels import DesignedChannels
from .dictation import WebDictation
from .history import WebHistory
from .replay import RunReplay
from .subscriptions import EventBus
from .trash import SessionTrash
from .wakeups import Chain
from .wakeups import Wakeups

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Iterator

    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from nagents.harness import Harness
    from nagents.harness.types import ApprovalRequest
    from nagents.harness.types import SessionInfo
    from nagents.types import ContentPart

    from .routing import Work
    from .settings import WebSettings
    from .wakeups import Wakeup

APPROVAL_TIMEOUT = 300


@dataclass
class Pending:
    id: str
    call_id: str
    answer: asyncio.Future[bool]
    record: dict[str, object] = field(default_factory=dict)
    in_chat: bool = False


@dataclass
class Run:
    session_id: str
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    queue: asyncio.Queue[dict[str, object]] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    task: asyncio.Task[None] = field(init=False)
    pending: Pending | None = None
    notices: ChannelNotices | None = field(default=None, repr=False)
    outcome: str = "completed"
    finished: bool = False
    background: bool = False
    server_owned: bool = False
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
        )
        if not record.get("task_id", extra.get("task_id", "")):
            if event == "text_chunk" and not any(key.startswith(f"{scope}:call:") for key in self.drafts):
                self._context_reply_live = True
            elif event in {"text_done", "tool_call", "tool_result", "done", "compaction_started"}:
                self._context_reply_live = False
        if event in {"text_chunk", "reasoning_chunk"}:
            key = f"{scope}:{event}"
            old = self.drafts.get(key, {})
            self.retain(key, {**record, "chunk": (str(old.get("chunk", "")) + str(record.get("chunk", "")))[-262144:]})
        elif event == "tool_call":
            self.retain(f"{scope}:call:{record.get('id', '')}", record)
        elif event == "tool_output":
            key = f"{scope}:output:{record.get('call_id', '')}"
            old = self.drafts.get(key, {})
            self.retain(key, {**record, "text": (str(old.get("text", "")) + str(record.get("text", "")))[-262144:]})
        elif event == "tool_result":
            self.forget(f"{scope}:call:{record.get('id', '')}")
            self.forget(f"{scope}:output:{record.get('id', '')}")
        elif event in {"text_done", "done", "task_completed"}:
            self.forget(f"{scope}:text_chunk")
            self.forget(f"{scope}:reasoning_chunk")

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
        self.dictation = WebDictation()
        self.bus = EventBus()
        self.history = WebHistory(harness.agent.session.db_path, self.user_message)
        # Preserve explicit custom persistence adapters. Their unlinked rows still
        # get stable SQLite history IDs, without fabricated ingress annotations.
        if type(harness.agent.session) is _HarnessSession:
            harness.agent.session = self.history
        self.wakeups = Wakeups(
            lambda: self.active is None and not self.mutating, self.wake, observer=self.activity_event
        )
        self.channels = ChannelHost(self)
        self.designed_channels = DesignedChannels(self)
        self.trash = SessionTrash(self)
        harness.approval_handler = self.approve
        harness.wakeup_handler = self.schedule

    def changed(self) -> None:
        self.wakeups.changed.set()
        self.channels.changed.set()

    @contextmanager
    def idle(self) -> Iterator[None]:
        if self.active is not None or self.mutating:
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
        if await automatic_reply(self, run, request):
            self.publish(
                run,
                {
                    "event": "notice",
                    "automatic": True,
                    "policy": "channel_auto_reply",
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
        pending.record = {"event": "approval", **asdict(request), "approval_id": pending.id, "run_id": run.id}
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
                        "approval_id": pending.id,
                        "task_id": request.task_id,
                        "activation": request.activation,
                        "call_id": request.id,
                        "decision": "allow" if approved else "deny",
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

    def activity_event(self, record: dict[str, object]) -> None:
        # This also captures lifecycle records emitted directly by the scheduler,
        # such as scheduled/fired, without duplicating background model events.
        run = self.active
        if run is not None and record.get("run_id") == run.id:
            run.remember(record)
        self.bus.event(record)

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
        try:
            await self.channels.activity(run.session_id, True, run.source)
            await notices.start(live=True)
            if run.background:
                if not isinstance(prompt, str):
                    raise ValueError("Background wake prompts must be text")
                source = self.running_harness.wake(prompt, task_id=task_id)
            else:
                source = self.running_harness.run(prompt)
            with host_run(self.running_harness, run.id):
                async with aclosing(source) as events:
                    async for event in events:
                        if isinstance(event, ErrorEvent):
                            run.outcome = "failed"
                            record = {
                                "event": "error",
                                "message": "The provider run failed. Check local provider configuration before trying again.",
                                "recoverable": event.recoverable,
                            }
                        else:
                            record = _event_record(event)
                        await notices.observe(record, live=True)
                        await self.send(run, record)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            self.wakeups.cancel(run.chain)
            raise
        except Exception:
            run.outcome = "failed"
            await self.send(run, {"event": "error", "message": "Run failed. Completed actions were not rolled back."})
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
                await _join(asyncio.create_task(self.channels.activity(run.session_id, False)))

    async def execute_work(self, work: Work) -> str:
        await self.channels.store.validate_work(work)
        # Shutdown can pass its active-run check while owner validation awaits
        # SQLite. Recheck before publishing/owning a producer, with no intervening
        # await. The inbox worker returns this unstarted claim to queued.
        if self.channels.closed:
            return "queued"
        run = Run(work.session_id, server_owned=True, message_id=work.message_id)
        if work.channel:
            run.source = {"channel": work.channel, "conversation_id": work.conversation_id, "thread_id": work.thread_id}
        self.active = run
        self.publish(run, {"event": "run_started", "message_id": work.message_id, "channel": work.channel})
        self.status()

        async def execute() -> None:
            try:
                async with self.designed_channels.execution(run) as harness:
                    self.running_harness = harness
                    await harness.resume(run.session_id)
                    with self.history.admitted(work, run.id):
                        prompt: str | list[ContentPart] = (
                            await self.channels.inbound_content(work.channel, work.prompt)
                            if work.channel
                            else work.prompt
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
