"""Stable events shared by library, terminal, and command-line clients."""

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from nagents.events import Event
from nagents.types import ToolArguments


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


@dataclass
class ToolOutput:
    call_id: str
    tool: str
    text: str


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
    profile: str = "agent"
    followup: int = 0


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
    profile: str = "agent"
    followup: int = 0
    status: Literal["running", "completed", "failed", "cancelled"] = "completed"


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
    profile: str = "agent"
    followup: int = 1


HarnessEvent = Event | ToolOutput | Notice | TaskStarted | TaskCompleted | TaskMessage
ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool]]
