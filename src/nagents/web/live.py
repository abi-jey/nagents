"""Dedicated, tool-free GPT-Live configuration and same-origin HTTP routes."""

import os
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
from nagents.harness.config import LIVE_VOICES
from nagents.harness.config import PROVIDERS
from nagents.live import LiveConfig
from nagents.provider import FoundryProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.session import SessionManager
from nagents.types import RetryConfig

if TYPE_CHECKING:
    from nagents.harness.config import HarnessConfig

    from .live_runtime import LiveService

MAX_SDP_CHARACTERS = 60000
SessionId = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Cursor = Annotated[int, Query(ge=0, le=2**53 - 1)]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sdp: str = Field(min_length=1, max_length=MAX_SDP_CHARACTERS)
    voice: str = Field(default="", max_length=64)


def unavailable_reason(config: "HarnessConfig") -> str:
    """Local readiness only: never probe the provider, read a login, or create an agent."""
    if not config.live_enabled:
        return "GPT-Live is disabled. Set live_enabled: true or NGN_LIVE_ENABLED=true and restart ngn serve."
    if config.demo:
        return "GPT-Live is unavailable in offline demo mode. Restart ngn serve without --demo."
    provider = PROVIDERS.get(config.live_provider)
    if provider not in {ProviderType.OPENAI_COMPATIBLE, ProviderType.AZURE_OPENAI_COMPATIBLE_V1}:
        return (
            "This Live provider is unsupported. Configure live_provider as openai, openai_compatible, "
            "or azure_openai_compatible_v1 with a GPT-Live-compatible endpoint."
        )
    if provider == ProviderType.AZURE_OPENAI_COMPATIBLE_V1 and not config.live_base_url:
        return "The Azure v1 Live provider requires an explicit live_base_url API prefix."
    key = os.environ.get(config.live_api_key_env, "")
    if not key:
        return (
            f"GPT-Live requires an API key in the server environment variable {config.live_api_key_env}. "
            "ChatGPT/Codex login does not authorize Live. Supply the key and restart ngn serve."
        )
    if len(key) > 65536 or any(not 33 <= ord(char) <= 126 for char in key):
        return "The configured Live API key is invalid. Check the server environment and restart ngn serve."
    return ""


def capabilities(config: "HarnessConfig", active_session_id: str = "") -> dict[str, object]:
    """Allowlisted public configuration; credential values and endpoint URLs stay server-side."""
    reason = unavailable_reason(config)
    return {
        "available": not reason,
        "reason": reason,
        "provider": config.live_provider,
        "model": config.live_model,
        "backend_model": config.live_backend_model,
        "voice": config.live_voice,
        "voices": list(LIVE_VOICES),
        "active_session_id": active_session_id,
    }


def create_agent(config: "HarnessConfig", voice: str = "") -> Agent:
    """A fresh owned voice agent, independent of Harness tools, history and login selection."""
    reason = unavailable_reason(config)
    if reason:
        raise HTTPException(503, reason)
    if voice and voice not in LIVE_VOICES:
        raise HTTPException(422, "Choose a supported Live voice.")
    options = LiveConfig(backend_model=config.live_backend_model, voice=voice or config.live_voice, store=False)
    key = os.environ.get(config.live_api_key_env, "")
    provider: Provider
    if PROVIDERS[config.live_provider] == ProviderType.AZURE_OPENAI_COMPATIBLE_V1:
        provider = FoundryProvider(
            base_url=config.live_base_url,
            model=config.live_model,
            api_key=key,
            live_config=options,
            retry_config=RetryConfig(max_retries=0),
        )
    else:
        provider = Provider(
            ProviderType.OPENAI_COMPATIBLE,
            api_key=key,
            model=config.live_model,
            base_url=config.live_base_url or "https://api.openai.com/v1",
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


def register(app: FastAPI, service: Callable[[], "LiveService"], config: "HarnessConfig") -> None:
    @app.get("/api/live")
    async def discover() -> dict[str, object]:
        return capabilities(config, service().active_session_id)

    @app.post("/api/live/sessions", status_code=201)
    async def create(body: SessionInput) -> dict[str, object]:
        if not body.sdp.strip():
            raise HTTPException(422, "An SDP offer is required.")
        if body.voice and body.voice not in LIVE_VOICES:
            raise HTTPException(422, "Choose a supported Live voice.")
        reason = unavailable_reason(config)
        if reason:
            raise HTTPException(503, reason)
        return await service().create(body.sdp, body.voice)

    @app.get("/api/live/sessions/{session_id}")
    async def snapshot(session_id: SessionId, after: Cursor = 0) -> dict[str, object]:
        return await service().snapshot(session_id, after)

    @app.post("/api/live/sessions/{session_id}/close")
    async def close(session_id: SessionId) -> dict[str, object]:
        return await service().close(session_id)
