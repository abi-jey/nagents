"""Dedicated, tool-free GPT-Live configuration and same-origin HTTP routes."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from fastapi import Query
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.agent import Agent
from nagents.live import LiveConfig
from nagents.provider import FoundryProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import RetryConfig

from .live_settings import LIVE_VOICES
from .live_settings import LiveSettingsInput
from .live_settings import Revision

if TYPE_CHECKING:
    from .live_runtime import LiveService
    from .live_settings import LiveConnection
    from .live_settings import LiveSettings

MAX_SDP_CHARACTERS = 60000
SessionId = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Cursor = Annotated[int, Query(ge=0, le=2**53 - 1)]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sdp: str = Field(min_length=1, max_length=MAX_SDP_CHARACTERS)
    voice: str = Field(default="", max_length=64)
    revision: Revision


def unavailable_reason(connection: "LiveConnection", *, demo: bool) -> str:
    """Local readiness only: never probe the provider, read a login, or create an agent."""
    values = connection.values
    if demo:
        return "GPT-Live is unavailable in offline demo mode. Restart ngn serve without --demo."
    if not values.enabled:
        return "GPT-Live is disabled. Enable it in Connection settings."
    if values.provider == "azure_openai_compatible_v1" and not values.base_url:
        return "The Azure v1 Live provider requires an API base URL. Open Connection settings to set it."
    if not connection.key_configured:
        return "Add a Live API key in Connection settings. ChatGPT/Codex login does not authorize Live."
    return ""


def capabilities(connection: "LiveConnection", active_session_id: str = "", *, demo: bool = False) -> dict[str, object]:
    """Allowlisted discovery fields without credentials; editable values have their own route."""
    reason = unavailable_reason(connection, demo=demo)
    values = connection.values
    return {
        "available": not reason,
        "reason": reason,
        "provider": values.provider,
        "model": values.model,
        "backend_model": values.backend_model,
        "voice": values.voice,
        "voices": list(LIVE_VOICES),
        "active_session_id": active_session_id,
        "revision": connection.revision,
        "enabled": values.enabled,
        "key_configured": connection.key_configured,
    }


def create_agent(connection: "LiveConnection", voice: str = "", *, demo: bool = False) -> Agent:
    """A fresh owned voice agent, independent of Harness tools, history and login selection."""
    reason = unavailable_reason(connection, demo=demo)
    if reason:
        raise HTTPException(503, reason)
    if voice and voice not in LIVE_VOICES:
        raise HTTPException(422, "Choose a supported Live voice.")
    values = connection.values
    options = LiveConfig(backend_model=values.backend_model, voice=voice or values.voice, store=False)
    key = connection.api_key.get_secret_value()
    provider: Provider
    if values.provider == "azure_openai_compatible_v1":
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


def register(app: FastAPI, service: Callable[[], "LiveService"], settings: Callable[[], "LiveSettings"]) -> None:
    @app.get("/api/live")
    async def discover() -> dict[str, object]:
        current = settings()
        return capabilities(await current.connection(), service().active_session_id, demo=current.demo)

    @app.get("/api/live/settings")
    async def connection_settings() -> dict[str, object]:
        return await settings().snapshot()

    @app.post("/api/live/settings")
    async def save_settings(body: LiveSettingsInput) -> dict[str, object]:
        return await settings().change(body)

    @app.post("/api/live/sessions", status_code=201)
    async def create(body: SessionInput) -> dict[str, object]:
        if not body.sdp.strip():
            raise HTTPException(422, "An SDP offer is required.")
        if body.voice and body.voice not in LIVE_VOICES:
            raise HTTPException(422, "Choose a supported Live voice.")
        current = settings()
        async with current.admit(body.revision) as connection:
            reason = unavailable_reason(connection, demo=current.demo)
            if reason:
                raise HTTPException(503, reason)
            return await service().create(body.sdp, body.voice)

    @app.get("/api/live/sessions/{session_id}")
    async def snapshot(session_id: SessionId, after: Cursor = 0) -> dict[str, object]:
        return await service().snapshot(session_id, after)

    @app.post("/api/live/sessions/{session_id}/close")
    async def close(session_id: SessionId) -> dict[str, object]:
        return await service().close(session_id)
