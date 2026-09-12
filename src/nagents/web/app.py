"""Single-Harness HTTP adapter with an owning, cancellable NDJSON response."""

import asyncio
import copy
import json
import secrets
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import aclosing
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal

import anyio
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.staticfiles import StaticFiles

from nagents.cli import _event_record
from nagents.cli import _json_default
from nagents.events import ErrorEvent
from nagents.harness import Harness
from nagents.harness.types import ApprovalRequest
from nagents.provider import CodexProvider

from . import built_assets
from . import local_authority
from .dictation import WebDictation
from .security import SECURITY_HEADERS
from .security import LocalOnly
from .settings import SettingsInput
from .settings import SettingsRevision
from .settings import WebSettings
from .settings import _join
from .wakeups import Chain
from .wakeups import Wakeup
from .wakeups import Wakeups

if TYPE_CHECKING:
    from starlette.types import Receive
    from starlette.types import Scope
    from starlette.types import Send

    from nagents.harness.config import HarnessConfig

APPROVAL_TIMEOUT = 300


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInput(Input):
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")


class PromptInput(SessionInput):
    prompt: str = Field(min_length=1, max_length=32000)


class RunInput(Input):
    run_id: str = Field(min_length=1, max_length=80)


class DecisionInput(RunInput):
    approval_id: str = Field(min_length=1, max_length=80)
    call_id: str = Field(min_length=1, max_length=200)
    decision: Literal["allow", "deny"]


@dataclass
class Pending:
    id: str
    call_id: str
    answer: asyncio.Future[bool]


@dataclass
class Run:
    session_id: str
    id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    queue: asyncio.Queue[dict[str, object]] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    task: asyncio.Task[None] = field(init=False)
    pending: Pending | None = None
    outcome: str = "completed"
    background: bool = False
    chain: Chain = field(default_factory=Chain)


