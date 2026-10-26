"""Public model catalog capabilities, without transport or credential metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypedDict

if TYPE_CHECKING:
    from nagents.types import ReasoningEffort


class ModelCapabilities(TypedDict, total=False):
    context_window: int
    context_window_source: Literal["configured", "provider"]
    reasoning_efforts: list[ReasoningEffort]


@dataclass(frozen=True)
class ModelCatalog:
    models: list[str]
    metadata: dict[str, ModelCapabilities]
