"""Live, permanently owned execution events; rendering belongs to the connector.

Create once inside WebState.produce (after run.task is assigned). Await start and
observe from that producer, waiting_for_approval from the actual approval handler
after Pending is installed, and finish in producer cleanup before clearing active
or restoring Harness selection. Never invoke these hooks from WS/history replay.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from nagents.channels import ChannelExecutionEvent
from nagents.channels import dispatch_channel_execution_event
from nagents.channels import sanitize_channel_tool_arguments
from nagents.channels.store import finish_on_cancel
from nagents.events import Event
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent

from .channel_privacy import CredentialProtectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.channels import ChannelExecutionPhase
    from nagents.channels import ChannelValue
    from nagents.harness.types import ApprovalRequest

    from .service import Run
    from .service import WebState

MAX_TOOL_EVENTS = 128
MAX_APPROVAL_EVENTS = 32
NOTICE_TIMEOUT = 2.0
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_TRANSPORT = frozenset({"channel_send", "channel_list", "channel_action"})
TerminalPhase = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class _Tool:
    activation: str
    call_id: str
    name: str
    arguments: object
    failed: bool = False


_EMPTY_TOOL = _Tool("", "", "", {})


def _record(event: Event | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(event, ToolCallEvent):
        return {
            "event": "tool_call",
            "id": event.id,
            "name": event.name,
            "arguments": event.arguments,
            "extra": event.extra,
        }
    if isinstance(event, ToolResultEvent):
        return {"event": "tool_result", "id": event.id, "name": event.name, "error": event.error, "extra": event.extra}
    return event if isinstance(event, Mapping) else {}


def _tool(record: Mapping[str, object], *, allow_transport: bool = False) -> _Tool | None:
    extra = record.get("extra", {})
    if not isinstance(extra, dict):
        return None
    for key in ("task_id", "activation"):
        if key in record and key in extra and (type(record[key]) is not type(extra[key]) or record[key] != extra[key]):
            return None
    task = record.get("task_id", extra.get("task_id", ""))
    activation = record.get("activation", extra.get("activation", 0))
    id, name = record.get("id"), record.get("name")
    if (
        type(task) is not str
        or (task and _ID.fullmatch(task) is None)
        or type(activation) is not int
        or not 0 <= activation <= 2**31 - 1
        or type(id) is not str
        or _ID.fullmatch(id) is None
        or type(name) is not str
        or _NAME.fullmatch(name) is None
        or (not allow_transport and name.casefold() in _TRANSPORT)
    ):
        return None
    error = record.get("error")
    if error is not None and type(error) is not str:
        return None
    # Length-prefix child identity: separators inside IDs cannot collide.
    scope = f"{len(task)}:{task}:{activation}"
    return _Tool(scope, id, name, record.get("arguments", {}), bool(error))


class ChannelNotices:
    """One live producer, one permanent owning chat; no generic sends or UI replay.

    Tool/approval counts and dedup memory are bounded separately. Start/terminal
    events have reserved slots; no host time-based throttle drops lifecycle state.
    All dispatches are awaited through the shared SDK's bounded, joined helper.
    """

    def __init__(self, state: WebState, run: Run) -> None:
        self.state = state
        self.run = run
        self.run_id = run.id
        self.session_id = run.session_id
        self._producer = run.task
        self._started = False
        self._closing = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._seen: set[tuple[str, str, str]] = set()
        self._approvals: set[tuple[str, str]] = set()

    def _active(self, *, terminal: bool = False) -> bool:
        run, state = self.run, self.state
        return (
            not self._closed
            and (terminal or not self._closing)
            and state.active is run
            and not run.finished
            and run.task is self._producer
            and run.id == self.run_id
            and run.session_id == self.session_id
            and not self._producer.done()
            and (terminal or not self._producer.cancelling())
            and (terminal or not run.chain.cancelled)
            and state.harness.session_id == self.session_id
            and not state.channels.closed
            and state.channels.catalog.allow_plugins
            and not state.harness.config.demo
        )

    async def start(self, *, live: bool = False) -> None:
        if live is not True or asyncio.current_task() is not self._producer:
            return
        async with self._lock:
            if self._started or not self._active():
                return
            self._started = True
            await self._emit("run_started", self._active)

    async def observe(self, event: Event | Mapping[str, object], *, live: bool = False) -> None:
        if live is not True or asyncio.current_task() is not self._producer:
            return
        async with self._lock:
            if not self._started or not self._active():
                return
            try:
                record = _record(event)
                if (
                    record.get("run_id", self.run_id) != self.run_id
                    or record.get("session_id", self.session_id) != self.session_id
                ):
                    return
                kind = record.get("event")
                if kind not in ("tool_call", "tool_result"):
                    return
                tool = _tool(record)
            except Exception:
                # Unknown plugin records have no public repr or exception text.
                return
            if tool is None:
                return
            phase: ChannelExecutionPhase = "tool_requested" if kind == "tool_call" else "tool_completed"
            key = (tool.activation, tool.call_id, phase)
            if key in self._seen or len(self._seen) >= MAX_TOOL_EVENTS:
                return
            self._seen.add(key)  # Failed/uncertain attempts cannot replay.
            await self._emit(phase, self._active, tool)

    async def waiting_for_approval(self, request: ApprovalRequest, *, live: bool = False) -> None:
        """Call after installing real Pending, before awaiting its answer; not for auto-approval."""
        if live is not True:
            return
        caller = asyncio.current_task()
        pending = self.run.pending
        harness = self.state.harness

        def waiting() -> bool:
            worker = harness._worker
            if request.task_id:
                info = harness.tasks._infos.get(request.task_id)
                child = harness.tasks._children.get(request.task_id)
                if (
                    info is None
                    or child is None
                    or info.session_id != self.session_id
                    or info.activation != request.activation
                ):
                    return False
                worker = child._worker
            return (
                self._active()
                and caller is not None
                and caller is worker
                and not caller.cancelling()
                and pending is not None
                and self.run.pending is pending
                and not pending.answer.done()
                and pending.call_id == request.id
                and pending.record.get("run_id") == self.run_id
                and pending.record.get("approval_id") == pending.id
                and pending.record.get("id") == request.id
                and pending.record.get("tool") == request.tool
                and pending.record.get("task_id", "") == request.task_id
                and pending.record.get("activation", 0) == request.activation
            )

        async with self._lock:
            if not self._started or not waiting():
                return
            tool = _tool(
                {
                    "id": request.id,
                    "name": request.tool,
                    "arguments": request.arguments,
                    "task_id": request.task_id,
                    "activation": request.activation,
                },
                allow_transport=True,
            )
            if tool is None:
                return
            key = (tool.activation, tool.call_id)
            if key in self._approvals or len(self._approvals) >= MAX_APPROVAL_EVENTS:
                return
            self._approvals.add(key)
            await self._emit("waiting_for_approval", waiting, tool)

    async def _emit(
        self, phase: ChannelExecutionPhase, eligible_run: Callable[[], bool], tool: _Tool = _EMPTY_TOOL
    ) -> None:
        host = self.state.channels
        try:
            async with asyncio.timeout(NOTICE_TIMEOUT):
                owner = await host.store.owner(self.session_id)
                if owner is None or owner.conflicted or not owner.channel or not owner.conversation_id:
                    return
                connection = host.catalog.connections.get(owner.channel)
                channel = host.channels.get(owner.channel)
                source = host.sources.get(owner.channel)

                def eligible() -> bool:
                    return (
                        eligible_run()
                        and connection is not None
                        and connection.enabled
                        and connection.auto_reply
                        and host.catalog.connections.get(owner.channel) is connection
                        and channel is not None
                        and host.channels.get(owner.channel) is channel
                        and source is not None
                        and host.sources.get(owner.channel) is source
                        and not source.done()
                        and not source.cancelling()
                        and host.catalog.status.get(owner.channel) == ("running", "")
                    )

                if not eligible():
                    return
                # Permanent ownership survives /new and root deletion. Require
                # live root membership, but never consult current chat bindings.
                await host.store._transaction(
                    lambda db: host.store.chat_root(db, self.session_id, owner.channel, owner.conversation_id)
                )
                if not eligible() or channel is None:
                    return
                guard = host.catalog.protection
                guard.check(tool.name)
                arguments: dict[str, ChannelValue] = {}
                try:
                    # Full ORIGINAL values first: SDK construction may redact or
                    # shorten them, which must not hide a known credential match.
                    guard.check(tool.arguments)
                except CredentialProtectionError:
                    pass
                else:
                    arguments = sanitize_channel_tool_arguments(tool.arguments)
                event = ChannelExecutionEvent(
                    owner.conversation_id,
                    self.session_id,
                    phase,
                    thread_id=(
                        self.run.source.get("thread_id", "")
                        if (self.run.source.get("channel"), self.run.source.get("conversation_id"))
                        == (owner.channel, owner.conversation_id)
                        else ""
                    ),
                    run_id=self.run_id,
                    activation_id=tool.activation,
                    call_id=tool.call_id,
                    tool_name=tool.name,
                    tool_arguments=arguments,
                    tool_failed=tool.failed,
                    message_id=self.run.message_id,
                )
                await dispatch_channel_execution_event(channel, event, timeout=NOTICE_TIMEOUT)
        except asyncio.CancelledError:
            raise  # Shared dispatcher joins the optional connector before this escapes.
        except Exception:
            pass  # Never expose connector/DB/credential exception text.

    async def finish(self, phase: TerminalPhase = "completed") -> None:
        """Emit one terminal phase and join cleanup, even under repeated cancellation.

        Invoke from producer finally while it still owns state.active. External
        cleanup may join/close this observer but cannot manufacture terminal events.
        """
        if phase not in ("completed", "failed", "cancelled"):
            raise ValueError("Invalid terminal execution phase")
        producer = asyncio.current_task() is self._producer
        self._closing = True

        async def close() -> None:
            async with self._lock:
                if self._closed:
                    return
                try:
                    if producer and self._started:
                        await self._emit(phase, lambda: self._active(terminal=True))
                finally:
                    self._closed = True
                    self._seen.clear()
                    self._approvals.clear()

        await finish_on_cancel(close())
