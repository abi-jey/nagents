"""Same-origin HTTP/WS routes for the lifecycle-owned, single-Harness web service."""

import asyncio
import copy
import logging
import secrets
import sqlite3
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
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

from nagents.agent import Agent
from nagents.harness import Harness
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import validate_request_timeout
from nagents.provider import OpenAIProvider
from nagents.provider._codex_catalog import catalog_logging

from . import built_assets
from . import empty_sessions
from . import is_loopback_host
from . import local_authority
from ._async import finish_on_cancel
from ._async import join_owned as _join
from .catalog import ChannelRevision
from .catalog import ConnectionInput
from .deletion import delete_session
from .designer import Designer
from .designer import register as register_designer
from .live import create_agent as create_live_agent
from .live import create_login_config
from .live import register as register_live
from .live_bridge import MainAgentBridge
from .live_context import LiveSeed
from .live_context import read_seed
from .live_context import with_summary
from .live_runtime import LiveService
from .live_settings import LiveSettings
from .live_summary import VoiceContextSummarizer
from .provider_login import ProviderLogin
from .provider_setup import provider_setup
from .routing import RoutingStore
from .security import SECURITY_HEADERS
from .security import LocalOnly
from .service import Run as Run
from .service import RunResponse
from .service import WebState as WebState
from .settings import SettingsInput
from .settings import SettingsRevision
from .settings import WebSettings
from .tool_settings import register as register_tool_settings

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig

    from .live_login import LoginVoiceConfig

