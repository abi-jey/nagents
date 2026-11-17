"""Private built-in public-API plugin. Never inject from ToolExecutor or UI events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID
from uuid import uuid4

from nagents.extensions import AgentPlugin
from nagents.extensions import RunContext
from nagents.types import Message

from .protocol import Emitter
from .protocol import ProtocolError
from .protocol import fields
from .protocol import mapping
from .protocol import text


@dataclass(frozen=True)
class _Offer:
    boundary_id: str
    input_id: str
    request_id: str
    position: int
    prompt: str


class _Steering:
    def __init__(self, run_id: str, emit: Emitter) -> None:
        self.run_id = run_id
        self.emit = emit
        self.pending: asyncio.Future[_Offer | None] | None = None
        self.boundary_id = ""
        self.offered: _Offer | None = None
        self.inputs: set[str] = set()
        self.requests: set[str] = set()
        self.position = 0

    async def boundary(self, kind: str) -> _Offer | None:
        if kind not in {"after_tools", "final"} or self.pending is not None or self.offered is not None:
            raise ProtocolError("Invalid or overlapping steering boundary")
        # A fresh UUID for every boundary, including different Agent.run calls in
        # this process. No model/session value can choose this correlation ID.
        self.boundary_id = str(uuid4())
        self.pending = asyncio.get_running_loop().create_future()
        try:
            self.emit(self.run_id, {"type": "steering_boundary", "boundary_id": self.boundary_id, "kind": kind})
            self.offered = await self.pending
            return self.offered
        finally:
            self.pending = None

    def accept(self, message: dict[str, object]) -> None:
        fields(message, {"type", "boundary_id", "input"})
        if message["type"] != "steering_offer":
            raise ProtocolError("Expected a steering offer")
        boundary_id = text(message, "boundary_id", 80)
        if self.pending is None or self.pending.done() or boundary_id != self.boundary_id:
            raise ProtocolError("Steering offer is not bound to a pending boundary")
        offer: _Offer | None = None
        if message["input"] is not None:
            value = mapping(message["input"])
            fields(value, {"input_id", "request_id", "position", "prompt"})
            input_id = text(value, "input_id", 80)
            request_id = text(value, "request_id", 80)
            position = value["position"]
            prompt = text(value, "prompt")
            try:
                valid_id = input_id == "steer-" + UUID(input_id.removeprefix("steer-")).hex
                valid_request = request_id == str(UUID(request_id))
            except ValueError:
                raise ProtocolError("Invalid host steering identity") from None
            if (
                not valid_id
                or not valid_request
                or type(position) is not int
                or position <= self.position
                or not prompt.strip()
                or input_id in self.inputs
                or request_id in self.requests
                or len(self.inputs) >= 32
            ):
                raise ProtocolError("Invalid, duplicate or unordered steering offer")
            offer = _Offer(boundary_id, input_id, request_id, position, prompt)
            self.inputs.add(input_id)
            self.requests.add(request_id)
            self.position = position
        self.pending.set_result(offer)

    def applied(self, offer: _Offer) -> None:
        if self.offered != offer:
            raise ProtocolError("Steering persistence acknowledgment has no matching offer")
        self.emit(
            self.run_id,
            {"type": "steering_applied", "boundary_id": offer.boundary_id, "input_id": offer.input_id},
        )
        self.offered = None


class _SteeringPlugin(AgentPlugin):
    def __init__(self, steering: _Steering, session_id: str) -> None:
        self.steering = steering
        self.session_id = session_id
        self.continuation: _Offer | None = None

    def _check_context(self, context: RunContext) -> None:
        if context.session_id != self.session_id:
            raise ProtocolError("Steering session identity mismatch")

    async def before_run(self, context: RunContext, message: Message) -> Message:
        self._check_context(context)
        if self.continuation is not None and (message.role != "user" or message.content != self.continuation.prompt):
            raise ProtocolError("Steering continuation does not match the offered input")
        return message

    async def before_generation(self, context: RunContext) -> None:
        self._check_context(context)
        if self.continuation is not None:
            # Agent.run has now persisted its initial user message. Do not insert
            # it a second time, and do not acknowledge merely on offer receipt.
            self.steering.applied(self.continuation)
            self.continuation = None

    async def after_tools(self, context: RunContext) -> None:
        self._check_context(context)
        # Public lifecycle contract: ALL tool results are persisted before here.
        # The executor never asks for/offers steering between calls in a batch.
        while (offer := await self.steering.boundary("after_tools")) is not None:
            await context.agent.session.add_message(context.session_id, Message(role="user", content=offer.prompt))
            self.steering.applied(offer)
