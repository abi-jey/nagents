"""GPT-Live voice configuration and same-origin HTTP routes."""

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from fastapi import Query
from fastapi import WebSocket
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.agent import Agent
from nagents.harness.connection import build_live_provider
from nagents.live import LiveConfig
from nagents.live.delegation import ClientDelegationHandler
from nagents.live.delegation import ClientDelegationObserver
from nagents.live.delegation import ClientDelegationRequest
from nagents.session import SessionManager

from .live_login import LoginVoiceConfig
from .live_settings import AgentVoiceInput
from .live_settings import AgentVoiceRevision
from .live_settings import GlobalVoiceInput
from .live_settings import Revision
from .live_settings import WorkspaceVoiceInput
from .routing import RoutingStore
from .voice_instructions import legacy_voice_instructions
from .voice_preferences import Scope

if TYPE_CHECKING:
    from .live_runtime import LiveService
    from .live_settings import LiveConnection
    from .live_settings import LiveSettings
    from .service import WebState

logger = logging.getLogger("uvicorn.error")
SessionId = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Cursor = Annotated[int, Query(ge=0, le=2**53 - 1)]


def voice_instructions(connection: "LiveConnection") -> str:
    if connection.instruction_prompt_captured:
        return connection.instruction_prompt
    return legacy_voice_instructions(connection.values.instructions, connection.values.backend_mode)


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    voice: str = Field(default="", max_length=64)
    revision: Revision
    session_id: str = Field(default="", max_length=80, pattern=r"^$|^ngn-[a-zA-Z0-9-]+$")


def unavailable_reason(connection: "LiveConnection", *, demo: bool) -> str:
    """Local readiness only: never probe the provider, read a login, or create an agent."""
    values = connection.values
    if demo:
        return "GPT-Live is unavailable in offline demo mode. Restart ngn serve without --demo."
    if connection.profile is None:
        return "Choose a provider connection in Voice settings."
    if connection.profile is not None and connection.profile.kind not in {
        "openai",
        "openai_compatible",
        "foundry",
        "azure_openai_compatible_v1",
    }:
        return "The selected voice connection does not support GPT-Live. Choose OpenAI or Foundry in Voice settings."
    if not values.enabled:
        return "GPT-Live is disabled. Enable it in Voice settings."
    if connection.voice_auth == "chatgpt" and values.backend_mode != "assistant":
        return "ChatGPT voice uses your main assistant. Choose Main assistant in Voice settings."
    if values.provider == "azure_openai_compatible_v1" and not values.base_url:
        return "The Azure v1 Live provider requires an API base URL. Open Provider connections to set it."
    if not connection.key_configured:
        if connection.voice_auth == "chatgpt":
            return "Sign in to the selected ChatGPT or Codex connection, then refresh Voice settings."
        if connection.profile is not None:
            return (
                f"Set ${connection.profile.key_env} in the ngn serve environment, then restart the server. "
                "Or choose a ChatGPT/Codex voice connection in Voice settings."
            )
        return "Open Global or Workspace settings → Provider connections to configure Live credentials."
    return ""


def capabilities(
    connection: "LiveConnection",
    active_session_id: str = "",
    *,
    demo: bool = False,
    assistant: dict[str, str] | None = None,
) -> dict[str, object]:
    """Allowlisted discovery fields without credentials; editable values have their own route."""
    reason = unavailable_reason(connection, demo=demo)
    values = connection.values
    return {
        "available": not reason,
        "reason": reason,
        "provider": values.provider,
        "model": connection.voice_model,
        "backend_model": values.backend_model,
        "backend_mode": values.backend_mode,
        "context_mode": values.context_mode,
        "assistant": assistant or {},
        "voice": values.voice,
        "voices": list(connection.voices),
        "active_session_id": active_session_id,
        "revision": connection.revision,
        "enabled": values.enabled,
        "key_configured": connection.key_configured,
        "voice_auth": connection.voice_auth,
        "transport": "websocket",
    }


def create_agent(
    connection: "LiveConnection",
    voice: str = "",
    *,
    demo: bool = False,
    client_request_handler: ClientDelegationHandler | None = None,
    client_request_observer: ClientDelegationObserver | None = None,
) -> Agent:
    """A fresh voice agent; client mode delegates inference to the selected Harness."""
    reason = unavailable_reason(connection, demo=demo)
    if reason:
        raise HTTPException(503, reason)
    if (voice or connection.values.voice) not in connection.voices:
        raise HTTPException(422, "Choose a supported Live voice.")
    values = connection.values
    if values.backend_mode == "assistant" and client_request_handler is None:
        raise HTTPException(503, "Select an assistant conversation before connecting GPT-Live.")
    options = LiveConfig(
        delegation="client" if values.backend_mode == "assistant" else "responses",
        backend_model=values.backend_model,
        voice=voice or values.voice,
        store=False,
        backend_timeout=420 if values.backend_mode == "assistant" else 120,
        client_request_handler=client_request_handler if values.backend_mode == "assistant" else None,
        client_request_observer=client_request_observer if values.backend_mode == "assistant" else None,
    )
    key = connection.api_key.get_secret_value()
    assert connection.profile is not None
    provider = build_live_provider(connection.profile, options, key, values.model)
    # Hosted Live never runs the text-session database. Even accidental use cannot
    # touch the Harness history or create a voice transcript on disk.
    return Agent(
        provider,
        SessionManager(Path(":memory:")),
        system_prompt=voice_instructions(connection),
        tools=[],
        compaction_strategy=None,
        save_tool_outputs=False,
    )