class WebState:
    settings: WebSettings

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.active: Run | None = None
        self.mutating = False
        self.dictation = WebDictation()
        self.wakeups = Wakeups(lambda: self.active is None and not self.mutating, self.wake)
        harness.approval_handler = self.approve
        harness.wakeup_handler = self.schedule

    @contextmanager
    def idle(self) -> Iterator[None]:
        if self.active is not None or self.mutating:
            raise HTTPException(409, "Harness busy. Cancel or finish the active operation first.")
        self.mutating = True
        try:
            yield
        finally:
            self.mutating = False
            self.wakeups.changed.set()

    async def snapshot(self) -> dict[str, object]:
        # Only the currently selected, workspace-checked session can be read.
        history = await self.harness.history()
        return {
            "session_id": self.harness.session_id,
            "activity_cursor": self.wakeups.cursor,
            "retained_tasks": [asdict(info) for info in self.harness.tasks.list()],
            "sessions": [asdict(session) for session in await self.harness.list_sessions()],
            "history": [
                {
                    "role": message.role,
                    "content": message.content if isinstance(message.content, str) else "",
                    "name": message.name or "",
                    "tool_call_id": message.tool_call_id or "",
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments} for call in message.tool_calls
                    ],
                }
                for message in history
                if message.role not in {"system", "developer"}
            ],
        }

    async def approve(self, request: ApprovalRequest) -> bool:
        run = self.active
        if run is None or run.pending is not None or run.task.cancelling():
            return False
        if run.background:
            self.publish(
                run,
                {
                    "event": "notice",
                    "text": "Unattended approval was denied; no action was taken.",
                    "task_id": request.task_id,
                    "activation": request.activation,
                    "call_id": request.id,
                    "tool": request.tool,
                },
            )
            return False
        pending = Pending(secrets.token_urlsafe(24), request.id, asyncio.get_running_loop().create_future())
        run.pending = pending
        approved = False
        expired = False
        try:
            await run.queue.put({"event": "approval", **asdict(request), "approval_id": pending.id, "run_id": run.id})
            # A forgotten tab never leaves an approval open indefinitely.
            approved = await asyncio.wait_for(pending.answer, timeout=APPROVAL_TIMEOUT)
            return approved
        except TimeoutError:
            expired = True
            await run.queue.put({"event": "notice", "text": "Approval expired and was denied."})
            return False
        finally:
            if not pending.answer.done():
                pending.answer.set_result(False)
            run.pending = None
            # The client also clears its dialog on run end or disconnect.
            if not run.task.cancelling():
                await run.queue.put(
                    {
                        "event": "approval_closed",
                        "approval_id": pending.id,
                        "task_id": request.task_id,
                        "activation": request.activation,
                        "call_id": request.id,
                        "decision": "allow" if approved else "deny",
                        "expired": expired,
                    }
                )

    async def schedule(self, task_id: str, delay: float, reason: str) -> dict[str, str]:
        run = self.active
        if run is None or run.task.cancelling() or run.task.done() or run.session_id != self.harness.session_id:
            raise RuntimeError("Wakeups require an active run in their originating session")
        return self.wakeups.schedule(run.session_id, run.id, run.chain, task_id, delay, reason)

    def publish(self, run: Run, record: dict[str, object]) -> None:
        record = {**record, "schema_version": 1, "run_id": run.id, "session_id": run.session_id}
        self.wakeups.publish(record)

    async def produce(self, run: Run, prompt: str, *, task_id: str = "") -> None:
        async def send(record: dict[str, object]) -> None:
            if run.background:
                self.publish(run, record)
            else:
                await run.queue.put(record)

        try:
            source = self.harness.wake(prompt, task_id=task_id) if run.background else self.harness.run(prompt)
            async with aclosing(source) as events:
                async for event in events:
                    if isinstance(event, ErrorEvent):
                        run.outcome = "failed"
                        record = {
                            "event": "error",
                            "message": "The provider run failed. Check local provider configuration before trying again.",
                            "recoverable": event.recoverable,
                        }
                    else:
                        record = _event_record(event)
                    await send(record)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            self.wakeups.cancel(run.chain)
            raise
        except Exception:
            run.outcome = "failed"
            await send({"event": "error", "message": "Run failed. Completed actions were not rolled back."})
        finally:
            if run.pending is not None and not run.pending.answer.done():
                run.pending.answer.set_result(False)
            run.pending = None

    async def wake(self, wakeup: Wakeup) -> None:
        # tick() checks idle and claims the timer without yielding. Own the slot
        # before session I/O, including restoration after cancellation or failure.
        run = Run(wakeup.session_id, background=True, chain=wakeup.chain)
        self.active = run
        self.publish(run, {"event": "run_started"})
        run.task = asyncio.create_task(self._wake(run, wakeup), name=f"ngn-web-{run.id}")
        try:
            await _join(run.task)
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            self.wakeups.cancel(run.chain)
        finally:
            if run.outcome != "completed":
                self.wakeups.lifecycle(wakeup, "cancelled" if run.outcome == "cancelled" else "failed", run_id=run.id)
            self.publish(run, {"event": "run_finished", "status": run.outcome})
            if self.active is run:
                self.active = None
            self.wakeups.changed.set()

    async def _wake(self, run: Run, wakeup: Wakeup) -> None:
        previous = self.harness.session_id
        try:
            await self.harness.resume(wakeup.session_id)
            self.wakeups.lifecycle(wakeup, "fired", run_id=run.id)
            await self.produce(run, wakeup.reason, task_id=wakeup.task_id)
        except Exception:
            run.outcome = "failed"
            self.publish(run, {"event": "error", "message": "Wakeup failed. Completed actions were not rolled back."})
        finally:
            # Restore the already workspace-checked selection without cancellable
            # database I/O. The run owns the mutation slot until this completes.
            self.harness.session_id = previous
            self.harness.tools.read_hashes.clear()

    async def stop(self, run: Run, *, cancel: bool = True) -> None:
        # Starlette's disconnect cancellation is level-triggered. Shield the join,
        # not execution, so Harness iterator/tool cleanup completes on its loop.
        with anyio.CancelScope(shield=True):
            if cancel:
                self.wakeups.cancel(run.chain)
            if not run.task.done() and not run.task.cancelling():
                run.outcome = "cancelled"
                run.task.cancel()
            with suppress(asyncio.CancelledError):
                await _join(run.task)
            if self.active is run:
                self.active = None
            self.wakeups.changed.set()


