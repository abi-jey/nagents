"""Scoped agent behavior preferences, independent of model and Live routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from ..default_instructions import load_default_instructions
from .documents import agent_document_store

if TYPE_CHECKING:
    from ..config import AgentProfile
    from ..providers.model_bindings import AgentModelPreference
    from ..providers.model_bindings import AgentModelStore
    from ..providers.registry import ScopedProviderRegistryStore

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
    instructions_path: str = ""

    def permits(self, name: str) -> bool:
        return not self.tools_limited or name in self.tools


def behavior_store(providers: ScopedProviderRegistryStore, scope: BehaviorScope) -> AgentModelStore:
    return agent_document_store(providers, scope)


def behavior_layers(
    providers: ScopedProviderRegistryStore, name: str, *, through_scope: BehaviorScope = "agent"
) -> dict[BehaviorScope, AgentModelPreference]:
    return {
        scope: behavior_store(providers, scope).load(name if scope == "agent" else "assistant")
        for scope in _LAYERS[: _LAYERS.index(through_scope) + 1]
    }


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
    instructions = load_default_instructions()
    instructions_path = "nagents/harness/instructions/assistant.md"
    instruction_origin: BehaviorOrigin = "builtin"
    tools: tuple[str, ...] = ()
    limited = False
    tool_origin: BehaviorOrigin = "builtin"
    for layer in _LAYERS:
        if layer == "agent":
            if profile.prompt_present:
                instructions, instruction_origin, instructions_path = (
                    profile.prompt,
                    "definition",
                    dict(profile.field_sources).get("prompt", profile.source),
                )
            elif profile.instructions:
                instructions += f"\n\n{profile.instructions}"
                instruction_origin, instructions_path = "definition", profile.source
            if profile.tools is not None:
                tools, limited, tool_origin = profile.tools, True, "definition"
        if not (inherit and layer == scope):
            values = layers[layer].preferences
            if "prompt" in values:
                value = values["prompt"]
                assert isinstance(value, str)
                instructions, instruction_origin, instructions_path = value, layer, str(layers[layer].path)
            if "tools" in values:
                value = values["tools"]
                assert isinstance(value, list) and all(isinstance(item, str) for item in value)
                tools, limited, tool_origin = tuple(str(item) for item in value), True, layer
        if layer == scope:
            break
    return AgentBehavior(instructions, instruction_origin, tools, limited, tool_origin, instructions_path)


def load_behavior(providers: ScopedProviderRegistryStore, name: str, profile: AgentProfile) -> AgentBehavior:
    return resolve_behavior(behavior_layers(providers, name), profile)
