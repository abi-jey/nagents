"""Host inputs admitted after a model response and its complete tool batch."""

from __future__ import annotations

from collections import deque
from contextlib import AbstractAsyncContextManager
from contextlib import contextmanager
from contextlib import nullcontext
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Literal
from uuid import uuid4

from nagents.extensions import AgentPlugin

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterator

    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.types import ContentPart

    from .subagents import SubagentManager


@dataclass(frozen=True)
class RunInput:
    prompt: str | list[ContentPart]
    identifier: str = ""
    kind: Literal["human", "followup", "notification", "wakeup"] = "human"
    task_ids: tuple[str, ...] = ()
    context: Callable[[], AbstractAsyncContextManager[None]] = field(default=nullcontext, repr=False)
    voice_session_id: str = ""
    message_id: str = ""


@dataclass(frozen=True)
class RunTurn:
    """Host-owned correlation, never copied from provider event extras."""

    id: str
    identifier: str
    kind: str
    task_ids: tuple[str, ...]
    voice_session_id: str = ""
    message_id: str = ""


class FollowupReady(Exception):
    def __init__(self, owner: RunInputs) -> None:
        self.owner = owner


class RunInputs(AgentPlugin):
    def __init__(self) -> None:
        self._pending: deque[RunInput] = deque()
        self._session = ""
        self._observer: Callable[[RunTurn], None] | None = None
        self.current = RunTurn("", "", "", ())
        self._reader: Callable[[], Awaitable[RunInput | None]] | None = None
        self._changed = False
        self._origin = ("", "")

    @property
    def pending(self) -> bool:
        return bool(self._pending) or self._changed

    def changed(self) -> None:
        self._changed = True

    def begin(self, session_id: str) -> None:
        self._session = session_id

    def end(self) -> None:
        self._session = ""
        self._pending.clear()
        self._changed = False

    def admit(self, session_id: str, message: RunInput) -> bool:
        if not self._session or session_id != self._session:
            return False
        if not isinstance(message.prompt, str) or not message.prompt.strip() or len(message.prompt) > 32000:
            raise ValueError("A follow-up requires bounded text")
        if len(self._pending) >= 32:
            raise ValueError("The root follow-up queue is full")
        self._pending.append(message)
        return True

    def start_turn(self, message: RunInput) -> RunTurn:
        self.current = RunTurn(
            uuid4().hex,
            message.identifier,
            message.kind,
            message.task_ids,
            message.voice_session_id,
            message.message_id,
        )
        return self.current

    def first(self, prompt: str | list[ContentPart]) -> RunInput:
        return RunInput(prompt, identifier=self._origin[1], voice_session_id=self._origin[0])

    @contextmanager
    def source(
        self, reader: Callable[[], Awaitable[RunInput | None]], origin: tuple[str, str] = ("", "")
    ) -> Iterator[None]:
        previous, old_origin = self._reader, self._origin
        self._reader, self._origin = reader, origin
        try:
            yield
        finally:
            self._reader, self._origin = previous, old_origin

    async def _pull(self) -> None:
        self._changed = False
        if self._reader is not None and not self._pending:
            message = await self._reader()
            if message is not None:
                self._pending.append(message)

    async def after_tools(self, context: RunContext) -> None:
        if context.session_id == self._session:
            await self._pull()
            if self._pending:
                raise FollowupReady(self)

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        if context.round_number > 0:
            await self.after_tools(context)
        return request

    def notify(self, turn: RunTurn) -> None:
        if self._observer is not None:
            self._observer(turn)

    @contextmanager
    def observe(self, observer: Callable[[RunTurn], None]) -> Iterator[None]:
        previous, self._observer = self._observer, observer
        try:
            yield
        finally:
            self._observer = previous

    async def next(self, tasks: SubagentManager, *, wait_for_tasks: bool = False) -> RunInput | None:
        while True:
            await self._pull()
            if self._pending:
                return self._pending.popleft()
            notification = await tasks.notification(wait_for_tasks=wait_for_tasks)
            if notification is not None:
                return RunInput(notification, kind="notification", task_ids=tasks.notification_sources)
            if self.pending or tasks._running():
                continue
            # No await between the empty decision and rejecting future admissions.
            self._session = ""
            return None