def create_login_config(
    connection: "LiveConnection",
    voice: str,
    handler: Callable[[ClientDelegationRequest], Awaitable[str]],
    *,
    observer: ClientDelegationObserver | None = None,
) -> LoginVoiceConfig:
    reason = unavailable_reason(connection, demo=False)
    if reason:
        raise HTTPException(503, reason)
    if connection.login_credentials is None:
        raise HTTPException(503, "Sign in to the selected ChatGPT or Codex connection first.")
    if (voice or connection.values.voice) not in connection.voices:
        raise HTTPException(422, "Choose a supported ChatGPT voice.")
    return LoginVoiceConfig(
        credentials=connection.login_credentials,
        model=connection.voice_model,
        voice=voice or connection.values.voice,
        instructions=voice_instructions(connection),
        handler=handler,
        observer=observer,
    )


def register(
    app: FastAPI,
    service: Callable[[], "LiveService"],
    settings: Callable[[], "LiveSettings"],
    state: Callable[[], "WebState"],
) -> None:
    def assistant() -> dict[str, str]:
        host = state()
        current = host.session_agents.target(host.selected_session_id)
        return {"provider": current["provider_kind"], "model": current["model"], "agent": current["agent"]}

    @app.get("/api/live")
    async def discover() -> dict[str, object]:
        current = settings()
        return capabilities(
            await current.connection(), service().active_session_id, demo=current.demo, assistant=assistant()
        )

    @app.get("/api/live/settings")
    async def connection_settings(scope: Scope | Literal["agent"], agent: str = "") -> dict[str, object]:
        return await settings().snapshot(scope, agent)

    @app.post("/api/live/settings")
    async def save_settings(body: GlobalVoiceInput | WorkspaceVoiceInput | AgentVoiceInput) -> dict[str, object]:
        result = (
            await settings().change_agent(body) if isinstance(body, AgentVoiceInput) else await settings().change(body)
        )
        logger.info(
            "Live settings saved: scope=%s source=%s",
            result["scope"],
            result["source"],
        )
        return result

    @app.delete("/api/live/settings")
    async def restore_agent_settings(body: AgentVoiceRevision) -> dict[str, object]:
        return await settings().change_agent(body)

    @app.post("/api/live/sessions", status_code=201)
    async def create(body: SessionInput) -> dict[str, object]:
        current = settings()
        host = state()
        # Voice can join an already-working assistant. Reserve configuration and
        # selection while provisioning without cancelling or replacing its run.
        with host.idle(allow_running=True):
            async with current.admit(body.revision, body.session_id) as connection:
                if body.voice and body.voice not in connection.voices:
                    raise HTTPException(422, "Choose a supported Live voice.")
                reason = unavailable_reason(connection, demo=current.demo)
                if reason:
                    raise HTTPException(503, reason)
                logger.info(
                    "Live connection requested: provider=%s profile=%s voice=%s",
                    connection.values.provider,
                    connection.profile_name or "(unconfigured)",
                    body.voice or connection.values.voice,
                )
                if connection.values.backend_mode == "assistant":
                    if not body.session_id:
                        raise HTTPException(422, "Select an assistant conversation before connecting GPT-Live.")
                    if body.session_id != host.selected_session_id:
                        raise HTTPException(
                            409, "The selected assistant conversation changed. Reconnect before using voice."
                        )
                    await host.channels.store._transaction(lambda db: RoutingStore.execution_root(db, body.session_id))
                result = await service().create_stream(body.voice)
                logger.info("Live connection created: session=%s", result.get("session_id", ""))
                return result

    @app.websocket("/api/live/sessions/{session_id}/audio")
    async def audio(socket: WebSocket, session_id: SessionId) -> None:
        await service().serve_audio(session_id, socket)

    @app.get("/api/live/sessions/{session_id}")
    async def snapshot(session_id: SessionId, after: Cursor = 0) -> dict[str, object]:
        return await service().snapshot(session_id, after)

    @app.get("/api/live/sessions/{session_id}/delegation-details")
    async def delegation_details_query(
        session_id: SessionId, delegation_id: Annotated[str, Query(min_length=1, max_length=256)]
    ) -> dict[str, object]:
        if not delegation_id.strip():
            raise HTTPException(422, "A nonblank delegation ID is required.")
        return await service().delegation_details(session_id, delegation_id)

    @app.get("/api/live/sessions/{session_id}/context")
    async def context_details(session_id: SessionId) -> dict[str, object]:
        return await service().context_details(session_id)

    @app.post("/api/live/sessions/{session_id}/close")
    async def close(session_id: SessionId) -> dict[str, object]:
        result = await service().close(session_id)
        logger.info("Live connection closed: session=%s", session_id)
        return result
