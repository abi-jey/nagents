"""Transport-neutral contracts for independently installable channel connectors."""

from abc import ABC
from abc import abstractmethod
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import TypeAlias

from nagents.events import Event
from nagents.types import JsonSchema

ChannelValue: TypeAlias = str | int | float | bool | list["ChannelValue"] | dict[str, "ChannelValue"] | None


@dataclass(frozen=True)
class ChannelAttachment:
    """A connector-owned reference, not automatically downloaded or executed."""

    reference: str
    media_type: str = "application/octet-stream"
    filename: str = ""
    size: int = 0


@dataclass(frozen=True)
class ChannelMessage:
    """An external event. Its identity is stable within the connector instance.

    Conversation/thread IDs describe the source; they do not select an Agent
    session. All connectors attached to one listener share that listener's session.
    ``reply_to`` is a transport message ID suitable for replying to this event;
    it can differ from the ingress ``message_id`` used for deduplication.
    Metadata must be JSON-compatible and must not contain credentials.
    """

    message_id: str
    conversation_id: str
    sender_id: str
    text: str = ""
    thread_id: str = ""
    reply_to: str = ""
    event_type: str = "message"
    attachments: tuple[ChannelAttachment, ...] = ()
    metadata: dict[str, ChannelValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelSend:
    """An explicit outgoing message selected by the application or model."""

    destination: str
    text: str
    thread_id: str = ""
    reply_to: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()
    metadata: dict[str, ChannelValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelDelivery:
    """Remote message identifiers returned after confirmed delivery."""

    message_ids: tuple[str, ...]
    metadata: dict[str, ChannelValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ChannelAction:
    """Advertise an integration-specific operation, such as editing a message."""

    name: str
    description: str
    parameters: JsonSchema


@dataclass(frozen=True)
class ChannelCommand:
    """A connector-recognized host command; the host decides which commands exist."""

    name: str
    arguments: str = ""


@dataclass(frozen=True)
class ChannelActivity:
    """Session activity for transport indicators, independent of model replies."""

    conversation_id: str
    active: bool
    thread_id: str = ""
    session_id: str = ""


class ChannelError(Exception):
    """A sanitized connector failure suitable for a tool result.

    No implicit send retries are performed. ``outcome_unknown`` means a remote
    side effect may have happened. Never put credential-bearing URLs in errors.
    """

    def __init__(self, message: str, *, retry_after: float = 0, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown


# Return only after durable inbox admission. Raising leaves the source event
# unacknowledged. Acceptance does not mean model execution or delivery succeeded.
ChannelReceiver: TypeAlias = Callable[[ChannelMessage], Awaitable[None]]


@dataclass(frozen=True)
class ChannelEvent:
    """Observe execution locally; observation never sends a channel reply."""

    session_id: str
    channel: str
    message_id: str
    event: Event


ChannelEventHandler: TypeAlias = Callable[[ChannelEvent], Awaitable[None]]


async def discard_event(event: ChannelEvent) -> None:
    """Default local observer for applications interested only in channel actions."""


class Channel(ABC):
    """A trusted connector. Installations remain explicit; imports do not connect.

    ``open`` initializes resources, ``listen`` delivers events with backpressure,
    and ``close`` releases resources after producers/execution have stopped.
    Transport acknowledgements belong to the connector. Agent responses are
    emitted only through explicit send/action tools, never auto-forwarded.
    """

    name: str
    description: str = ""
    capabilities: tuple[str, ...] = ("receive", "send_text")
    actions: tuple[ChannelAction, ...] = ()

    async def open(self) -> None:
        return None

    @abstractmethod
    async def listen(self, receive: ChannelReceiver) -> None:
        """Deliver events until cancelled, or return when a finite source ends."""

    @abstractmethod
    async def send(self, message: ChannelSend) -> ChannelDelivery:
        """Send once; return confirmed IDs or raise a sanitized ChannelError."""

    async def action(self, name: str, arguments: dict[str, ChannelValue]) -> dict[str, ChannelValue]:
        raise ChannelError(f"Unsupported channel action: {name}")

    def command(self, message: ChannelMessage) -> ChannelCommand | None:
        """Recognize an explicit transport command without doing I/O."""
        return None

    async def activity(self, event: ChannelActivity) -> None:
        """Start/stop an optional typing indicator; close() must stop keepalives."""
        return None

    async def close(self) -> None:
        return None


ChannelFactory: TypeAlias = Callable[[dict[str, ChannelValue]], Channel]


@dataclass(frozen=True)
class ChannelPlugin:
    """Callable entry point with a configuration schema for management clients.

    JSON Schema properties marked ``writeOnly: true`` are credentials: clients
    collect them separately and never echo saved values. The host validates and
    persists configuration; the factory performs connector-specific validation.
    Ordinary callable factories remain supported by ``load_channel``.
    """

    name: str
    description: str
    config_schema: dict[str, ChannelValue]
    factory: ChannelFactory

    def __call__(self, config: dict[str, ChannelValue]) -> Channel:
        return self.factory(config)
