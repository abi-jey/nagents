"""Resolve full Voice prompts through the canonical document hierarchy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..voice.instructions import default_voice_instructions
from ..voice.instructions import default_voice_instructions_path

if TYPE_CHECKING:
    from ..voice.store import InstructionPreference
    from ..voice.store import InstructionScope
    from ..voice.store import VoiceInstructionStore


@dataclass(frozen=True)
class AuthoredPrompt:
    text: str = ""
    present: bool = False
    path: str = ""


_EMPTY_DEFINITION = AuthoredPrompt()


@dataclass(frozen=True)
class ResolvedInstructions:
    scope: InstructionScope
    agent: str
    revision: str
    preference: InstructionPreference
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
    definition: AuthoredPrompt = _EMPTY_DEFINITION,
) -> ResolvedInstructions:
    builtin = default_voice_instructions(backend_mode)
    text, source, source_path = builtin, "builtin", str(default_voice_instructions_path(backend_mode))
    revisions = [scope, agent, base_revision, hashlib.sha256(builtin.encode()).hexdigest()]
    scopes: tuple[InstructionScope, ...] = ("global", "workspace", "agent")
    for layer in scopes:
        if layer == "agent" and definition.present:
            text, source, source_path = definition.text, "definition", definition.path
        inherited, inherited_source = text, source
        preference = store.load(layer, agent)
        revisions.extend((str(preference.path), preference.revision))
        if preference.present:
            text, source, source_path = preference.text, layer, str(preference.path)
        if layer == scope:
            return ResolvedInstructions(
                scope,
                agent,
                hashlib.sha256(":".join(revisions).encode()).hexdigest(),
                preference,
                text,
                source,
                source_path,
                inherited,
                inherited_source,
                builtin,
            )
    raise ValueError("Invalid Voice instruction scope.")
