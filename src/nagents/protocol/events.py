"""Events recorded and streamed by the engine; subtype identity is explicit."""

from typing import Annotated
from typing import Literal

from pydantic import Field

from .models import Counter
from .models import Cursor
from .models import EngineInfo
from .models import Identifier
from .models import RunStatus
from .models import Session
from .models import TaskInfo
from .models import ToolCall
from .models import ToolResult
from .models import WireModel


class RunQueued(WireModel):
    type: Literal["run.queued"] = "run.queued"
    prompt: str


class QueueUpdated(WireModel):
    type: Literal["queue.updated"] = "queue.updated"
    paused: bool


class RunStarted(WireModel):
    type: Literal["run.started"] = "run.started"
    prompt: str


class SteeringUpdated(WireModel):
    type: Literal["steering.updated"] = "steering.updated"
    input_id: str


class TextDelta(WireModel):
    type: Literal["text.delta"] = "text.delta"
    text: str


class TextCompleted(WireModel):
    type: Literal["text.completed"] = "text.completed"
    text: str


class ToolStarted(WireModel):
    type: Literal["tool.started"] = "tool.started"
    call: ToolCall


class ToolCompleted(WireModel):
    type: Literal["tool.completed"] = "tool.completed"
    result: ToolResult
    duration_ms: Counter


class ToolApprovalTiming(WireModel):
    type: Literal["tool.approval_timing"] = "tool.approval_timing"
    call_id: str
    elapsed_ms: Counter
    waiting: bool


class ApprovalRequested(WireModel):
    type: Literal["approval.requested"] = "approval.requested"
    approval_id: Identifier
    call: ToolCall
    preview: str


class ApprovalResolved(WireModel):
    type: Literal["approval.resolved"] = "approval.resolved"
    approval_id: Identifier
    allowed: bool


class Notice(WireModel):
    type: Literal["notice"] = "notice"
    text: str


class TaskStarted(WireModel):
    type: Literal["task.started"] = "task.started"
    task: TaskInfo


class TaskUpdated(WireModel):
    type: Literal["task.updated"] = "task.updated"
    task: TaskInfo


class TaskFinished(WireModel):
    type: Literal["task.finished"] = "task.finished"
    task: TaskInfo
    error: str


class RunFinished(WireModel):
    type: Literal["run.finished"] = "run.finished"
    status: RunStatus
    error: str


class SessionUpdated(WireModel):
    type: Literal["session.updated"] = "session.updated"
    session: Session


class EngineConnected(WireModel):
    type: Literal["engine.connected"] = "engine.connected"
    engine: EngineInfo


type EngineEvent = Annotated[
    RunQueued
    | QueueUpdated
    | RunStarted
    | SteeringUpdated
    | TextDelta
    | TextCompleted
    | ToolStarted
    | ToolCompleted
    | ToolApprovalTiming
    | ApprovalRequested
    | ApprovalResolved
    | Notice
    | TaskStarted
    | TaskUpdated
    | TaskFinished
    | RunFinished
    | SessionUpdated
    | EngineConnected,
    Field(discriminator="type"),
]


class EventRecord(WireModel):
    schema_version: Literal[1]
    sequence: Annotated[Cursor, Field(description="Increasing event ID; gaps can occur in durable replay.")]
    session_id: str
    run_id: str
    timestamp_ms: Counter
    event: EngineEvent


class History(WireModel):
    events: list[EventRecord]
    cursor: Annotated[Cursor, Field(description="Exclusive cursor for the next page or a stream subscription.")]
    has_more: bool
