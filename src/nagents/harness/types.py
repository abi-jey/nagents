"""Stable events shared by library, terminal, and command-line clients."""

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Literal

from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import ToolCallEvent
from nagents.types import ToolArguments


@dataclass(frozen=True)
class TranscriptAnchor:
    """Owned persistence notification for a committed root tool-call block."""

    session_id: str
    message_id: int
    # Host-local references to the exact events emitted by this generation,
    # in committed call-position order. Never serialized or matched by call ID.
    calls: tuple[ToolCallEvent, ...]


@dataclass(frozen=True)
class TranscriptAbandoned:
    """Streamed calls discarded without an assistant-row commit."""

    session_id: str
    calls: tuple[ToolCallEvent, ...]


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    description: str
    arguments: ToolArguments
    preview: str = ""
    session_id: str = ""
    title: str = ""
    depth: int = 0
    activation: int = 0
    mandatory: bool = False


@dataclass
class ToolOutput:
    call_id: str
    tool: str
    text: str
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class Notice:
    text: str
    level: str = "info"


@dataclass
class SessionInfo:
    id: str
    title: str
    updated_at: str
    forked_from: str = ""


@dataclass
class TaskStarted:
    session_id: str
    title: str
    prompt: str
    parent_session_id: str = ""
    root_session_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    followup: int = 0
    activation: int = 0
    trigger: str = "delegation"
    provider: str = ""
    provider_kind: str = ""
    model: str = ""
    timestamp: float = 0.0
    origin_history_id: str = ""
    origin_run_id: str = ""
    reasoning_effort: str = ""
    activation_history_id: str = ""
    activation_run_id: str = ""
    activation_call_id: str = ""
    activation_call_position: int = -1


@dataclass
class TaskCompleted:
    session_id: str
    title: str
    result: str
    error: str = ""
    parent_session_id: str = ""
    root_session_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    followup: int = 0
    status: Literal["running", "completed", "failed", "cancelled"] = "completed"
    activation: int = 0
    trigger: str = "delegation"
    provider: str = ""
    provider_kind: str = ""
    model: str = ""
    reasoning_effort: str = ""


@dataclass(kw_only=True)
class TaskDeliveryWarning(ErrorEvent):
    """Host-owned diagnostic: a stopped parent cannot receive this task's data.

    This is recoverable for the root run, not permission to retry delivery or
    forward descendant data elsewhere. The distinct Python type lets web clients
    recognize trusted task metadata without trusting an upstream error code.
    """

    message: str = field(
        default="The immediate parent stopped or was cancelled; this descendant result was not forwarded to Main.",
        init=False,
    )
    code: str = field(default="TASK_DELIVERY_SKIPPED", init=False)
    recoverable: bool = field(default=True, init=False)
    session_id: str
    title: str
    parent_session_id: str
    root_session_id: str
    depth: int
    activation: int
    followup: int


@dataclass
class TaskMessage:
    """A human follow-up accepted for a child, not a parent-model instruction."""

    session_id: str
    title: str
    prompt: str
    parent_session_id: str = ""
    root_session_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    followup: int = 1
    message_id: str = ""
    activation: int = 0


@dataclass(frozen=True)
class TaskActivity:
    """Public child evidence scoped by host identity, never root model output.

    Payloads contain only published text/tool activity and lifecycle metadata.
    Provider extras and reasoning events are deliberately not forwarded.
    """

    session_id: str
    root_session_id: str
    parent_session_id: str
    depth: int
    activation: int
    sequence: int
    timestamp: float
    turn_id: str
    payload: dict[str, object]


@dataclass
class TaskNotification:
    """An untrusted notification delivered at the recipient's model-run boundary."""

    notification_id: str
    source_session_id: str
    recipient_session_id: str
    source_title: str
    recipient_title: str
    cause: Literal["completion", "wakeup"]
    text: str
    root_session_id: str = ""
    source_activation: int = 0


HarnessEvent = (
    Event
    | ToolOutput
    | Notice
    | TaskStarted
    | TaskCompleted
    | TaskMessage
    | TaskNotification
    | TaskActivity
    | TranscriptAnchor
    | TranscriptAbandoned
)
ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool]]
WakeupHandler = Callable[[str, float, str], Awaitable[dict[str, str]]]
