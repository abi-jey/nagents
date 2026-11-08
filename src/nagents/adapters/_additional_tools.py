"""Request-local Responses tool timelines; no executable callables are retained.

Timelines are a cache optimization, not conversation state. A process restart,
compaction, edited prefix, removal or schema change safely rebases the full tool
list. Session storage therefore needs no provider-specific migration.
"""

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field

from ..http import HTTPError


class AdditionalToolsUnsupported(HTTPError):
    """A sanitized, explicit rejection of the additional_tools extension."""

    def __init__(self) -> None:
        super().__init__(400, "Provider does not support additional_tools")


def rejects_additional_tools(error: object) -> bool:
    """Do not interpret arbitrary 400s, auth failures or malformed tools as support failures."""
    if not isinstance(error, dict):
        return False
    message = error.get("message", "")
    parameter = error.get("param", "")
    code = error.get("code", "")
    if not isinstance(message, str):
        return False
    text = message.lower()
    return ("additional_tools" in text or (isinstance(parameter, str) and "additional_tools" in parameter)) and (
        (isinstance(code, str) and code in {"unsupported_parameter", "unknown_parameter", "unsupported_value"})
        or any(term in text for term in ("not supported", "unsupported", "unknown parameter", "unrecognized"))
    )


@dataclass
class ToolTimeline:
    """Stable wire prefix plus chronological additive tool declarations."""

    prefix: list[object] = field(default_factory=list)
    base: list[dict[str, object]] = field(default_factory=list)
    current: list[dict[str, object]] = field(default_factory=list)
    additions: list[tuple[int, list[dict[str, object]]]] = field(default_factory=list)
    initialized: bool = False

    def apply(self, body: dict[str, object]) -> None:
        inputs = body["input"]
        tools = body.get("tools", [])
        assert isinstance(inputs, list) and isinstance(tools, list)
        old = {tool["name"]: tool for tool in self.current}
        new = {tool["name"]: tool for tool in tools}
        # A changed conversation prefix cannot safely retain old insertion offsets.
        if (
            not self.initialized
            or inputs[: len(self.prefix)] != self.prefix
            or len(new) != len(tools)
            or any(name not in new or new[name] != tool for name, tool in old.items())
        ):
            self.base = deepcopy(tools)
            self.current = deepcopy(tools)
            self.additions = []
            self.initialized = True
        else:
            added = [tool for tool in tools if tool["name"] not in old]
            if added:
                self.additions.append((len(inputs), deepcopy(added)))
                self.current.extend(deepcopy(added))
        self.prefix = deepcopy(inputs)
        wire = list(inputs)
        for offset, added in reversed(self.additions):
            wire.insert(offset, {"type": "additional_tools", "role": "developer", "tools": deepcopy(added)})
        body["input"] = wire
        if self.base:
            body["tools"] = deepcopy(self.base)
        else:
            body.pop("tools", None)
