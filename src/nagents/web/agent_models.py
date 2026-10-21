"""Model-only edits for reusable agent profiles, including the main assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from nagents.harness.model_bindings import ModelBinding
from nagents.harness.model_bindings import ResolvedModelBinding

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Self

    from nagents.harness import Harness

    from .service import WebState

logger = logging.getLogger("uvicorn.error")
AgentName = Annotated[str, PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class ModelReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def complete_reference(self) -> Self:
        if bool(self.provider) != bool(self.id):
            raise ValueError("Choose both a provider connection and a model, or inherit defaults")
        ModelBinding(self.provider, self.id)
        return self

    def binding(self) -> ModelBinding:
        return ModelBinding(self.provider, self.id)


class AgentModelRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class AgentModelInput(AgentModelRevision):
    model: ModelReferenceInput


def _profile(harness: Harness, name: str) -> None:
    harness.refresh_agent_definitions()
    if name in harness.config.profiles and harness.config.profiles[name].a2a.configured:
        raise HTTPException(422, "This remote agent manages its own model. Open Connection settings.")
    if name not in harness.config.profile_names:
        raise HTTPException(404, "This agent profile is no longer available.")


def _default(harness: Harness) -> ResolvedModelBinding:
    try:
        return harness.default_model_binding()
    except ValueError:
        # The editor must remain usable when a referenced connection is removed.
        # Preserve the unavailable pair instead of substituting a working route.
        workspace = harness.provider_store.model_binding("workspace")
        global_default = harness.provider_store.model_binding("global")
        if workspace.configured:
            return ResolvedModelBinding(
                workspace, "workspace", str(harness.provider_store.model_store("workspace").path)
            )
        if global_default.configured:
            return ResolvedModelBinding(
                global_default, "global", str(harness.provider_store.model_store("global").path)
            )
        return ResolvedModelBinding(
            ModelBinding(harness._default_provider_name or harness.providers.active, harness._selected_model), "builtin"
        )


def snapshot(harness: Harness, name: str) -> dict[str, object]:
    """Read definition and override provenance without changing any running agent."""
    _profile(harness, name)
    try:
        preference = harness.provider_store.agent_models.load(name)
        inherited = _default(harness)
        profile = harness.config.profile(name)
        try:
            definition = harness.definition_model_binding(name)
        except ValueError:
            definition = (
                ModelBinding(profile.provider or inherited.binding.provider, profile.model or inherited.binding.id)
                if profile.model or profile.provider
                else ModelBinding()
            )
        exists = preference.model_present
        unavailable_reason = ""
        try:
            effective = harness.resolved_model_binding(name)
        except ValueError as error:
            unavailable_reason = str(error)
            binding = preference.binding if exists else definition
            effective = (
                ResolvedModelBinding(binding, "agent", str(preference.path) if exists else profile.source)
                if binding.configured
                else inherited
            )
        registry = harness.provider_store.load()
        local_names = harness.provider_store.load_scope("workspace").providers
        provider = effective.binding.provider
        if provider and provider not in registry.providers:
            unavailable_reason = f"Model references unavailable provider connection {provider!r}"
        provider_origin = (
            ("workspace" if provider in local_names else "global") if provider in registry.providers else ""
        )
        return {
            "agent": name,
            "revision": preference.revision,
            "model": preference.binding.as_dict() if exists else None,
            "definition_model": definition.as_dict() if definition.configured else None,
            "inherited": inherited.binding.as_dict(),
            "effective": effective.binding.as_dict(),
            "source": effective.layer,
            "provider_origin": provider_origin,
            "source_path": effective.source,
            "available": not bool(unavailable_reason),
            "unavailable_reason": unavailable_reason,
        }
    except (OSError, ValueError):
        raise HTTPException(422, "Agent model settings could not be read. Check the saved model references.") from None


def register(app: FastAPI, state: Callable[[], WebState]) -> None:
    @app.get("/api/agent-models/{name}")
    async def get_model(name: AgentName) -> dict[str, object]:
        return snapshot(state().harness, name)

    async def change(
        name: str, body: AgentModelRevision, action: Literal["save", "reset"], binding: ModelBinding
    ) -> dict[str, object]:
        host = state()
        async with host.configure():
            harness = host.harness
            _profile(harness, name)
            if (
                action == "save"
                and binding.provider not in harness.provider_store.load().providers
                and binding.configured
            ):
                raise HTTPException(
                    422, "The model must reference a configured global or workspace provider connection."
                )
            # Check all inputs needed by the reply before committing an edit.
            # Unavailable references remain inspectable and can be repaired.
            snapshot(harness, name)
            store = harness.provider_store.agent_models
            try:
                current = store.load(name)
                if current.revision != body.revision:
                    raise HTTPException(409, "Agent model changed. Refresh before saving.")
                saved = (
                    store.save(name, binding, expected=body.revision)
                    if action == "save"
                    else store.reset(name, expected=body.revision)
                )
            except ValueError as error:
                status = 409 if "changed" in str(error) else 422
                raise HTTPException(status, str(error)) from None
            except OSError:
                raise HTTPException(500, "Agent model settings could not be saved.") from None
            result = snapshot(harness, name)
            session_ids = {host.selected_session_id, *(subscriber.session_id for subscriber in host.bus.subscribers)}
            for session_id in session_ids - {""}:
                host.bus.event(
                    {
                        "event": "agent_profiles_changed",
                        "schema_version": 1,
                        "session_id": session_id,
                        "profile_revision": harness.profile_revision,
                        "agent": name,
                        "model_revision": saved.revision,
                    }
                )
            host.changed()
            logger.info("Agent model preference updated: agent=%s action=%s", name, action)
            return result

    @app.put("/api/agent-models/{name}")
    async def save_model(name: AgentName, body: AgentModelInput) -> dict[str, object]:
        return await change(name, body, "save", body.model.binding())

    @app.delete("/api/agent-models/{name}")
    async def reset_model(name: AgentName, body: AgentModelRevision) -> dict[str, object]:
        return await change(name, body, "reset", ModelBinding())
