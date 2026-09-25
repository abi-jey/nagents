"""Private, task-bound authority for owned local delivery sinks.

Origins are correlation data, never bearer capabilities. A sink must obtain and
revalidate them while the explicitly bound async channel_send definition runs.
"""

import asyncio
import copy
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nagents.channels.delivery_types import DeliveryOrigin
from nagents.harness.types import HarnessEvent
from nagents.harness.types import TranscriptAbandoned
from nagents.harness.types import TranscriptAnchor

if TYPE_CHECKING:
    from nagents.events import ToolCallEvent
    from nagents.session import SessionManager
    from nagents.types import Message
    from nagents.types import ToolCall
    from nagents.types import ToolDefinition

    from .runtime import Harness
    from .tools import HarnessExecutor


_current: ContextVar["ExecutionBridge | None"] = ContextVar("delivery_execution", default=None)
_host: ContextVar[tuple[object, str]] = ContextVar("delivery_host_run", default=(None, ""))
_observer: ContextVar[tuple[object, Callable[[HarnessEvent], None] | None]] = ContextVar(
    "delivery_host_observer", default=(None, None)
)
_owned_adapter_types: set[type["SessionManager"]] = set()


def _register_owned_adapter_type(adapter_type: type["SessionManager"]) -> None:
    """Host registration of an audited adapter; subclasses are not authorized.

    Registration confers only write-capture eligibility, never a Harness/run
    binding. Web registers when its own module loads; Harness never imports it.
    """
    _owned_adapter_types.add(adapter_type)


def _is_owned_adapter(adapter: "SessionManager") -> bool:
    return type(adapter) in _owned_adapter_types


@contextmanager
def host_run(
    harness: "Harness", run_id: str, *, observe: Callable[[HarnessEvent], None] | None = None
) -> Iterator[None]:
    """Bind a host's run ID at its owned stream boundary (not its worker task)."""
    if not run_id:
        raise ValueError("Host run ID must not be empty")
    token = _host.set((harness, run_id))
    observer_token = _observer.set((harness, observe))
    try:
        yield
    finally:
        _host.reset(token)
        _observer.reset(observer_token)


def observe_host_event(harness: "Harness", event: HarnessEvent) -> None:
    """Let an owning UI retain the bounded, unconsumed stream tail on cancellation.

    This is a synchronous host-local observer, not a second consumer or an agent
    plugin. Child Harnesses cannot inherit the root's observer.
    """
    owner, observe = _observer.get()
    if owner is harness and observe is not None:
        observe(event)


def host_run_id(harness: "Harness") -> str:
    owner, run_id = _host.get()
    return run_id if owner is harness else uuid.uuid4().hex


@dataclass
class _Block:
    message: "Message"
    calls: list["ToolCall"]
    events: tuple["ToolCallEvent", ...]
    row_id: int = 0
    writing: bool = True


class ExecutionBridge:
    def __init__(self, harness: "Harness", adapter: "SessionManager", run_id: str) -> None:
        self.harness = harness
        self.adapter = adapter
        self.run_id = run_id
        self.session_id = harness.session_id
        self.worker = asyncio.current_task()
        self.turn_id = ""
        self.block: _Block | None = None
        self.call: ToolCall | None = None
        self.approved_call: ToolCall | None = None
        self.position = -1
        self.invocation: DeliveryOrigin | None = None
        self.definition: ToolDefinition | None = None
        self.approved: ToolDefinition | None = None
        self.executor: HarnessExecutor | None = None
        self.destination = ""
        self.channel = ""

    def live(self) -> bool:
        h = self.harness
        return (
            asyncio.current_task() is self.worker
            and self.worker is not None
            and not self.worker.cancelling()
            and h._worker is self.worker
            and not h._closed
            and not h._is_subagent
            and h.session_id == self.session_id
            and h.agent.session is self.adapter
            and h.agent._execution_bridge is self
            and h.agent._active_runs == 1
            and bool(self.turn_id)
        )

    @contextmanager
    def turn(self, session_id: str | None) -> Iterator[None]:
        if self.turn_id or session_id != self.session_id:
            raise RuntimeError("Nested or mismatched delivery turn")
        self.turn_id = uuid.uuid4().hex
        token = _current.set(self)
        try:
            yield
        finally:
            self.invocation = None
            self.block = None
            self.call = None
            self.approved_call = None
            self.turn_id = ""
            _current.reset(token)

    @contextmanager
    def reserve(self, message: "Message", events: tuple["ToolCallEvent", ...]) -> Iterator[None]:
        if len(message.tool_calls) != len(events):
            raise RuntimeError("Committed tool block is missing its streamed call correspondence")
        block = _Block(message, copy.deepcopy(message.tool_calls), events)
        self.block = block
        try:
            yield
        finally:
            block.writing = False

    async def abandon(self, events: tuple["ToolCallEvent", ...]) -> None:
        if self.live() and events:
            await self.harness.emit(TranscriptAbandoned(self.session_id, events))

    @contextmanager
    def executing(self, call: "ToolCall", position: int) -> Iterator[None]:
        self.call, self.position = call, position
        try:
            yield
        finally:
            self.call, self.position = None, -1

    @contextmanager
    def invoke(
        self, executor: "HarnessExecutor", definition: "ToolDefinition", original: "ToolCall", call: "ToolCall"
    ) -> Iterator[None]:
        block = self.block
        allowed = (
            self.live()
            and self.invocation is None
            and executor is self.harness.agent.tool_executor
            and definition is self.harness._delivery_definition
            and self.harness._delivery_binding is not None
            and definition.func is self.harness._delivery_binding.func
            and definition.parameters == self.harness._delivery_binding.parameters
            and definition.name == "channel_send"
            and original is self.call
            and block is not None
            and not block.writing
            and block.row_id > 0
            and 0 <= self.position < len(block.calls)
            and block.calls[self.position] == call
            and isinstance(call.arguments.get("channel"), str)
            and call.arguments.get("destination") == self.session_id
        )
        previous = self.invocation
        previous_call = self.approved_call
        self.invocation = None
        self.approved_call = None
        if allowed and block is not None:
            self.definition, self.executor = definition, executor
            self.approved_call = call
            self.approved = copy.copy(definition)
            self.approved.parameters = copy.deepcopy(definition.parameters)
            self.channel = str(call.arguments["channel"])
            self.destination = str(call.arguments["destination"])
            self.invocation = DeliveryOrigin(
                self.session_id,
                self.session_id,
                self.run_id,
                self.turn_id,
                self.harness._task_id,
                self.harness._activation,
                uuid.uuid4().hex,
                block.row_id,
                self.position,
                call.id,
                call.name,
            )
        token = _current.set(self if allowed else None)
        try:
            yield
        finally:
            self.invocation = previous
            self.approved_call = previous_call
            _current.reset(token)


