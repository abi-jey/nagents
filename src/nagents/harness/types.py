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
    task_id: str = ""
    task_name: str = ""
    depth: int = 0
    activation: int = 0


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


@dataclass
class TaskStarted:
    task_id: str
    name: str
    prompt: str
    parent_task_id: str = ""
    parent_session_id: str = ""
    child_session_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    followup: int = 0
    activation: int = 0
    trigger: str = "delegation"


@dataclass
class TaskCompleted:
    task_id: str
    name: str
    result: str
    error: str = ""
    parent_task_id: str = ""
    parent_session_id: str = ""
    child_session_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    followup: int = 0
    status: Literal["running", "completed", "failed", "cancelled"] = "completed"
    activation: int = 0
    trigger: str = "delegation"


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
    task_id: str
    task_name: str
    parent_task_id: str
    parent_session_id: str
    child_session_id: str
    depth: int
    activation: int
    followup: int


@dataclass
class TaskMessage:
    """A human follow-up accepted for a child, not a parent-model instruction."""

    task_id: str
    name: str
    prompt: str
    parent_task_id: str = ""
    parent_session_id: str = ""
    child_session_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    followup: int = 1


@dataclass
class TaskNotification:
    """An untrusted notification delivered at the recipient's model-run boundary."""

    notification_id: str
    source_task_id: str
    recipient_task_id: str
    source_name: str
    recipient_name: str
    cause: Literal["completion", "wakeup"]
    text: str


HarnessEvent = (
    Event
    | ToolOutput
    | Notice
    | TaskStarted
    | TaskCompleted
    | TaskMessage
    | TaskNotification
    | TranscriptAnchor
    | TranscriptAbandoned
)
ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool]]
WakeupHandler = Callable[[str, float, str], Awaitable[dict[str, str]]]
