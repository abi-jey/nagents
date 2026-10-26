"""Reasoning preferences, resolved before native provider request construction."""

from typing import Literal
from typing import cast

from nagents.provider.reasoning import reasoning_effort_options
from nagents.types import ReasoningEffort

ReasoningPreference = ReasoningEffort | Literal["default"]
REASONING_PREFERENCES = ("", "default", "none", "minimal", "low", "medium", "high", "xhigh", "max")


def resolve_effort(value: str, inherited: ReasoningEffort = "") -> ReasoningEffort:
    """Empty inherits a preference; explicit default clears it for this agent."""
    if value not in REASONING_PREFERENCES:
        raise ValueError("Unknown reasoning effort")
    return inherited if not value else "" if value == "default" else cast("ReasoningEffort", value)


def validate_effort(
    value: ReasoningEffort, provider: str, model: str, *, api: str = "auto", auth: str = "", base_url: str = ""
) -> None:
    if value and value not in reasoning_effort_options(provider, model, api=api, auth=auth, base_url=base_url):
        raise ValueError("This reasoning effort is not supported by the selected provider and model")
