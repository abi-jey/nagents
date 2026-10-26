"""Authenticated instruction editing, independent from other Voice preferences."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from ..voice_instruction_store import InstructionScope
from ..voice_instruction_store import validate_instruction_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..execution.service import WebState
    from ..voice_preferences import Scope
    from .settings import LiveSettings

logger = logging.getLogger(__name__)
AgentName = Annotated[str, Field(max_length=128, pattern=r"^$|^[A-Za-z0-9_-]+$")]
InstructionText = Annotated[str, AfterValidator(validate_instruction_text)]


class InstructionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    scope: InstructionScope
    agent: AgentName = ""
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: InstructionText = ""

    @model_validator(mode="after")
    def selection(self) -> InstructionInput:
        if (self.scope == "agent") != bool(self.agent):
            raise ValueError("Select an agent only for agent Voice instructions.")
        return self


async def save_instructions(settings: LiveSettings, body: InstructionInput) -> dict[str, object]:
    with settings._idle(allow_active=True):
        scope: Scope = "global" if body.scope == "global" else "workspace"

        def save() -> dict[str, object]:
            layer = settings._agent_layer(body.agent)
            current = settings._scoped(scope, layer)
            if current.revision != body.revision:
                raise HTTPException(409, "Voice instructions or their defaults changed. Reload before saving.")
            profiles = (
                *settings.providers.load_scope("global").providers.values(),
                *settings.providers.load_scope("workspace").providers.values(),
            )
            keys = (os.environ.get(profile.key_env, "") for profile in profiles)
            if any(key and key in body.prompt for key in keys):
                raise HTTPException(422, "Keep API keys out of Voice instructions.")
            settings.instructions.save(
                body.scope,
                body.agent,
                body.prompt,
                present="prompt" in body.model_fields_set,
                expected=current.instruction_preferences_revision,
            )
            return settings._scoped(scope, settings._agent_layer(body.agent)).instruction_settings

        async def commit() -> dict[str, object]:
            try:
                return await asyncio.to_thread(save)
            except HTTPException:
                raise
            except ValueError as error:
                raise HTTPException(
                    409, "The agent Markdown file changed or could not be read. Reload before saving."
                ) from error
            except OSError:
                raise HTTPException(500, "Voice instructions could not be saved. Reload before retrying.") from None

        return await settings._owned(commit())


def register(app: FastAPI, get: Callable[[], LiveSettings], state: Callable[[], WebState]) -> None:
    @app.get("/api/live/instructions")
    async def read(scope: InstructionScope, agent: AgentName = "") -> dict[str, object]:
        if (scope == "agent") != bool(agent):
            raise HTTPException(422, "Select an agent only for agent Voice instructions.")
        current = await get().scoped("global" if scope == "global" else "workspace", agent=agent)
        return current.instruction_settings

    @app.put("/api/live/instructions")
    async def write(body: InstructionInput) -> dict[str, object]:
        async with state().queued_mutation():
            result = await save_instructions(get(), body)
            state().changed()
            logger.info("Voice instructions saved: scope=%s agent=%s", body.scope, body.agent)
            return result
