"""Version-one HTTP messages shared by the engine and its clients."""

from typing import Annotated
from typing import Literal

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue

MAX_CURSOR = 9_007_199_254_740_991  # Precisely representable in a browser.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
REQUEST_ID_PATTERN = r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"

type Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)]
type RequestId = Annotated[str, Field(pattern=REQUEST_ID_PATTERN)]
type Cursor = Annotated[int, Field(ge=0, le=MAX_CURSOR)]
type Counter = Annotated[int, Field(ge=0)]
type Title = Annotated[str, Field(min_length=1, max_length=160)]
type RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
type RunMode = Literal["queue", "idle"]
type QueueAction = Literal["pause", "resume", "clear"]
type SteeringStatus = Literal["pending", "offered", "applied", "uncertain", "undelivered"]


def _validate_prompt(value: str) -> str:
    if not value.strip() or len(value.encode("utf-8")) > 32_000:
        raise ValueError("Prompt must be nonblank and at most 32,000 UTF-8 bytes.")
    return value


type Prompt = Annotated[str, Field(min_length=1, max_length=32_000), AfterValidator(_validate_prompt)]


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class RequestModel(WireModel):
    # A request must not silently drop information that affects its authority.
    model_config = ConfigDict(strict=True, extra="forbid")


class EngineInfo(WireModel):
    protocol_version: Annotated[int, Field(ge=1)]
    protocol_revision: Counter
    version: str
    capabilities: list[str]


class EngineConfig(WireModel):
    workspace: str
    provider: str
    model: str | None
    read_only: bool


class Session(WireModel):
    id: Identifier
    title: str
    updated_at_ms: Counter
    archived: bool


class SessionCreate(RequestModel):
    title: Title = "New session"


class SessionRename(RequestModel):
    title: Title


class SessionFork(RequestModel):
    # Omit for normal fork naming; explicit null asks for the default title.
    title: Title | None = None


class Run(WireModel):
    id: Identifier
    session_id: Identifier
    request_id: RequestId
    status: RunStatus


class RunStart(RequestModel):
    prompt: Prompt
    request_id: RequestId
    mode: RunMode = "queue"
    interactive: bool = False


class InputLookup(RequestModel):
    prompt: Prompt
    request_id: RequestId


class InputReceipt(WireModel):
    request_id: RequestId
    session_id: Identifier
    status: RunStatus
    # Before dispatch, a durable input need not have a runtime activation ID.
    run_id: Identifier | None
    warning: str = ""


class QueueEntry(WireModel):
    request_id: RequestId
    prompt: str
    run_id: Identifier | None


class QueueState(WireModel):
    session_id: Identifier
    paused: bool
    entries: list[QueueEntry]
    limit: Counter


class QueueUpdate(RequestModel):
    action: QueueAction


class SteeringInput(RequestModel):
    prompt: Prompt
    request_id: RequestId


class SteeringReceipt(WireModel):
    input_id: Identifier
    request_id: RequestId
    run_id: Identifier
    session_id: Identifier
    prompt: str
    position: Counter
    status: SteeringStatus


class SteeringState(WireModel):
    run_id: Identifier
    session_id: Identifier
    run_status: RunStatus
    supported: bool
    accepting: bool
    receipts: list[SteeringReceipt]


class ToolCall(WireModel):
    call_id: str
    name: str
    arguments: dict[str, JsonValue]


class ToolResult(WireModel):
    call_id: str
    name: str
    result: JsonValue
    error: str


class Approval(WireModel):
    id: Identifier
    run_id: Identifier
    session_id: Identifier
    root_session_id: Identifier
    call: ToolCall
    preview: str
    agent_title: str
    depth: Counter


class ApprovalDecision(RequestModel):
    run_id: Identifier
    call_id: Annotated[str, Field(min_length=1, max_length=200)]
    allow: bool


class ApprovalReceipt(WireModel):
    approval_id: Identifier
    allowed: bool


class TaskInfo(WireModel):
    run_id: Identifier
    session_id: Identifier
    parent_run_id: Identifier
    parent_session_id: Identifier
    root_run_id: Identifier
    root_session_id: Identifier
    call_id: str
    title: str
    depth: Counter
    read_only: bool
    status: RunStatus


class EngineSnapshot(WireModel):
    runs: list[Run]
    approvals: list[Approval]


class Provider(WireModel):
    id: Identifier
    kind: str


class Model(WireModel):
    id: str
    provider_id: Identifier
    label: str


class ErrorResponse(WireModel):
    detail: str