APPROVAL_TIMEOUT = 300
logger = logging.getLogger("uvicorn.error")
RootId = Annotated[str, PathParameter(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")]
ChannelId = Annotated[str, PathParameter(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionInput(Input):
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")


class DeleteSessionInput(Input):
    permanent: bool = False


class TrashGenerationInput(Input):
    deletion_id: str = Field(min_length=1, max_length=128)


class TrashSettingsInput(Input):
    revision: str = Field(min_length=1, max_length=128)
    retention_days: int = Field(ge=1, le=365)


class PromptInput(SessionInput):
    prompt: str = Field(min_length=1, max_length=32000)


class MessageInput(SessionInput):
    prompt: str = Field(default="", max_length=32000)
    attachments: list[str] = Field(default_factory=list, max_length=3)
    message_id: str = Field(pattern=r"^[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$")
    command: Literal["", "compact"] = ""


class RunInput(Input):
    run_id: str = Field(min_length=1, max_length=80)


class DecisionInput(RunInput):
    approval_id: str = Field(min_length=1, max_length=80)
    call_id: str = Field(min_length=1, max_length=200)
    decision: Literal["allow", "allow_tool", "deny"]


class ProviderInput(Input):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: dict[str, object]


class ProviderRevision(Input):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class LoginCancel(Input):
    id: str = Field(min_length=1, max_length=128)


def _provider_profile(fields: dict[str, object]) -> ProviderProfile:
    if set(fields) - set(ProviderProfile.__dataclass_fields__) or "kind" not in fields:
        raise HTTPException(422, "Invalid provider connection fields")
    try:

        def text(name: str, default: str = "") -> str:
            value = fields.get(name, default)
            if not isinstance(value, str):
                raise ValueError("Invalid provider field")
            return value

        profile = ProviderProfile(
            kind=text("kind"),
            auth=text("auth", "api-key"),
            base_url=text("base_url"),
            api=text("api", "auto"),
            api_key_env=text("api_key_env"),
            api_version=text("api_version"),
            scope=text("scope", "https://ai.azure.com/.default"),
            request_timeout=validate_request_timeout(fields.get("request_timeout", 120.0)),
        )
        profile.validate()
        return profile
    except (TypeError, ValueError):
        raise HTTPException(422, "Invalid provider connection; check its fields and authentication mode") from None


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
    enforce_authority = is_loopback_host(host)
    directory = built_assets() if assets is None else assets
    token = secrets.token_urlsafe(32)
    state: WebState
    designer: Designer
    live: LiveService
    live_settings: LiveSettings
    provider_login: ProviderLogin

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal state, designer, live, live_settings, provider_login
        harness = harness_factory(copy.deepcopy(config))
        logger.info(
            "ngn serve starting: workspace=%s state_dir=%s demo=%s",
            harness.workspace,
            harness.config.data_dir,
            config.demo,
        )
        state = WebState(harness)
        voice_summaries = VoiceContextSummarizer()
        live_settings = LiveSettings(
            harness.agent.session.db_path,
            demo=config.demo,
            active=lambda: live.active_session_id,
            providers=harness.provider_store,
            auth=harness.openai_auth,
        )

        def voice_agent(voice: str) -> Agent:
            connection = live_settings.admitted()
            handler = (
                MainAgentBridge(
                    state,
                    live_settings.admitted_session(),
                    voice_session_id=live.active_session_id,
                    report=live.delegation_reporter(live.active_session_id),
                ).handle
                if connection.values.backend_mode == "assistant"
                else None
            )
            return create_live_agent(connection, voice, demo=live_settings.demo, client_handler=handler)

        def login_voice(voice: str) -> "LoginVoiceConfig | None":
            connection = live_settings.admitted()
            if connection.voice_auth != "chatgpt":
                return None
            handler = MainAgentBridge(
                state,
                live_settings.admitted_session(),
                voice_session_id=live.active_session_id,
                report=live.delegation_reporter(live.active_session_id),
            ).handle_native
            return create_login_config(connection, voice, handler)

        async def voice_context(identifier: str) -> LiveSeed:
            connection = live_settings.admitted()
            if connection.values.backend_mode != "assistant":
                return LiveSeed(
                    chat_session_id=live_settings.admitted_session() or state.selected_session_id,
                    notice="Hosted voice has no access to saved chat context.",
                )
            root = live_settings.admitted_session()
            active = state.active
            task_state = (
                "The main assistant already has an active run. Its final result is not yet confirmed."
                if active is not None and active.session_id == root
                else ""
            )
            seed = await read_seed(
                harness.agent.session.db_path, root, connection.values.context_mode, task_state=task_state
            )
            if seed.mode == "summary" and seed.summary_source:
                brief = await voice_summaries.prepare(
                    source=seed.summary_source,
                    fingerprint=seed.fingerprint,
                    session_id=root,
                    provider=harness.agent.provider,
                )
                seed = (
                    with_summary(seed, brief.text)
                    if brief.text
                    else replace(
                        seed,
                        notice="A fresh summary could not be prepared. Recent text and any existing summary were used instead.",
                    )
                )
            return seed

        live = LiveService(
            voice_agent,
            login_factory=login_voice,
            caption_factory=lambda identifier: state.bind_live_captions(
                live_settings.admitted_session() or state.selected_session_id, identifier
            ),
            context_factory=voice_context,
        )
        state.approval_timeout = lambda: APPROVAL_TIMEOUT
        app.state.web = state
        app.state.live = live
        app.state.live_settings = live_settings
        provider_login = ProviderLogin(state, lambda: live.active_session_id)
        app.state.provider_login = provider_login
        try:
            await harness.initialize(create_session=not (resume_session or continue_session))
            await live_settings.load()
            designer = Designer(state)
            await designer.traces.initialize()
            await state.history.initialize()
            state.tool_approvals.initialize()
            harness.agent.plugins.append(state.history.identity)
            state.settings = WebSettings(harness)
            await state.settings.load()
            registry = harness.provider_store.load()
            logger.info(
                "ngn serve config: workspace=%s config_files=%s global_providers=%s workspace_providers=%s active=%s providers=%s",
                harness.workspace,
                [str(path) for path in harness.config.config_paths],
                harness.provider_store.path,
                harness.provider_store.workspace_store.path,
                registry.active or "(legacy configuration)",
                {name: profile.kind for name, profile in registry.providers.items()},
            )
            logger.info(
                "ngn serve settings: session_db=%s workspace_settings_row=%s global_db=%s global_settings_row=%s "
                "workspace_provider_file=%s provider_selection=%s live_source=%s",
                harness.agent.session.db_path,
                state.settings.persisted,
                state.settings.global_path,
                state.settings.global_persisted,
                harness.provider_store.workspace_store.path,
                "workspace" if harness.provider_store.load_scope("workspace").active else "inherited global default",
                "provider connection" if registry.active else "workspace Live settings",
            )
            if resume_session:
                await harness.resume(resume_session)
            elif continue_session:
                await empty_sessions.continue_session(state)
            state.selected_session_id = harness.session_id
            await state.channels.start()
            state.wakeups.start()
            await state.trash.start()
            # Startup inbox recovery may already own a mutation. Draft cleanup
            # can wait for the next idle listing; it must not abort startup.
            if not state.mutating:
                with state.idle(allow_running=True):
                    await empty_sessions.prune(state, live.active_session_id)
            logger.info(
                "ngn serve ready: session=%s provider=%s model=%s",
                harness.session_id,
                harness.config.provider or harness.config.provider_profile().kind,
                harness.agent.provider.model,
            )
            yield
        finally:
            logger.info("ngn serve stopping: workspace=%s", harness.workspace)
            state.channels.closed = True
            state.channels.management.shutdown()
            state.wakeups.shutdown()
            state.trash.shutdown()

            async def close_host() -> None:
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
                            harness.agent.plugins[:] = [
                                plugin for plugin in harness.agent.plugins if plugin is not state.history.identity
                            ]
                            await harness.close()

            async def close_resources() -> None:
                try:
                    await provider_login.close()
                finally:
                    try:
                        await live_settings.shutdown()
                    finally:
                        try:
                            await live.shutdown()
                        finally:
                            try:
                                await state.trash.close()
                            finally:
                                await close_host()

            cleanup = asyncio.create_task(close_resources())
            try:
                await asyncio.wait({cleanup})
            finally:
                await _join(cleanup)
                logger.info("ngn serve stopped: workspace=%s", harness.workspace)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnly, authority=authority, token=token, enforce_authority=enforce_authority)
    register_designer(app, lambda: designer)
    register_tool_settings(app, lambda: state)
    register_live(app, lambda: live, lambda: live_settings, lambda: state)
    from .local_delivery import register_assets

    register_assets(app, lambda: state)
    from .uploads import register_uploads

    register_uploads(app, lambda: state)

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
            "provider": state.harness.config.provider_profile().kind,
            "model": state.harness.config.profile(state.settings.values.agent).model or state.settings.values.model,
            "agent": state.settings.values.agent,
            "demo": state.harness.config.demo,
            "provider_setup": provider_setup(state.harness),
            "active_run_id": state.active.id if state.active is not None else "",
            "active_session_id": state.active.session_id if state.active is not None else "",
            "active_run_background": state.active.background if state.active is not None else False,
        }

    @app.websocket("/api/events")
    async def events(socket: WebSocket) -> None:
        await state.bus.serve(socket, state.snapshot, state.disconnected)

    @app.post("/api/messages")
    async def message(body: MessageInput) -> dict[str, str]:
        if not config.demo and not state.harness.config.provider:
            raise HTTPException(409, "Select a named provider connection before sending a message.")
        if not body.prompt.strip() and not body.attachments:
            raise HTTPException(422, "Prompt must not be blank.")
        if body.command and (body.prompt != "/compact" or body.attachments):
            raise HTTPException(422, "The compact command accepts no arguments or attachments.")

        async def accept() -> dict[str, str]:
            if body.command:
                session_id, admitted = await state.channels.store.web(
                    body.session_id, body.message_id, body.prompt, command=body.command
                )
            elif body.attachments:
                media = await state.uploads.capabilities(body.session_id)
                session_id, admitted = await state.channels.store.web(
                    body.session_id, body.message_id, body.prompt, tuple(body.attachments), media
                )
            else:
                session_id, admitted = await state.channels.store.web(body.session_id, body.message_id, body.prompt)
            active = state.active
            if (
                admitted
                and state.harness.config.submit_mode == "interrupt"
                and active is not None
                and not active.voice
                and active.session_id == session_id
                and active.message_id != body.message_id
                and not active.finished
                and not active.task.done()
            ):
                await state.stop(active)
            state.channels.changed.set()
            return {"session_id": session_id, "message_id": body.message_id, "status": "queued"}

        # Admission and the selected policy complete even if the HTTP caller
        # disconnects after commit. A retry only observes the existing message.
        result = await finish_on_cancel(accept())
        logger.info(
            "Message queued: session=%s message=%s chars=%d attachments=%d",
            result["session_id"],
            body.message_id,
            len(body.prompt),
            len(body.attachments),
        )
        return result

    @app.get("/api/channels")
    async def channels() -> dict[str, object]:
        return await state.channels.snapshot()

    @app.post("/api/channels/refresh")
    async def refresh_channels() -> dict[str, object]:
        with state.idle():
            state.channels.catalog.discover(descriptors=True)
            result = await state.channels.snapshot()
            logger.info("Channel plugins refreshed: count=%d", len(state.channels.catalog.plugins))
            return result

    @app.put("/api/channels/{id}")
    async def save_channel(id: ChannelId, body: ConnectionInput) -> dict[str, object]:
        with state.idle():
            with anyio.CancelScope(shield=True):
                result = await finish_on_cancel(state.channels.save(id, body))
            logger.info("Channel connection saved: id=%s plugin=%s enabled=%s", id, body.plugin, body.enabled)
            return result

    @app.delete("/api/channels/{id}")
    async def delete_channel(id: ChannelId, body: ChannelRevision) -> dict[str, object]:
        with state.idle():
            with anyio.CancelScope(shield=True):
                result = await finish_on_cancel(state.channels.delete(id, body.revision))
            logger.info("Channel connection removed: id=%s", id)
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
        logger.info(
            "Model catalog requested: active=%s provider=%s",
            state.harness.config.provider or "(unconfigured)",
            state.harness.config.provider_profile().kind,
        )
        if state.harness.config.demo:
            raise HTTPException(501, "Model discovery is unavailable in offline demo mode. Enter a model ID manually.")
        try:
            with catalog_logging(logger):
                model_ids = await provider.get_model_list()
            source = (
                "codex"
                if isinstance(provider, OpenAIProvider) and provider.uses_chatgpt_auth
                else provider.provider_type.value
            )
        except NotImplementedError:
            logger.info(
                "Model catalog unsupported: provider=%s",
                state.harness.config.provider or state.harness.config.provider_profile().kind,
            )
            raise HTTPException(
                501, "Model discovery is not supported by this connection. Enter a model ID manually."
            ) from None
        except Exception:
            logger.warning(
                "Model catalog failed: provider=%s",
                state.harness.config.provider or state.harness.config.provider_profile().kind,
            )
            raise HTTPException(
                502,
                "Model discovery failed. Check backend credentials and provider availability, or enter a model ID manually.",
            ) from None
        logger.info("Model catalog completed: source=%s count=%d", source, len(model_ids))
        return {"models": model_ids, "source": source}

    @app.get("/api/login/chatgpt")
    async def login_status() -> dict[str, object]:
        return dict(provider_login.snapshot())

    @app.post("/api/login/chatgpt")
    async def login_start(body: Input) -> dict[str, object]:
        return dict(await provider_login.start())

    @app.post("/api/login/chatgpt/cancel")
    async def login_cancel(body: LoginCancel) -> dict[str, object]:
        return dict(await provider_login.cancel(body.id))

    @app.get("/api/providers")
    async def workspace_providers() -> dict[str, object]:
        return state.harness.provider_store.snapshot("workspace")

    @app.get("/api/provider-scopes/{scope}/providers")
    async def providers(scope: Literal["workspace", "global"]) -> dict[str, object]:
        return state.harness.provider_store.snapshot(scope)

    @app.put("/api/provider-scopes/{scope}/providers/{name}")
    async def save_provider(
        scope: Literal["workspace", "global"],
        name: Annotated[str, PathParameter(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        body: ProviderInput,
    ) -> dict[str, object]:
        with state.idle():
            if live.active_session_id:
                raise HTTPException(409, "End the active Live call before editing providers.")
            profile = _provider_profile(body.profile)
            try:
                await _join(asyncio.create_task(state.harness.save_provider(name, profile, body.revision, scope)))
            except ValueError:
                raise HTTPException(
                    409, "Provider connection changed or could not be applied. Reload providers."
                ) from None
            state.settings.sync_provider()
            logger.info("Provider connection saved: scope=%s name=%s kind=%s", scope, name, profile.kind)
            return state.harness.provider_store.snapshot(scope)

    @app.delete("/api/provider-scopes/{scope}/providers/{name}")
    async def delete_provider(
        scope: Literal["workspace", "global"],
        name: Annotated[str, PathParameter(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        body: ProviderRevision,
    ) -> dict[str, object]:
        with state.idle():
            if live.active_session_id:
                raise HTTPException(409, "End the active Live call before editing providers.")
            try:
                await _join(asyncio.create_task(state.harness.delete_provider(name, body.revision, scope)))
            except ValueError:
                raise HTTPException(409, "Provider is active, bound to an agent, or configuration changed.") from None
            logger.info("Provider connection deleted: scope=%s name=%s", scope, name)
            return state.harness.provider_store.snapshot(scope)

    @app.post("/api/provider-scopes/{scope}/providers/{name}/activate")
    async def activate_provider(
        scope: Literal["workspace", "global"],
        name: Annotated[str, PathParameter(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        body: ProviderRevision,
    ) -> dict[str, object]:
        with state.idle():
            if live.active_session_id:
                raise HTTPException(409, "End the active Live call before switching providers.")
            try:
                await _join(asyncio.create_task(state.harness.activate_provider(name, body.revision, scope)))
            except ValueError:
                raise HTTPException(
                    409, "Provider connection changed or could not be selected. Reload providers."
                ) from None
            state.settings.sync_provider()
            logger.info("Provider connection activated: scope=%s name=%s", scope, name)
            return state.harness.provider_store.snapshot(scope)

    @app.post("/api/providers/inherit")
    async def inherit_provider(body: ProviderRevision) -> dict[str, object]:
        with state.idle():
            if live.active_session_id:
                raise HTTPException(409, "End the active Live call before switching providers.")
            try:
                await _join(asyncio.create_task(state.harness.inherit_provider(body.revision)))
            except ValueError:
                raise HTTPException(409, "Provider selection changed. Reload providers.") from None
            state.settings.sync_provider()
            logger.info("Workspace provider selection reset to global default")
            return state.harness.provider_store.snapshot("workspace")

    @app.get("/api/providers/{name}/models")
    async def provider_models(
        name: Annotated[str, PathParameter(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
    ) -> dict[str, object]:
        logger.info("Model catalog requested: connection=%s", name)
        if state.harness.config.demo:
            raise HTTPException(501, "Model discovery is unavailable in offline demo mode.")
        try:
            with catalog_logging(logger):
                models = await state.harness.provider_models(name)
        except NotImplementedError:
            logger.info("Model catalog unsupported: connection=%s", name)
            raise HTTPException(
                501, "This provider does not expose a model catalog; enter a model ID manually."
            ) from None
        except ValueError:
            logger.warning("Model catalog unavailable: connection=%s", name)
            raise HTTPException(422, "Provider connection is unavailable; check its environment and login.") from None
        except Exception:
            logger.warning("Model catalog failed: connection=%s", name)
            raise HTTPException(
                502, "Model discovery failed; check provider access or enter a model ID manually."
            ) from None
        logger.info("Model catalog completed: connection=%s count=%d", name, len(models))
        return {"models": models, "source": name}

    @app.post("/api/settings")
    async def save_settings(body: SettingsInput) -> dict[str, object]:
        with state.idle():
            await state.settings.change(
                body.revision,
                body.values,
                api_key=body.api_key,
                clear_api_key=body.clear_api_key,
            )
            logger.info(
                "Workspace settings saved: provider=%s agent=%s model=%s",
                state.harness.config.provider or state.harness.config.provider_profile().kind,
                state.harness.config.agent,
                state.harness.config.model,
            )
            return state.settings.snapshot()

    @app.post("/api/settings/reset")
    async def reset_settings(body: SettingsRevision) -> dict[str, object]:
        with state.idle():
            await state.settings.load_global()
            await state.settings.change(body.revision, state.settings.defaults, reset=True)
            logger.info("Workspace settings reset to inherited defaults")
            return state.settings.snapshot()

    @app.get("/api/settings/global")
    async def global_settings() -> dict[str, object]:
        await state.settings.load_global()
        return state.settings.global_snapshot()

    @app.post("/api/settings/global")
    async def save_global_settings(body: SettingsInput) -> dict[str, object]:
        if body.api_key or body.clear_api_key:
            raise HTTPException(
                422, "Global settings use credential references. Store key values in workspace settings."
            )
        with state.idle():
            try:
                await finish_on_cancel(state.settings.change_global(body.revision, body.values))
            except ValueError:
                raise HTTPException(422, "Invalid global settings.") from None
            logger.info("Global settings saved: path=%s", state.settings.global_path)
            return state.settings.global_snapshot()

    @app.post("/api/settings/global/reset")
    async def reset_global_settings(body: SettingsRevision) -> dict[str, object]:
        with state.idle():
            await finish_on_cancel(
                state.settings.change_global(body.revision, state.settings.startup_defaults, reset=True)
            )
            logger.info("Global settings reset: path=%s", state.settings.global_path)
            return state.settings.global_snapshot()

    @app.get("/api/sessions")
    async def sessions() -> dict[str, object]:
        if state.active is not None and state.active.server_owned:
            return await state.snapshot()
        with state.idle():
            await empty_sessions.prune(state, live.active_session_id)
            return await state.snapshot()

    @app.get("/api/sessions/{session_id}")
    async def session_snapshot(session_id: RootId) -> dict[str, object]:
        return await state.snapshot(session_id)

    @app.get("/api/sessions/{session_id}/context")
    async def session_context(session_id: RootId) -> dict[str, object]:
        return await state.context_stats(session_id)

    @app.delete("/api/sessions/{session_id}")
    async def remove_session(session_id: RootId, body: DeleteSessionInput) -> dict[str, object]:
        result = await delete_session(state, session_id, permanent=body.permanent)
        logger.info("Session deleted: id=%s permanent=%s", session_id, body.permanent)
        return result

    @app.get("/api/trash")
    async def trash() -> dict[str, object]:
        return await state.trash.snapshot()

    @app.put("/api/trash/settings")
    async def trash_settings(body: TrashSettingsInput) -> dict[str, object]:
        return await state.trash.change(body.revision, body.retention_days)

    @app.post("/api/trash/{session_id}/restore")
    async def restore_session(session_id: RootId, body: TrashGenerationInput) -> dict[str, object]:
        return await state.trash.restore(session_id, body.deletion_id)

    @app.delete("/api/trash/{session_id}")
    async def purge_session(session_id: RootId, body: TrashGenerationInput) -> dict[str, object]:
        return await state.trash.purge(session_id, body.deletion_id)

    @app.post("/api/sessions/new")
    async def new_session(body: Input) -> dict[str, object]:
        with state.idle():
            await empty_sessions.new_session(state, live.active_session_id)
            logger.info("Session ready: id=%s", state.harness.session_id)
            return await state.snapshot()

    @app.post("/api/sessions/resume")
    async def resume(body: SessionInput) -> dict[str, object]:
        # Selecting a UI root never switches the Harness underneath a producer.
        if state.active is not None and state.active.server_owned:
            result = await state.snapshot(body.session_id)
            state.selected_session_id = body.session_id
            logger.info("Session selected: id=%s", body.session_id)
            return result
        with state.idle():
            if body.session_id not in {session.id for session in await state.harness.list_sessions()}:
                raise HTTPException(404, "Session not found in this workspace.")
            await state.harness.resume(body.session_id)
            state.selected_session_id = body.session_id
            await empty_sessions.prune(state, live.active_session_id)
            return await state.snapshot()

    @app.post("/api/run")
    async def run(body: PromptInput) -> StreamingResponse:
        if not config.demo and not state.harness.config.provider:
            raise HTTPException(409, "Select a named provider connection before starting a run.")
        with state.idle():
            await state.channels.store._transaction(lambda db: RoutingStore.execution_root(db, body.session_id))
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
            active.task = asyncio.create_task(state.produce_session(active, body.prompt), name=f"ngn-web-{active.id}")
            logger.info("Run started: session=%s run=%s chars=%d", body.session_id, active.id, len(body.prompt))
        return RunResponse(state, active)

    @app.post("/api/cancel")
    async def cancel(body: RunInput) -> dict[str, str]:
        active = state.active
        if active is None or active.id != body.run_id:
            raise HTTPException(409, "This run is no longer active.")
        await state.stop(active)
        logger.info("Run cancelled: run=%s", body.run_id)
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
        if body.decision == "allow_tool":
            if pending.binding is None or pending.request is None or pending.harness is None:
                raise HTTPException(409, "This tool cannot be remembered. Allow this call once instead.")
            # Re-resolve the registered definition. A replacement must receive
            # its own approval and must never inherit the pending tool grant.
            if state.tool_approvals.requested_binding(pending.harness, pending.request) != pending.binding:
                raise HTTPException(409, "Tool definition or permissions changed. Deny this call and retry.")
            try:
                state.tool_approvals.grant(pending.binding)
            except sqlite3.Error:
                raise HTTPException(
                    503, "Could not save this tool permission. Nothing was approved; retry or allow once."
                ) from None
        pending.decision = body.decision
        pending.answer.set_result(body.decision in {"allow", "allow_tool"})
        logger.info("Approval decided: run=%s approval=%s decision=%s", body.run_id, body.approval_id, body.decision)
        return {"status": body.decision}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(directory / "index.html")

    app.mount("/assets", StaticFiles(directory=directory / "assets", follow_symlink=False), name="assets")
    return app
