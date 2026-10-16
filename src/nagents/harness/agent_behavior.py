"""Scoped agent behavior preferences, independent of model and Live routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from .model_bindings import AgentModelStore

if TYPE_CHECKING:
    from .config import AgentProfile
    from .model_bindings import AgentModelPreference
    from .providers import ScopedProviderRegistryStore

BehaviorScope = Literal["global", "workspace", "agent"]
BehaviorOrigin = Literal["builtin", "global", "workspace", "definition", "agent"]
_LAYERS: tuple[BehaviorScope, ...] = ("global", "workspace", "agent")


@dataclass(frozen=True)
class AgentBehavior:
    instructions: str = ""
    instructions_origin: BehaviorOrigin = "builtin"
    tools: tuple[str, ...] = ()
    tools_limited: bool = False
    tools_origin: BehaviorOrigin = "builtin"

    def permits(self, name: str) -> bool:
        return not self.tools_limited or name in self.tools


def behavior_store(providers: ScopedProviderRegistryStore, scope: BehaviorScope) -> AgentModelStore:
    if scope == "agent":
        return providers.agent_models
    return AgentModelStore(providers.model_store(scope).path.parent / "agent-defaults")


def behavior_layers(providers: ScopedProviderRegistryStore, name: str) -> dict[BehaviorScope, AgentModelPreference]:
    return {scope: behavior_store(providers, scope).load(name if scope == "agent" else "defaults") for scope in _LAYERS}


def resolve_behavior(
    layers: dict[BehaviorScope, AgentModelPreference],
    profile: AgentProfile,
    scope: BehaviorScope = "agent",
    *,
    inherit: bool = False,
) -> AgentBehavior:
    """Empty strings/lists are explicit; removing an overlay reveals its source.

    Authored profile tool lists are also enforced independently as permission
    ceilings. A user preference never grants an unavailable tool.
    """
    instructions = ""
    instruction_origin: BehaviorOrigin = "builtin"
    tools: tuple[str, ...] = ()
    limited = False
    tool_origin: BehaviorOrigin = "builtin"
    for layer in _LAYERS:
        if layer == "agent":
            if profile.instructions:
                instructions, instruction_origin = profile.instructions, "definition"
            if profile.tools is not None:
                tools, limited, tool_origin = profile.tools, True, "definition"
        if not (inherit and layer == scope):
            values = layers[layer].preferences
            if "instructions" in values:
                value = values["instructions"]
                assert isinstance(value, str)
                instructions, instruction_origin = value, layer
            if "tools" in values:
                value = values["tools"]
                assert isinstance(value, list) and all(isinstance(item, str) for item in value)
                tools, limited, tool_origin = tuple(str(item) for item in value), True, layer
        if layer == scope:
            break
    return AgentBehavior(instructions, instruction_origin, tools, limited, tool_origin)


def load_behavior(providers: ScopedProviderRegistryStore, name: str, profile: AgentProfile) -> AgentBehavior:
    return resolve_behavior(behavior_layers(providers, name), profile)
