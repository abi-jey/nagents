"""The typed FastAPI surface, shared by the live engine and contract exporter."""

from typing import TYPE_CHECKING
from typing import Annotated
from typing import Protocol

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Path
from fastapi import Query
from fastapi import WebSocket
from fastapi.security import HTTPBearer

from nagents.protocol import EVENTS_SUBPROTOCOL
from nagents.protocol import PROTOCOL_REVISION
from nagents.protocol import PROTOCOL_VERSION
from nagents.protocol.events import History
from nagents.protocol.models import IDENTIFIER_PATTERN
from nagents.protocol.models import MAX_CURSOR
from nagents.protocol.models import ApprovalDecision
from nagents.protocol.models import ApprovalReceipt
from nagents.protocol.models import EngineConfig
from nagents.protocol.models import EngineInfo
from nagents.protocol.models import EngineSnapshot
from nagents.protocol.models import ErrorResponse
from nagents.protocol.models import InputLookup
from nagents.protocol.models import InputReceipt
from nagents.protocol.models import Model
from nagents.protocol.models import Provider
from nagents.protocol.models import QueueState
from nagents.protocol.models import QueueUpdate
from nagents.protocol.models import Run
from nagents.protocol.models import RunStart
from nagents.protocol.models import Session
from nagents.protocol.models import SessionCreate
from nagents.protocol.models import SessionFork
from nagents.protocol.models import SessionRename
from nagents.protocol.models import SteeringInput
from nagents.protocol.models import SteeringReceipt
from nagents.protocol.models import SteeringState
from nagents.protocol.models import TaskInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

API_PREFIX = f"/api/v{PROTOCOL_VERSION}"
API_VERSION = f"{PROTOCOL_VERSION}.{PROTOCOL_REVISION}"
EVENTS_PATH = f"{API_PREFIX}/events"
PathId = Annotated[str, Path(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)]
After = Annotated[int, Query(ge=0, le=MAX_CURSOR)]
HistoryLimit = Annotated[int, Query(ge=1, le=1000)]
SessionFilter = Annotated[str, Query(max_length=200, pattern=r"^([A-Za-z0-9][A-Za-z0-9._-]*)?$")]


class Backend(Protocol):
    async def info(self) -> EngineInfo: ...
    async def config(self) -> EngineConfig: ...
    async def sessions(self) -> list[Session]: ...
    async def create_session(self, body: SessionCreate) -> Session: ...
    async def session(self, session_id: str) -> Session: ...
    async def rename_session(self, session_id: str, body: SessionRename) -> Session: ...
    async def fork_session(self, session_id: str, body: SessionFork) -> Session: ...
    async def history(self, session_id: str, after: int, limit: int) -> History: ...
    async def start_run(self, session_id: str, body: RunStart) -> InputReceipt: ...
    async def input_receipt(self, session_id: str, body: InputLookup) -> InputReceipt | None: ...
    async def cancel_run(self, run_id: str) -> Run: ...
    async def queue(self, session_id: str) -> QueueState: ...
    async def control_queue(self, session_id: str, body: QueueUpdate) -> QueueState: ...
    async def steering(self, run_id: str) -> SteeringState: ...
    async def steer(self, run_id: str, body: SteeringInput) -> SteeringReceipt: ...
    async def tasks(self, session_id: str) -> list[TaskInfo]: ...
    async def snapshot(self) -> EngineSnapshot: ...
    async def decide_approval(self, approval_id: str, body: ApprovalDecision) -> ApprovalReceipt: ...
    async def providers(self) -> list[Provider]: ...
    async def models(self, provider_id: str) -> list[Model]: ...
    async def events(self, socket: WebSocket, after: int, session_id: str) -> None: ...


