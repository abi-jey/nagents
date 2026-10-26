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

from nagents.harness.agents.behavior import BehaviorScope
from nagents.harness.agents.behavior import behavior_layers
from nagents.harness.agents.behavior import behavior_store
from nagents.harness.agents.behavior import resolve_behavior
from nagents.harness.config import AgentProfile
from nagents.harness.default_instructions import load_default_instructions
from nagents.harness.tools.coding import MODIFYING_TOOLS
from nagents.harness.tools.coding import READ_ONLY_TOOLS
from nagents.harness.tools.configuration import HarnessToolRegistry
from nagents.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.harness import Harness
    from nagents.types import ToolDefinition

    from ..execution.service import WebState

logger = logging.getLogger(__name__)
AgentName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
ToolName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_.:-]*$")]


class BehaviorPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instructions: str = Field(default="", max_length=24000)
    tools: list[ToolName] = Field(default_factory=list, max_length=512)


class BehaviorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: BehaviorScope
    agent: AgentName = "assistant"
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    preferences: BehaviorPreferences


def _runtime_reason(
    harness: Harness, profile: AgentProfile, scope: BehaviorScope, agent: str, tool: ToolDefinition
) -> str:
    """Preview durable runtime gates without borrowing another agent's policy.

    Shared defaults have no source-profile ceiling. Agent previews use the named
    profile, not the currently selected agent's delegation or reviewer settings.
    Scheduling and delegation limits here are capabilities, never live quotas.
    """
    builtin = tool.func is not None and tool.func == harness.tools.builtins.get(tool.name)
    root = harness.tasks.root.harness if harness._is_subagent else harness
    if tool.name == "to_agent":
        if not harness.allow_subagents or not root.allow_subagents:
            return "Delegation is disabled for this runtime."
        if harness.subagent_depth >= min(harness.config.max_subagent_depth, root.config.max_subagent_depth):
            return "Unavailable at the configured delegation depth limit."
        if not any(
            harness.config.profile(name).subagent_available
            and (profile.allowed_agents is None or name in profile.allowed_agents)
            for name in harness.config.profile_names
        ):
            return "No agents are eligible for delegation from this profile."
    if harness.config.demo and (not builtin or tool.name not in READ_ONLY_TOOLS | {"demo_preview", "to_agent"}):
        return "Unavailable in offline demo mode."
    if (
        scope == "agent"
        and harness.mode_for_profile(agent) == "reviewer"
        and (not builtin or tool.name in MODIFYING_TOOLS)
    ):
        return "Unavailable in reviewer mode."
    if harness._is_subagent and not builtin and not harness.supports_child_custom_tools:
        return "Custom tools are unavailable in this child runtime."
    if builtin and tool.func == harness.tools.schedule_wakeup and root.wakeup_handler is None:
        return "No wake-up scheduler is configured for this runtime."
    return ""


def snapshot(state: WebState, scope: BehaviorScope, agent: str) -> dict[str, object]:
    harness = state.harness
    harness.refresh_agent_definitions()
    if agent not in harness.config.profile_names:
        raise HTTPException(404, "This agent definition is no longer available.")
    profile = harness.config.agent_authored_profiles.get(agent, AgentProfile()) if scope == "agent" else AgentProfile()
    layers = behavior_layers(harness.provider_store, agent, through_scope=scope)
    preference = layers[scope]
    managed = layers["global"]
    revisions = [scope, agent, harness.profile_revision]
    for name in ("global", "workspace", "agent"):
        revisions.append(layers[name].revision)
        if name == scope:
            break
    revision = hashlib.sha256(":".join(revisions).encode()).hexdigest()
    resolved = resolve_behavior(layers, profile, scope)
    inherited = resolve_behavior(layers, profile, scope, inherit=True)
    policy = harness.tool_settings
    if scope == "agent":
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
        if not reason:
            reason = _runtime_reason(harness, profile, scope, agent, tool)
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
    identities = harness.agent_identities()
    return {
        "scope": scope,
        "agent": agent,
        "revision": revision,
        "preferences_revision": preference.revision,
        "preferences": {
            ("instructions" if key == "prompt" else key): value
            for key, value in preference.preferences.items()
            if key in {"prompt", "tools"}
        },
        "values": {"instructions": resolved.instructions, "tools": desired},
        "inherited": {
            "instructions": inherited.instructions,
            "tools": list(inherited.tools) if inherited.tools_limited else sorted(names),
        },
        "origins": {"instructions": resolved.instructions_origin, "tools": resolved.tools_origin},
        "inherited_origins": {"instructions": inherited.instructions_origin, "tools": inherited.tools_origin},
        "field_paths": {"instructions": resolved.instructions_path},
        "inherited_paths": {"instructions": inherited.instructions_path},
        "managed_defaults": {
            "auto_update": managed.preferences.get("auto_update", False),
            "path": str(managed.path),
            "default_revision": managed.preferences.get("default_revision", ""),
        },
        "definition": {
            "instructions": profile.prompt if profile.prompt_present else profile.instructions,
            "tools": list(profile.tools) if profile.tools is not None else None,
        },
        "profiles": [
            {
                "id": name,
                "name": identities[name].name,
                "description": identities[name].description,
                "source": harness.config.profile(name).source,
                "source_format": harness.config.profile(name).source_format,
                "mode": harness.mode_for_profile(name),
                "transport": "a2a" if harness.config.profile(name).a2a.configured else "local",
            }
            for name in harness.config.profile_names
        ],
        "tools": sorted(tools, key=lambda tool: str(tool["name"])),
        "effective_tools": [name for name in desired if name in available],
        "source_path": str(preference.path),
        "preferences_path": str(preference.path),
        "definition_path": profile.source,
        "native_instructions": load_default_instructions(),
        "workspace_instructions": [{"path": path, "text": text} for path, text in sorted(harness.instructions.items())],
        "applies": "next_model_round",
    }


def register(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/agent-settings")
    async def read(scope: BehaviorScope = "agent", agent: AgentName = "assistant") -> dict[str, object]:
        try:
            return snapshot(get(), scope, agent)
        except (OSError, ValueError) as error:
            logger.warning(
                "Agent settings read failed: scope=%s agent=%s error_type=%s", scope, agent, type(error).__name__
            )
            raise HTTPException(422, "Agent settings could not be read. Check the saved agent preferences.") from None

    @app.put("/api/agent-settings")
    async def save(body: BehaviorInput) -> dict[str, object]:
        state = get()
        if body.scope == "agent" and state.harness.config.profile(body.agent).a2a.configured:
            raise HTTPException(
                422, "This remote agent manages its own instructions and tools. Open Connection settings."
            )
        async with state.queued_mutation():
            try:
                current = snapshot(state, body.scope, body.agent)
                if current["revision"] != body.revision:
                    raise HTTPException(409, "Agent settings changed. Reload before saving.")
                store = behavior_store(state.harness.provider_store, body.scope)
                values = body.preferences.model_dump(exclude_unset=True)
                store.replace_fields(
                    body.agent if body.scope == "agent" else "assistant",
                    ("prompt", "tools"),
                    {("prompt" if key == "instructions" else key): value for key, value in values.items()},
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
