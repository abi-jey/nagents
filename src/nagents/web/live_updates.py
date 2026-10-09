"""One attached voice consumer of ordered, host-owned chat events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from nagents.harness.followups import RunTurn
from nagents.live.runtime import result_chunks

from ._async import join_owned
from .routing import Work

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.live.delegation import LiveAppend
    from nagents.live.delegation import LiveAppendKind

    from .service import Run
    from .service import WebState

logger = logging.getLogger("uvicorn.error")


@dataclass
class _Turn:
    origin: str = ""
    kind: str = "human"
    texts: set[str] = field(default_factory=set)


class AssistantUpdates:
    def __init__(
        self,
        state: WebState,
        session_id: str,
        report: Callable[[str, str, str, str], None],
        answer: Callable[[str, str, str], None],
        finished: Callable[[Run], None],
    ) -> None:
        self.state, self.session_id = state, session_id
        self.report, self.answer = report, answer
        self.finished = finished
        self.attached = False
        self.closed = False
        self.failed = False
        self.detached = False
        self._sink: LiveAppend | None = None
        self._worker: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[LiveAppendKind, str, str]] = asyncio.Queue(maxsize=128)
        self._roots: dict[str, str] = {}
        self._wakeups: dict[str, tuple[str, str]] = {}
        self._turns: dict[str, _Turn] = {}
        self._tasks: dict[str, str] = {}
        self._requests: dict[str, set[str]] = {}
        self.needs_attention: Callable[[], bool] = lambda: False
        self.ready: asyncio.Event | None = None
        self.admitted: Callable[[str, str, str], None] = lambda identifier, run_id, prompt: None
        self.model: Callable[[str, str, dict[str, object]], None] = lambda identifier, run_id, request: None
        self.retain: Callable[[], bool] = lambda: False
        self.known: set[str] = set()
        self.voice_session_id = ""

    def bind(self, run: Run, identifier: str, *, initial: bool = False) -> None:
        self._requests.setdefault(run.id, set()).add(identifier)
        if initial:
            self._roots[run.id] = identifier

    def origin(self, run: Run) -> str:
        return self._turns.get(run.id, _Turn(self._roots.get(run.id, ""))).origin

    def turn_origin(self, marker: RunTurn, run_id: str) -> str:
        if marker.identifier:
            return (
                marker.identifier
                if marker.voice_session_id == self.voice_session_id and marker.identifier in self.known
                else ""
            )
        if marker.kind == "notification":
            origins = {self._tasks.get(task, "") for task in marker.task_ids}
            return next(iter(origins)) if len(origins) == 1 else ""
        if marker.kind in {"human", "wakeup"}:
            return self._roots.get(run_id, "")
        return marker.identifier

    def attach(self, sink: LiveAppend) -> None:
        if self.attached or self.closed:
            raise RuntimeError("A voice update consumer can only attach once")
        self._sink = sink
        self.attached = True
        self.state.run_observers.add(self.observe)
        self._worker = asyncio.create_task(self._send(), name="ngn-live-assistant-updates")

    def append(self, kind: LiveAppendKind, text: str, identifier: str = "") -> None:
        if not self.attached or self.closed or not text.strip():
            return
        if len(text) > 32000:
            text = text[:32000] + "\nThe full answer is available in the chat."
        try:
            self._queue.put_nowait((kind, text, identifier))
        except asyncio.QueueFull:
            # Never block workspace tools on a stalled voice transport, nor
            # accumulate unbounded speech. Detach; the chat retains all results.
            self.closed = True
            self.failed = True
            if not self.retain():
                self.state.run_observers.discard(self.observe)
            if self._worker is not None:
                self._worker.cancel()
            self.report(identifier, kind, "", "")
            logger.warning("Live assistant update buffer filled; updates remain available in chat.")

    async def _send(self) -> None:
        assert self._sink is not None
        identifier, part = "", ""
        kind: LiveAppendKind = "thinking"
        try:
            if self.ready is not None:
                await self.ready.wait()
            while not self.closed:
                kind, content, identifier = await self._queue.get()
                if kind == "instructions" and not self.needs_attention():
                    continue
                for part in result_chunks(content):
                    if self.closed:
                        return
                    async with asyncio.timeout(15):
                        wire = await self._sink(kind, part, identifier)
                    if wire and not self.closed:
                        self.report(identifier, kind, part, wire)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
            self.report(identifier, kind, part, "")
            logger.warning("Live assistant updates stopped; results remain available in chat.")
        finally:
            self.closed = True
            if not self.retain():
                self.state.run_observers.discard(self.observe)

    def observe(self, run: Run | None, record: dict[str, object]) -> None:
        if record.get("session_id") != self.session_id:
            return
        if self.closed and not self.retain():
            self.state.run_observers.discard(self.observe)
            return
        run_id = str(record.get("run_id", ""))
        event = record.get("event")
        if event == "input_admitted" and run is not None:
            work = record.get("_work")
            if (
                isinstance(work, Work)
                and work.voice_session_id == self.voice_session_id
                and work.voice_delegation_id in self.known
            ):
                self.bind(run, work.voice_delegation_id, initial=record.get("initial") is True)
                self.admitted(work.voice_delegation_id, run.id, work.prompt)
            return
        if event == "model_request":
            marker, request = record.get("_turn"), record.get("request")
            if isinstance(marker, RunTurn) and isinstance(request, dict):
                origin = self.turn_origin(marker, run_id)
                if origin:
                    self.model(origin, run_id, request)
            return
        if event == "run_finished" and run is not None:
            origins = self._requests.pop(run_id, set())
            if run.outcome in {"failed", "cancelled"}:
                message = (
                    "The assistant request was stopped. Check the chat for any actions already completed."
                    if run.outcome == "cancelled"
                    else "The assistant could not complete the request. Check the chat for details before trying again."
                )
                for origin in sorted(origins):
                    self.append("thinking", message, origin)
                self.append("commentary", message, self.origin(run) or (next(iter(sorted(origins))) if origins else ""))
            self.finished(run)
            if self.closed and not self.retain():
                self.state.run_observers.discard(self.observe)
            return
        if event == "root_turn":
            marker = record.get("_turn")
            if isinstance(marker, RunTurn):
                self._turns[run_id] = _Turn(self.turn_origin(marker, run_id), marker.kind)
            return
        turn = self._turns.get(run_id, _Turn(self._roots.get(run_id, "")))
        task = str(record.get("task_id", ""))
        if event == "task_started":
            parent = str(record.get("parent_task_id", ""))
            origin = self._tasks.get(parent, "") if parent else turn.origin
            self._tasks[task] = origin
            self.append("thinking", f"Background task {str(record.get('name', ''))[:128]} started.", origin)
        elif event == "task_completed":
            status = record.get("status", "completed")
            summary = "finished" if status == "completed" else "was cancelled" if status == "cancelled" else "failed"
            self.append(
                "thinking", f"Background task {str(record.get('name', ''))[:128]} {summary}.", self._tasks.get(task, "")
            )
        elif event == "text_done":
            extra = record.get("extra", {})
            if task or (isinstance(extra, dict) and extra.get("task_id")):
                return  # Child reasoning/results reach voice only through its parent's answer.
            text = record.get("text")
            if isinstance(text, str) and text.strip() and text not in turn.texts:
                turn.texts.add(text)
                self._turns[run_id] = turn
                self.append("commentary", text, turn.origin)
                if turn.kind in {"notification", "wakeup"} and self.needs_attention():
                    self.append(
                        "instructions",
                        "A background result has arrived. Tell the caller the verified assistant update that was just supplied.",
                        turn.origin,
                    )
                self.answer(turn.origin, text, run_id)
        elif event == "done":
            text = record.get("final_text")
            extra = record.get("extra", {})
            if (
                not task
                and not (isinstance(extra, dict) and extra.get("task_id"))
                and isinstance(text, str)
                and text.strip()
                and not turn.texts
            ):
                turn.texts.add(text)
                self.append("commentary", text, turn.origin)
                self.answer(turn.origin, text, run_id)
        elif event == "wakeup":
            phase = record.get("status")
            wakeup_id = str(record.get("wakeup_id", ""))
            if phase == "scheduled":
                marker = record.get("_turn")
                origin = self.turn_origin(marker, run_id) if isinstance(marker, RunTurn) else turn.origin
                if task:
                    origin = self._tasks.get(task, "")
                self._wakeups[wakeup_id] = (origin, task)
            origin, child = self._wakeups.get(wakeup_id, (self._roots.get(run_id, ""), ""))
            if child and not origin:
                origin = self._tasks.get(child, "")
            if phase == "fired":
                self._roots[run_id] = origin
            if phase in {"fired", "failed", "cancelled"}:
                self._wakeups.pop(wakeup_id, None)
            if phase in {"scheduled", "fired", "failed", "cancelled"}:
                self.append("thinking", f"The assistant's scheduled follow-up is {phase}.", origin)

    async def close(self) -> None:
        self.closed = True
        self.detached = True
        if not self.retain():
            self.state.run_observers.discard(self.observe)
        if self._worker is not None:
            self._worker.cancel()
            try:
                await join_owned(self._worker)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
        while not self._queue.empty():
            self._queue.get_nowait()
