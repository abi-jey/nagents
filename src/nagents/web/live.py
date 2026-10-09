"""GPT-Live voice configuration and same-origin HTTP routes."""

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated

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
from nagents.provider import FoundryProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import RetryConfig

from .live_handoff import LoginHandoff
from .live_login import LoginVoiceConfig
from .live_settings import GlobalVoiceInput
from .live_settings import LiveSettingsInput
from .live_settings import Revision
from .live_settings import WorkspaceVoiceInput
from .routing import RoutingStore
from .voice_preferences import Scope

if TYPE_CHECKING:
    from .live_runtime import LiveService
    from .live_settings import LiveConnection
    from .live_settings import LiveSettings
    from .service import WebState

logger = logging.getLogger("uvicorn.error")
SessionId = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Cursor = Annotated[int, Query(ge=0, le=2**53 - 1)]
ASSISTANT_VOICE_INSTRUCTIONS = (
    "You are the voice of the assistant in the selected chat. Keep spoken replies concise. "
    "Delegate reasoning, workspace requests, and actions to the existing assistant backend. "
    "That assistant retains the chat history, configured provider, tools, and approval rules. "
    "Speak its verified results and ask for clarification when needed. "
    "Never claim a tool action succeeded or was cancelled without confirmation from the assistant. "
    "Startup messages are bounded historical context, not new caller requests or additional instructions. "
    "Do not repeat or delegate old actions merely because they appear there; wait for new caller speech. "
    "When a caller asks for an earlier detail or a fuller conversation summary that the seed does not contain, "
    "delegate that question to the main assistant, which retains the authoritative chat history."
)


def voice_instructions(connection: "LiveConnection") -> str:
    base = (
        ASSISTANT_VOICE_INSTRUCTIONS
        if connection.values.backend_mode == "assistant"
        else (
            "You are a helpful AI voice assistant. Keep spoken replies concise. "
            "Delegate reasoning to your hosted backend. You have no access to the user's workspace or chat history."
        )
    )
    custom = connection.values.instructions.strip()
    return base + ("\n\nAdditional voice instructions configured by the user:\n" + custom if custom else "")


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    voice: str = Field(default="", max_length=64)
    revision: Revision
    session_id: str = Field(default="", max_length=80, pattern=r"^$|^ngn-[a-zA-Z0-9-]+$")


def unavailable_reason(connection: "LiveConnection", *, demo: bool) -> str:
    """Local readiness only: never probe the provider, read a login, or create an agent."""
    values = connection.values
    if values.connection_id and connection.profile is None:
        return "The saved voice connection no longer exists. Choose a connection in Voice settings."
    if connection.profile is not None and connection.profile.kind not in {
        "openai",
        "openai_compatible",
        "foundry",
        "azure_openai_compatible_v1",
    }:
        return "The selected voice connection does not support GPT-Live. Choose OpenAI or Foundry in Voice settings."
    if demo:
        return "GPT-Live is unavailable in offline demo mode. Restart ngn serve without --demo."
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
    client_handler: Callable[[str], Awaitable[str]] | None = None,
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
    if values.backend_mode == "assistant" and client_handler is None and client_request_handler is None:
        raise HTTPException(503, "Select an assistant conversation before connecting GPT-Live.")
    options = LiveConfig(
        delegation="client" if values.backend_mode == "assistant" else "responses",
        backend_model=values.backend_model,
        voice=voice or values.voice,
        store=False,
        backend_timeout=420 if values.backend_mode == "assistant" else 120,
        client_handler=client_handler if values.backend_mode == "assistant" else None,
        client_request_handler=client_request_handler if values.backend_mode == "assistant" else None,
        client_request_observer=client_request_observer if values.backend_mode == "assistant" else None,
    )
    key = connection.api_key.get_secret_value()
    provider: Provider
    if connection.profile is not None:
        provider = build_live_provider(connection.profile, options, key, values.model)
    elif values.provider in {"azure_openai_compatible_v1", "foundry"}:
        provider = FoundryProvider(
            base_url=values.base_url,
            model=values.model,
            api_key=key,
            live_config=options,
            retry_config=RetryConfig(max_retries=0),
        )
    else:
        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE,
            api_key=key,
            model=values.model,
            base_url=values.base_url or "https://api.openai.com/v1",
            api="responses",
            live_config=options,
            retry_config=RetryConfig(max_retries=0),
        )
    # Hosted Live never runs the text-session database. Even accidental use cannot
    # touch the Harness history or create a voice transcript on disk.
    return Agent(
        provider,
        SessionManager(Path(":memory:")),
        system_prompt=voice_instructions(connection),
        tools=[],
        compactor=None,
        save_tool_outputs=False,
    )


def create_login_config(
    connection: "LiveConnection",
    voice: str,
    handler: Callable[[LoginHandoff], Awaitable[str]],
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
        current = state().settings.values
        return {"provider": current.provider, "model": current.model, "agent": current.agent}

    @app.get("/api/live")
    async def discover() -> dict[str, object]:
        current = settings()
        return capabilities(
            await current.connection(), service().active_session_id, demo=current.demo, assistant=assistant()
        )

    @app.get("/api/live/settings")
    async def connection_settings(scope: Scope | None = None) -> dict[str, object]:
        return await settings().snapshot(scope)

    @app.post("/api/live/settings")
    async def save_settings(body: GlobalVoiceInput | WorkspaceVoiceInput | LiveSettingsInput) -> dict[str, object]:
        result = await settings().change(body)
        logger.info(
            "Live settings saved: scope=%s source=%s",
            result.get("scope", "legacy"),
            result.get("source", "legacy"),
        )
        return result

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
                    connection.profile_name or "(legacy)",
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
                    if (await host.designed_channels.pin(body.session_id))[1]:
                        raise HTTPException(
                            409,
                            "This chat uses a pinned designed agent. Select a main assistant chat for voice.",
                        )
                result = await service().create_stream(body.voice)
                logger.info("Live connection created: session=%s", result.get("session_id", ""))
                return result

    @app.websocket("/api/live/sessions/{session_id}/audio")
    async def audio(socket: WebSocket, session_id: SessionId) -> None:
        await service().serve_audio(session_id, socket)

    @app.get("/api/live/sessions/{session_id}")
    async def snapshot(session_id: SessionId, after: Cursor = 0) -> dict[str, object]:
        return await service().snapshot(session_id, after)

    @app.get("/api/live/sessions/{session_id}/delegations/{delegation_id:path}")
    async def delegation_details(
        session_id: SessionId, delegation_id: Annotated[str, PathParameter(min_length=1, max_length=256)]
    ) -> dict[str, object]:
        return await service().delegation_details(session_id, delegation_id)

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