def install_api(app: "FastAPI", backend: "Callable[[], Backend]") -> None:
    """Mount the v1 transport. The hosting app must authenticate HTTP and WebSockets."""
    app.version = API_VERSION
    scheme = HTTPBearer(scheme_name="EngineToken")
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(scheme)],
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )

    @router.get("/info", operation_id="info")
    async def get_info() -> EngineInfo:
        return await backend().info()

    @router.get("/config", operation_id="config")
    async def get_config() -> EngineConfig:
        return await backend().config()

    @router.get("/sessions", operation_id="sessions")
    async def list_sessions() -> list[Session]:
        return await backend().sessions()

    @router.post("/sessions", operation_id="create_session", status_code=201)
    async def create_session(body: SessionCreate) -> Session:
        return await backend().create_session(body)

    @router.get("/sessions/{session_id}", operation_id="session")
    async def get_session(session_id: PathId) -> Session:
        return await backend().session(session_id)

    @router.post("/sessions/{session_id}/rename", operation_id="rename_session")
    async def rename_session(session_id: PathId, body: SessionRename) -> Session:
        return await backend().rename_session(session_id, body)

    @router.post("/sessions/{session_id}/fork", operation_id="fork_session", status_code=201)
    async def fork_session(session_id: PathId, body: SessionFork) -> Session:
        return await backend().fork_session(session_id, body)

    @router.get("/sessions/{session_id}/history", operation_id="history")
    async def get_history(session_id: PathId, after: After = 0, limit: HistoryLimit = 256) -> History:
        return await backend().history(session_id, after, limit)

    @router.post("/sessions/{session_id}/runs", operation_id="start_run", status_code=202)
    async def start_run(session_id: PathId, body: RunStart) -> InputReceipt:
        return await backend().start_run(session_id, body)

    @router.post("/sessions/{session_id}/receipts", operation_id="input_receipt")
    async def input_receipt(session_id: PathId, body: InputLookup) -> InputReceipt | None:
        # Keep submitted text out of the URL. This lookup does not admit input.
        return await backend().input_receipt(session_id, body)

    @router.post("/runs/{run_id}/cancel", operation_id="cancel_run")
    async def cancel_run(run_id: PathId) -> Run:
        return await backend().cancel_run(run_id)

    @router.get("/sessions/{session_id}/queue", operation_id="queue")
    async def get_queue(session_id: PathId) -> QueueState:
        return await backend().queue(session_id)

    @router.post("/sessions/{session_id}/queue", operation_id="control_queue")
    async def control_queue(session_id: PathId, body: QueueUpdate) -> QueueState:
        return await backend().control_queue(session_id, body)

    @router.get("/runs/{run_id}/steer", operation_id="steering")
    async def get_steering(run_id: PathId) -> SteeringState:
        return await backend().steering(run_id)

    @router.post("/runs/{run_id}/steer", operation_id="steer")
    async def steer(run_id: PathId, body: SteeringInput) -> SteeringReceipt:
        return await backend().steer(run_id, body)

    @router.get("/sessions/{session_id}/tasks", operation_id="tasks")
    async def tasks(session_id: PathId) -> list[TaskInfo]:
        return await backend().tasks(session_id)

    @router.get("/state", operation_id="snapshot")
    async def snapshot() -> EngineSnapshot:
        return await backend().snapshot()

    @router.post("/approvals/{approval_id}", operation_id="decide_approval")
    async def decide_approval(approval_id: PathId, body: ApprovalDecision) -> ApprovalReceipt:
        return await backend().decide_approval(approval_id, body)

    @router.get("/providers", operation_id="providers")
    async def providers() -> list[Provider]:
        return await backend().providers()

    @router.get("/providers/{provider_id}/models", operation_id="models")
    async def models(provider_id: PathId) -> list[Model]:
        return await backend().models(provider_id)

    app.include_router(router)

    @app.websocket(EVENTS_PATH)
    async def events(socket: WebSocket, after: After = 0, session_id: SessionFilter = "") -> None:
        if EVENTS_SUBPROTOCOL not in socket.scope.get("subprotocols", []):
            await socket.close(code=1002)
            return
        await backend().events(socket, after, session_id)


def schema_backend() -> Backend:
    """Schema export binds a router without creating an engine or any state."""
    raise HTTPException(status_code=503, detail="No engine is bound to this contract.")