class RunResponse(StreamingResponse):
    def __init__(self, state: WebState, run: Run) -> None:
        self.state = state
        self.run = run
        self.finished = False
        super().__init__(self.events(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})

    async def events(self) -> AsyncIterator[str]:
        run = self.run

        def line(record: dict[str, object]) -> str:
            return (
                json.dumps(
                    {**record, "schema_version": 1, "run_id": run.id, "session_id": run.session_id},
                    default=_json_default,
                    ensure_ascii=True,
                )
                + "\n"
            )

        yield line({"event": "run_started", "session_id": run.session_id})
        while not run.task.done() or not run.queue.empty():
            try:
                record = await asyncio.wait_for(run.queue.get(), timeout=1)
            except TimeoutError:
                # Also detects disconnected ASGI 2.4 clients while tools are silent.
                yield line({"event": "heartbeat"})
            else:
                yield line(record)
        yield line({"event": "run_finished", "status": run.outcome, "session_id": run.session_id})
        self.finished = True

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Covers disconnect before the first body byte, not just generator exit.
            await self.state.stop(self.run, cancel=not self.finished)


def create_app(
    config: "HarnessConfig",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    assets: Path | None = None,
    resume_session: str = "",
    continue_session: bool = False,
    harness_factory: Callable[["HarnessConfig"], Harness] = Harness,
) -> FastAPI:
    authority = local_authority(host, port)
    directory = built_assets() if assets is None else assets
    token = secrets.token_urlsafe(32)
    state: WebState

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal state
        harness = harness_factory(copy.deepcopy(config))
        state = WebState(harness)
        try:
            await harness.initialize()
            state.settings = WebSettings(harness)
            await state.settings.load()
            if resume_session:
                await harness.resume(resume_session)
            elif continue_session:
                sessions = await harness.list_sessions()
                if sessions:
                    await harness.resume(sessions[0].id)
            state.wakeups.start()
            yield
        finally:
            state.wakeups.shutdown()

            async def close_resources() -> None:
                try:
                    if state.active is not None:
                        await state.stop(state.active)
                finally:
                    try:
                        await state.wakeups.close()
                    finally:
                        try:
                            await state.dictation.close()
                        finally:
                            await harness.close()

            cleanup = asyncio.create_task(close_resources())
            try:
                # Observe lifespan cancellation without cancelling cleanup. Only
                # the final join is shielded, so AnyIO cancellation also escapes.
                await asyncio.wait({cleanup})
            finally:
                await _join(cleanup)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnly, authority=authority, token=token)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid request fields or JSON body."}, status_code=422)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        # Do not return provider exceptions, credential-bearing URLs, or tracebacks.
        return JSONResponse(
            {"detail": "Local operation failed. Check ngn configuration and reconnect."},
            status_code=500,
            headers=SECURITY_HEADERS,
        )

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, object]:
        return {
            "schema_version": 1,
            "token": token,
            "workspace": str(state.harness.workspace),
            "provider": state.harness.config.provider,
            "model": state.settings.values.model,
            "agent": state.settings.values.agent,
            "demo": state.harness.config.demo,
            "dictation": state.settings.dictation_snapshot(),
            "active_run_id": state.active.id if state.active is not None else "",
            "active_session_id": state.active.session_id if state.active is not None else "",
            "active_run_background": state.active.background if state.active is not None else False,
        }

    @app.get("/api/activity/{session_id}/{after}")
    async def activity(
        session_id: Annotated[str, PathParameter(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")],
        after: Annotated[int, PathParameter(ge=0, le=2**53 - 1)],
    ) -> dict[str, object]:
        if session_id not in {session.id for session in await state.harness.list_sessions()}:
            raise HTTPException(404, "Session not found in this workspace.")
        active = state.active
        return state.wakeups.activity(
            session_id, after, active.id if active and active.session_id == session_id else ""
        )

    @app.get("/api/settings")
    async def settings() -> dict[str, object]:
        return state.settings.snapshot()

    @app.get("/api/models")
    async def models() -> dict[str, object]:
        provider = state.harness.agent.provider
        if state.harness.config.demo:
            raise HTTPException(501, "Model discovery is unavailable in offline demo mode. Enter a model ID manually.")
        try:
            model_ids = await provider.get_model_list()
            source = "codex" if isinstance(provider, CodexProvider) else provider.provider_type.value
        except NotImplementedError:
            raise HTTPException(
                501, "Model discovery is not supported by this connection. Enter a model ID manually."
            ) from None
        except Exception:
            raise HTTPException(
                502,
                "Model discovery failed. Check backend credentials and provider availability, or enter a model ID manually.",
            ) from None
        return {"models": model_ids, "source": source}

    @app.post("/api/settings")
    async def save_settings(body: SettingsInput) -> dict[str, object]:
        with state.idle():
            await state.settings.change(body.revision, body.values)
            return state.settings.snapshot()

    @app.post("/api/settings/reset")
    async def reset_settings(body: SettingsRevision) -> dict[str, object]:
        with state.idle():
            await state.settings.change(body.revision, state.settings.defaults, reset=True)
            return state.settings.snapshot()

    @app.get("/api/sessions")
    async def sessions() -> dict[str, object]:
        with state.idle():
            return await state.snapshot()

    @app.post("/api/sessions/new")
    async def new_session(body: Input) -> dict[str, object]:
        with state.idle():
            await state.harness.new_session()
            return await state.snapshot()

    @app.post("/api/sessions/resume")
    async def resume(body: SessionInput) -> dict[str, object]:
        with state.idle():
            if body.session_id not in {session.id for session in await state.harness.list_sessions()}:
                raise HTTPException(404, "Session not found in this workspace.")
            await state.harness.resume(body.session_id)
            return await state.snapshot()

    @app.post("/api/run")
    async def run(body: PromptInput) -> StreamingResponse:
        with state.idle():
            if body.session_id != state.harness.session_id:
                raise HTTPException(409, "The selected session changed. Reconnect before submitting.")
            if not body.prompt.strip():
                raise HTTPException(422, "Prompt must not be blank.")
            active = Run(state.harness.session_id)
            state.active = active
            active.task = asyncio.create_task(state.produce(active, body.prompt), name=f"ngn-web-{active.id}")
        return RunResponse(state, active)

    @app.post("/api/dictation/transcribe")
    async def transcribe(request: Request) -> dict[str, str]:
        # Admission, session/revision checks and the detached configuration
        # snapshot are atomic before the first await (and before any body read).
        with state.idle():
            if request.headers.getlist("x-ngn-session") != [state.harness.session_id]:
                raise HTTPException(409, "The selected session changed. Reconnect before recording again.")
            if request.headers.getlist("x-ngn-settings-revision") != [state.settings.revision]:
                raise HTTPException(409, "Settings changed. Reload settings before recording again.")
            config = state.settings.dictation_config()
            return await state.dictation.transcribe(request, config)

    @app.post("/api/cancel")
    async def cancel(body: RunInput) -> dict[str, str]:
        active = state.active
        if active is None or active.id != body.run_id:
            raise HTTPException(409, "This run is no longer active.")
        await state.stop(active)
        return {"status": "cancelled"}

    @app.post("/api/approval")
    async def approve(body: DecisionInput) -> dict[str, str]:
        active = state.active
        pending = active.pending if active is not None else None
        if (
            active is None
            or active.id != body.run_id
            or active.task.done()
            or active.task.cancelling()
            or pending is None
            or pending.id != body.approval_id
            or pending.call_id != body.call_id
            or pending.answer.done()
        ):
            raise HTTPException(409, "Approval is stale, unknown, or already decided.")
        pending.answer.set_result(body.decision == "allow")
        return {"status": body.decision}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(directory / "index.html")

    app.mount("/assets", StaticFiles(directory=directory / "assets", follow_symlink=False), name="assets")
    # No catch-all: unknown API routes and filesystem paths must stay 404.
    return app
