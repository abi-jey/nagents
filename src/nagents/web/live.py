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
from nagents.provider import FoundryProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import RetryConfig

from .live_settings import LIVE_VOICES
from .live_settings import LiveSettingsInput
from .live_settings import Revision
from .routing import RoutingStore

if TYPE_CHECKING:
    from .live_runtime import LiveService
    from .live_settings import LiveConnection
    from .live_settings import LiveSettings
    from .service import WebState

logger = logging.getLogger("uvicorn.error")
SessionId = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Cursor = Annotated[int, Query(ge=0, le=2**53 - 1)]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    voice: str = Field(default="", max_length=64)
    revision: Revision
    session_id: str = Field(default="", max_length=80, pattern=r"^$|^ngn-[a-zA-Z0-9-]+$")


def unavailable_reason(connection: "LiveConnection", *, demo: bool) -> str:
    """Local readiness only: never probe the provider, read a login, or create an agent."""
    values = connection.values
    if connection.profile is not None and connection.profile.kind not in {
        "openai",
        "openai_compatible",
        "foundry",
        "azure_openai_compatible_v1",
    }:
        return "The active provider does not support GPT-Live. Select an OpenAI or Foundry connection."
    if demo:
        return "GPT-Live is unavailable in offline demo mode. Restart ngn serve without --demo."
    if not values.enabled:
        return "GPT-Live is disabled. Enable it in Connection settings."
    if values.provider == "azure_openai_compatible_v1" and not values.base_url:
        return "The Azure v1 Live provider requires an API base URL. Open Connection settings to set it."
    if not connection.key_configured:
        if connection.profile is not None:
            return (
                f"Set ${connection.profile.key_env} in the ngn serve environment, then restart the server. "
                "ChatGPT/Codex login does not authorize Live."
            )
        return "Add a named provider connection in Global or Workspace settings to configure Live credentials."
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
        "model": values.model,
        "backend_model": values.backend_model,
        "backend_mode": values.backend_mode,
        "assistant": assistant or {},
        "voice": values.voice,
        "voices": list(LIVE_VOICES),
        "active_session_id": active_session_id,
        "revision": connection.revision,
        "enabled": values.enabled,
        "key_configured": connection.key_configured,
    }


def create_agent(
    connection: "LiveConnection",
    voice: str = "",
    *,
    demo: bool = False,
    client_handler: Callable[[str], Awaitable[str]] | None = None,
) -> Agent:
    """A fresh voice agent; client mode delegates inference to the selected Harness."""
    reason = unavailable_reason(connection, demo=demo)
    if reason:
        raise HTTPException(503, reason)
    if voice and voice not in LIVE_VOICES:
        raise HTTPException(422, "Choose a supported Live voice.")
    values = connection.values
    if values.backend_mode == "assistant" and client_handler is None:
        raise HTTPException(503, "Select an assistant conversation before connecting GPT-Live.")
    options = LiveConfig(
        delegation="client" if values.backend_mode == "assistant" else "responses",
        backend_model=values.backend_model,
        voice=voice or values.voice,
        store=False,
        backend_timeout=420 if values.backend_mode == "assistant" else 120,
        client_handler=client_handler if values.backend_mode == "assistant" else None,
    )
    key = connection.api_key.get_secret_value()
    provider: Provider
    if connection.profile is not None:
        provider = build_live_provider(connection.profile, options, key)
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
        system_prompt=(
            "You are a helpful AI voice assistant. Keep spoken replies concise. "
            "Delegate reasoning to your hosted backend. You have no access to the user's workspace or chat history."
        ),
        tools=[],
        compactor=None,
        save_tool_outputs=False,
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
    async def connection_settings() -> dict[str, object]:
        return await settings().snapshot()

    @app.post("/api/live/settings")
    async def save_settings(body: LiveSettingsInput) -> dict[str, object]:
        result = await settings().change(body)
        logger.info(
            "Live settings saved: provider=%s source=%s enabled=%s",
            body.values.provider,
            result.get("source", "legacy"),
            body.values.enabled,
        )
        return result

    @app.post("/api/live/sessions", status_code=201)
    async def create(body: SessionInput) -> dict[str, object]:
        if body.voice and body.voice not in LIVE_VOICES:
            raise HTTPException(422, "Choose a supported Live voice.")
        current = settings()
        host = state()
        with host.idle():
            async with current.admit(body.revision, body.session_id) as connection:
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

    @app.post("/api/live/sessions/{session_id}/close")
    async def close(session_id: SessionId) -> dict[str, object]:
        result = await service().close(session_id)
        logger.info("Live connection closed: session=%s", session_id)
        return result
