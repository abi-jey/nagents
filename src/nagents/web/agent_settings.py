"""Agent navigator behavior editing with explicit defaults and provenance."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.harness.agent_behavior import BehaviorScope
from nagents.harness.agent_behavior import behavior_layers
from nagents.harness.agent_behavior import behavior_store
from nagents.harness.agent_behavior import resolve_behavior
from nagents.harness.config import AgentProfile
from nagents.harness.default_instructions import load_default_instructions
from nagents.harness.tool_config import HarnessToolRegistry
from nagents.harness.tools import MODIFYING_TOOLS
from nagents.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from .service import WebState

logger = logging.getLogger(__name__)
AgentName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
ToolName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$")]


class BehaviorPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instructions: str = Field(default="", max_length=12000)
    tools: list[ToolName] = Field(default_factory=list, max_length=512)


class BehaviorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: BehaviorScope
    agent: AgentName = "assistant"
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    preferences: BehaviorPreferences


def snapshot(state: WebState, scope: BehaviorScope, agent: str) -> dict[str, object]:
    harness = state.harness
    harness.refresh_agent_definitions()
    if agent not in harness.config.profile_names:
        raise HTTPException(404, "This agent definition is no longer available.")
    profile = harness.config.profile(agent) if scope == "agent" else AgentProfile()
    layers = behavior_layers(harness.provider_store, agent)
    preference = layers[scope]
    revisions = [scope, agent, harness.profile_revision]
    for name in ("global", "workspace", "agent"):
        revisions.append(layers[name].revision)
        if name == scope:
            break
    revision = hashlib.sha256(":".join(revisions).encode()).hexdigest()
    resolved = resolve_behavior(layers, profile, scope)
    inherited = resolve_behavior(layers, profile, scope, inherit=True)
    policy = harness.tool_settings
    policy.load()
    registry = harness.agent.tool_registry
    catalog = registry.get_catalog() if isinstance(registry, HarnessToolRegistry) else ToolRegistry.get_all(registry)
    names = {tool.name for tool in catalog}
    desired = list(resolved.tools) if resolved.tools_limited else sorted(names)
    tools: list[dict[str, object]] = []
    for tool in catalog:
        reason = ""
        if scope == "agent":
            if profile.tools is not None and tool.name not in profile.tools:
                reason = "Not included in this agent definition's tool permissions."
            elif tool.name in profile.denied_tools:
                reason = "Denied by this agent definition."
            elif not policy.enabled(agent, tool.name):
                reason = "Disabled in the workspace tool configuration."
            elif harness.mode_for_profile(agent) == "reviewer" and (
                tool.name in MODIFYING_TOOLS or tool.func != harness.tools.builtins.get(tool.name)
            ):
                reason = "Unavailable in reviewer mode."
        category = (
            "Files"
            if tool.name in {"read_file", "list_files", "find", "search", "edit", "write"}
            else "MCP"
            if tool.name.startswith("mcp__")
            else "Channels"
            if "channel" in tool.name
            else "Agent"
        )
        tools.append(
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "category": category,
                "available": not reason,
                "reason": reason,
            }
        )
    for name in sorted(set(desired) - names):
        tools.append(
            {
                "name": name,
                "description": "Saved selection; this tool is not currently registered.",
                "parameters": {},
                "category": "Unavailable",
                "available": False,
                "reason": "Tool is not currently registered.",
            }
        )
    available = {str(tool["name"]) for tool in tools if tool["available"]}
    return {
        "scope": scope,
        "agent": agent,
        "revision": revision,
        "preferences_revision": preference.revision,
        "preferences": {
            key: value for key, value in preference.preferences.items() if key in {"instructions", "tools"}
        },
        "values": {"instructions": resolved.instructions, "tools": desired},
        "inherited": {
            "instructions": inherited.instructions,
            "tools": list(inherited.tools) if inherited.tools_limited else sorted(names),
        },
        "origins": {"instructions": resolved.instructions_origin, "tools": resolved.tools_origin},
        "definition": {
            "instructions": profile.instructions,
            "tools": list(profile.tools) if profile.tools is not None else None,
        },
        "profiles": [
            {
                "id": name,
                "name": name,
                "description": harness.config.profile(name).description,
                "source": harness.config.profile(name).source,
                "source_format": harness.config.profile(name).source_format,
                "mode": harness.mode_for_profile(name),
            }
            for name in harness.config.profile_names
        ],
        "tools": sorted(tools, key=lambda tool: str(tool["name"])),
        "effective_tools": [name for name in desired if name in available],
        "source_path": str(preference.path),
        "native_instructions": load_default_instructions(),
        "workspace_instructions": [{"path": path, "text": text} for path, text in sorted(harness.instructions.items())],
        "applies": "next_model_round",
    }


def register(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/agent-settings")
    async def read(scope: BehaviorScope = "agent", agent: AgentName = "assistant") -> dict[str, object]:
        try:
            return snapshot(get(), scope, agent)
        except (OSError, ValueError):
            raise HTTPException(422, "Agent settings could not be read. Check the saved agent preferences.") from None

    @app.put("/api/agent-settings")
    async def save(body: BehaviorInput) -> dict[str, object]:
        state = get()
        async with state.queued_mutation():
            try:
                current = snapshot(state, body.scope, body.agent)
                if current["revision"] != body.revision:
                    raise HTTPException(409, "Agent settings changed. Reload before saving.")
                store = behavior_store(state.harness.provider_store, body.scope)
                store.replace_fields(
                    body.agent if body.scope == "agent" else "defaults",
                    ("instructions", "tools"),
                    body.preferences.model_dump(exclude_unset=True),
                    expected=str(current["preferences_revision"]),
                )
                result = snapshot(state, body.scope, body.agent)
            except ValueError as error:
                status = 409 if "changed" in str(error).lower() else 422
                raise HTTPException(
                    status,
                    "Agent preferences changed. Reload before saving."
                    if status == 409
                    else "Invalid agent preferences.",
                ) from None
            except OSError:
                raise HTTPException(500, "Agent settings could not be saved. Reload before retrying.") from None
            state.changed()
            logger.info("Agent behavior preferences saved: scope=%s agent=%s", body.scope, body.agent)
            return result
