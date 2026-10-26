"""Strict, data-only Live profiles independent from normal agent models."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model_bindings import ModelBinding

LIVE_FIELDS = frozenset({"enabled", "model", "voice", "prompt", "context_mode", "backend_mode", "backend_model"})
LIVE_VOICES = frozenset(
    {"marin", "cedar", "arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"}
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")


@dataclass(frozen=True)
class LiveProfile:
    """Only present fields override parents; model references are atomic pairs."""

    fields: tuple[tuple[str, object], ...] = ()

    @classmethod
    def parse(cls, value: object) -> LiveProfile:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ValueError("live must be a mapping")
        if set(value) - LIVE_FIELDS:
            raise ValueError("Unsupported live profile fields")
        selected: dict[str, object] = {}
        for name, item in value.items():
            if name == "enabled":
                if type(item) is not bool:
                    raise ValueError("live.enabled must be a boolean")
            elif name == "model":
                item = ModelBinding.parse(item)
                if not _MODEL.fullmatch(item.id):
                    raise ValueError("live.model.id must be a bounded model identifier")
            elif name == "prompt":
                if not isinstance(item, str) or len(item) > 12000 or "\x00" in item:
                    raise ValueError(f"live.{name} must be a bounded string")
                try:
                    if len(item.encode("utf-8")) > 24000:
                        raise ValueError(f"live.{name} exceeds the UTF-8 size limit")
                except UnicodeError:
                    raise ValueError(f"live.{name} must contain valid Unicode text") from None
            elif name == "backend_model":
                if not isinstance(item, str) or not _MODEL.fullmatch(item):
                    raise ValueError("live.backend_model must be a bounded model identifier")
            else:
                choices = {
                    "voice": LIVE_VOICES,
                    "context_mode": frozenset({"recent", "summary", "none"}),
                    "backend_mode": frozenset({"assistant", "hosted"}),
                }[name]
                if not isinstance(item, str) or item not in choices:
                    raise ValueError(f"Invalid live.{name}")
            selected[name] = item
        return cls(tuple(sorted(selected.items())))

    def selected(self) -> dict[str, object]:
        return {name: value.as_dict() if isinstance(value, ModelBinding) else value for name, value in self.fields}
