"""Resolve Markdown instruction scopes without rewriting older Voice settings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .voice_instructions import default_voice_instructions
from .voice_instructions import default_voice_instructions_path
from .voice_instructions import legacy_voice_instructions

if TYPE_CHECKING:
    from .voice_instruction_store import InstructionMode
    from .voice_instruction_store import InstructionPreference
    from .voice_instruction_store import InstructionScope
    from .voice_instruction_store import VoiceInstructionStore


@dataclass(frozen=True)
class LegacyInstructions:
    text: str = ""
    present: bool = False
    source: str = ""
    path: str = ""


@dataclass(frozen=True)
class ResolvedInstructions:
    scope: InstructionScope
    agent: str
    revision: str
    preference: InstructionPreference
    mode: InstructionMode
    text: str
    source: str
    source_path: str
    inherited: str
    inherited_source: str
    builtin: str

    def snapshot(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "agent": self.agent,
            "revision": self.revision,
            "mode": self.mode,
            "present": self.preference.present,
            "text": self.text,
            "source": self.source,
            "source_path": self.source_path,
            "inherited": self.inherited,
            "inherited_source": self.inherited_source,
            "preferences_path": str(self.preference.path),
            "builtin": self.builtin,
            "applies": "next_voice_conversation",
        }


def resolve_instructions(
    store: VoiceInstructionStore,
    scope: InstructionScope,
    agent: str,
    backend_mode: str,
    base_revision: str,
    legacy: tuple[LegacyInstructions, LegacyInstructions, LegacyInstructions],
) -> ResolvedInstructions:
    builtin = default_voice_instructions(backend_mode)
    text, source, source_path = builtin, "builtin", str(default_voice_instructions_path(backend_mode))
    revisions = [scope, agent, base_revision, hashlib.sha256(builtin.encode()).hexdigest()]
    scopes: tuple[InstructionScope, ...] = ("global", "workspace", "agent")
    for layer, previous in zip(scopes, legacy, strict=True):
        inherited, inherited_source = text, source
        preference = store.load(layer, agent)
        revisions.extend((str(preference.path), preference.revision))
        mode: InstructionMode = "inherit"
        if preference.present:
            mode = preference.mode
            if mode == "custom":
                text, source, source_path = preference.text, layer, str(preference.path)
        elif previous.present and (layer != "global" or previous.text.strip()):
            mode = "custom"
            text, source, source_path = (
                legacy_voice_instructions(previous.text, backend_mode),
                previous.source,
                previous.path,
            )
        if layer == scope:
            return ResolvedInstructions(
                scope,
                agent,
                hashlib.sha256(":".join(revisions).encode()).hexdigest(),
                preference,
                mode,
                text,
                source,
                source_path,
                inherited,
                inherited_source,
                builtin,
            )
    raise ValueError("Invalid Voice instruction scope.")