def _execution_for(harness: "Harness") -> ExecutionBridge | None:
    """Concrete executor access stays in Harness, never on the core protocol."""
    bridge = _current.get()
    if bridge is None or bridge.harness is not harness or asyncio.current_task() is not bridge.worker:
        return None
    return bridge


@contextmanager
def _mask_delivery_authority() -> Iterator[None]:
    """Every executor entry hides ambient authority, including foreign Harnesses.

    Mask the task-local context rather than mutating the ambient bridge: copied
    contexts in other tasks must not invalidate the legitimate producer.
    """
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def capture_write(adapter: "SessionManager", session_id: str, message: "Message") -> _Block | None:
    """Called by the owned adapter in the caller, before spawning its DB task."""
    bridge = _current.get()
    if bridge is None or not bridge.live() or bridge.adapter is not adapter or bridge.session_id != session_id:
        return None
    block = bridge.block
    if block is None or not block.writing or block.message is not message or block.row_id:
        return None
    return block


async def publish_anchor(adapter: "SessionManager", session_id: str, block: _Block) -> None:
    """Preserve producer queue ordering between streamed calls and their results."""
    bridge = _current.get()
    if bridge is not None and bridge.live() and bridge.adapter is adapter and block is bridge.block:
        await bridge.harness.emit(TranscriptAnchor(session_id, block.row_id, block.events))


@contextmanager
def bind_channel_send(harness: "Harness", definition: "ToolDefinition") -> Iterator[None]:
    """Host-only registration binding; tool names alone never authorize a sink."""
    if definition.name != "channel_send" or harness.agent.tool_registry.get(definition.name) is not definition:
        raise ValueError("Bind the registered channel_send definition")
    previous = harness._delivery_definition
    previous_binding = harness._delivery_binding
    harness._delivery_definition = definition
    harness._delivery_binding = copy.copy(definition)
    harness._delivery_binding.parameters = copy.deepcopy(definition.parameters)
    try:
        yield
    finally:
        harness._delivery_definition = previous
        harness._delivery_binding = previous_binding


def delivery_origin(harness: "Harness", channel: str, destination: str) -> DeliveryOrigin:
    """Require the bound callable's live authority for this exact channel/destination.

    Returns the shared immutable DeliveryOrigin. Call in the Harness producer,
    before starting the owned journal commit, never in its database worker.
    """
    bridge = _current.get()
    if (
        bridge is None
        or bridge.harness is not harness
        or not bridge.live()
        or bridge.invocation is None
        or bridge.executor is not harness.agent.tool_executor
        or bridge.definition is not harness._delivery_definition
        or bridge.definition is None
        or bridge.approved is None
        or bridge.definition.func is not bridge.approved.func
        or bridge.definition.parameters != bridge.approved.parameters
        or harness.agent.tool_registry.get("channel_send") is not bridge.definition
        or bridge.call is None
        or bridge.approved_call is None
        or bridge.block is None
        or bridge.position != bridge.invocation.call_position
        or bridge.block.row_id != bridge.invocation.anchor_message_id
        or bridge.turn_id != bridge.invocation.turn_id
        or bridge.run_id != bridge.invocation.host_run_id
        or harness._task_id != bridge.invocation.task_id
        or harness._activation != bridge.invocation.activation
        or bridge.call != bridge.block.calls[bridge.position]
        or bridge.approved_call != bridge.block.calls[bridge.position]
        or bridge.call.arguments.get("channel") != channel
        or bridge.call.arguments.get("destination") != destination
        or bridge.approved_call.arguments.get("channel") != channel
        or bridge.approved_call.arguments.get("destination") != destination
        or channel != bridge.channel
        or destination != bridge.destination
        or destination != bridge.session_id
    ):
        raise PermissionError("No live owned channel_send invocation")
    return bridge.invocation


def revalidate_origin(harness: "Harness", channel: str, destination: str, origin: DeliveryOrigin) -> None:
    """Recheck in the producer immediately before starting an owned commit.

    The caller must join that operation, including on cancellation, before the
    callable returns and its invocation expires. The DB worker needs only data.
    """
    if delivery_origin(harness, channel, destination) is not origin:
        raise PermissionError("Delivery invocation has expired")
