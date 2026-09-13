"""Same-origin HTTP/WS routes for the lifecycle-owned, single-Harness web service."""

import asyncio
import copy
import secrets
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal

import anyio
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from fastapi import WebSocket
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.staticfiles import StaticFiles

from nagents.channels.store import finish_on_cancel
from nagents.harness import Harness
from nagents.provider import CodexProvider

from . import built_assets
from . import local_authority
from .catalog import ChannelRevision
from .catalog import ConnectionInput
from .security import SECURITY_HEADERS
from .security import LocalOnly
from .service import Run as Run
from .service import RunResponse
from .service import WebState as WebState
from .settings import SettingsInput
from .settings import SettingsRevision
from .settings import WebSettings
from .settings import _join

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig

APPROVAL_TIMEOUT = 300
RootId = Annotated[str, PathParameter(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")]
ChannelId = Annotated[str, PathParameter(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInput(Input):
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")


class PromptInput(SessionInput):
    prompt: str = Field(min_length=1, max_length=32000)


class MessageInput(PromptInput):
    message_id: str = Field(pattern=r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")


class RunInput(Input):
    run_id: str = Field(min_length=1, max_length=80)


class DecisionInput(RunInput):
    approval_id: str = Field(min_length=1, max_length=80)
    call_id: str = Field(min_length=1, max_length=200)
    decision: Literal["allow", "deny"]


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
        state.approval_timeout = lambda: APPROVAL_TIMEOUT
        app.state.web = state
        try:
            await harness.initialize()
            await state.history.initialize()
            harness.agent.plugins.append(state.history.identity)
            state.settings = WebSettings(harness)
            await state.settings.load()
            if resume_session:
                await harness.resume(resume_session)
            elif continue_session:
                sessions = await harness.list_sessions()
                if sessions:
                    await harness.resume(sessions[0].id)
            state.selected_session_id = harness.session_id
            await state.channels.start()
            state.wakeups.start()
            yield
        finally:
            state.channels.closed = True
            state.wakeups.shutdown()

            async def close_resources() -> None:
                try:
                    if state.active is not None:
                        await state.stop(state.active)
                finally:
                    try:
                        await state.channels.close()
                    finally:
                        try:
                            await state.wakeups.close()
                        finally:
                            try:
                                await state.dictation.close()
                            finally:
                                harness.agent.plugins[:] = [
                                    plugin for plugin in harness.agent.plugins if plugin is not state.history.identity
                                ]
                                await harness.close()

            cleanup = asyncio.create_task(close_resources())
            try:
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

    @app.websocket("/api/events")
    async def events(socket: WebSocket) -> None:
        await state.bus.serve(socket, state.snapshot, state.disconnected)

    @app.post("/api/messages")
    async def message(body: MessageInput) -> dict[str, str]:
        if not body.prompt.strip():
            raise HTTPException(422, "Prompt must not be blank.")
        session_id = await state.channels.store.web(body.session_id, body.message_id, body.prompt)
        state.channels.changed.set()
        return {"session_id": session_id, "message_id": body.message_id, "status": "queued"}

    @app.get("/api/channels")
    async def channels() -> dict[str, object]:
        return await state.channels.snapshot()

    @app.post("/api/channels/refresh")
    async def refresh_channels() -> dict[str, object]:
        with state.idle():
            state.channels.catalog.discover(descriptors=True)
            return await state.channels.snapshot()

    @app.put("/api/channels/{id}")
    async def save_channel(id: ChannelId, body: ConnectionInput) -> dict[str, object]:
        with state.idle():
            with anyio.CancelScope(shield=True):
                result = await finish_on_cancel(state.channels.save(id, body))
            return result

    @app.delete("/api/channels/{id}")
    async def delete_channel(id: ChannelId, body: ChannelRevision) -> dict[str, object]:
        with state.idle():
            with anyio.CancelScope(shield=True):
                result = await finish_on_cancel(state.channels.delete(id, body.revision))
            return result

    @app.get("/api/activity/{session_id}/{after}")
    async def activity(
        session_id: RootId, after: Annotated[int, PathParameter(ge=0, le=2**53 - 1)]
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
        if state.active is not None and state.active.server_owned:
            return await state.snapshot()
        with state.idle():
            return await state.snapshot()

    @app.get("/api/sessions/{session_id}")
    async def session_snapshot(session_id: RootId) -> dict[str, object]:
        return await state.snapshot(session_id)

    @app.post("/api/sessions/new")
    async def new_session(body: Input) -> dict[str, object]:
        with state.idle():
            await state.harness.new_session()
            state.selected_session_id = state.harness.session_id
            return await state.snapshot()

    @app.post("/api/sessions/resume")
    async def resume(body: SessionInput) -> dict[str, object]:
        # Selecting a UI root never switches the Harness underneath a producer.
        if state.active is not None and state.active.server_owned:
            result = await state.snapshot(body.session_id)
            state.selected_session_id = body.session_id
            return result
        with state.idle():
            if body.session_id not in {session.id for session in await state.harness.list_sessions()}:
                raise HTTPException(404, "Session not found in this workspace.")
            await state.harness.resume(body.session_id)
            state.selected_session_id = body.session_id
            return await state.snapshot()

    @app.post("/api/run")
    async def run(body: PromptInput) -> StreamingResponse:
        with state.idle():
            if body.session_id != state.selected_session_id:
                raise HTTPException(409, "The selected session changed. Reconnect before submitting.")
            if not body.prompt.strip():
                raise HTTPException(422, "Prompt must not be blank.")
            if state.harness.session_id != body.session_id:
                await state.harness.resume(body.session_id)
            active = Run(body.session_id)
            state.active = active
            state.publish(active, {"event": "run_started"})
            state.status()
            active.task = asyncio.create_task(state.produce(active, body.prompt), name=f"ngn-web-{active.id}")
        return RunResponse(state, active)

    @app.post("/api/dictation/transcribe")
    async def transcribe(request: Request) -> dict[str, str]:
        with state.idle():
            sessions = request.headers.getlist("x-ngn-session")
            # A recovered WebSocket editor can retain its verified root while
            # the legacy shared selection changes (for example after restart).
            # Transcription returns a draft only; it never changes that selection.
            if len(sessions) != 1 or (
                sessions[0] != state.selected_session_id and not state.bus.listening(sessions[0])
            ):
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
        if (active.server_owned or active.background) and not state.bus.listening(active.session_id):
            pending.answer.set_result(False)
            raise HTTPException(409, "A live subscriber to this session is required for approval.")
        pending.answer.set_result(body.decision == "allow")
        return {"status": body.decision}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(directory / "index.html")

    app.mount("/assets", StaticFiles(directory=directory / "assets", follow_symlink=False), name="assets")
    return app
