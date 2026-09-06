"""Stable events shared by library, terminal, and command-line clients."""

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass

from nagents.events import Event
from nagents.types import ToolArguments


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    description: str
    arguments: ToolArguments
    preview: str = ""


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


HarnessEvent = Event | ToolOutput | Notice
ApprovalHandler = Callable[[ApprovalRequest], Awaitable[bool]]
