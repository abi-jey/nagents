"""Voice prompt editing backed by the shared assistant Markdown documents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from nagents.harness.agents.documents import agent_document_store

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.model_bindings import AgentModelStore
    from nagents.harness.providers import ScopedProviderRegistryStore

InstructionScope = Literal["global", "workspace", "agent"]
MAX_INSTRUCTION_BYTES = 24000
MAX_INSTRUCTION_CHARACTERS = 12000
EMPTY_REVISION = "0" * 64


def validate_instruction_text(text: str) -> str:
    try:
        valid = len(text) <= MAX_INSTRUCTION_CHARACTERS and len(text.encode("utf-8")) <= MAX_INSTRUCTION_BYTES
    except UnicodeError:
        valid = False
    if not valid or "\x00" in text:
        raise ValueError("Voice instructions require valid text within 12,000 characters and 24,000 UTF-8 bytes.")
    return text


@dataclass(frozen=True)
class InstructionPreference:
    path: Path
    text: str = ""
    revision: str = EMPTY_REVISION
    present: bool = False


class VoiceInstructionStore:
    """Edit live.prompt using the complete document's CAS revision."""

    def __init__(self, providers: ScopedProviderRegistryStore) -> None:
        self.providers = providers

    @staticmethod
    def name(scope: InstructionScope, agent: str = "") -> str:
        if scope == "agent":
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", agent):
                raise ValueError("Choose a valid agent for Voice instructions.")
            return agent
        if scope not in {"global", "workspace"}:
            raise ValueError("Choose a valid Voice instruction scope.")
        return "assistant"

    def document_store(self, scope: InstructionScope) -> AgentModelStore:
        return agent_document_store(self.providers, scope)

    def path(self, scope: InstructionScope, agent: str = "") -> Path:
        return self.document_store(scope).directory / f"{self.name(scope, agent)}.md"

    def load(self, scope: InstructionScope, agent: str = "") -> InstructionPreference:
        current = self.document_store(scope).load(self.name(scope, agent))
        live = current.preferences.get("live", {})
        assert isinstance(live, dict)
        text = live.get("prompt", "")
        assert isinstance(text, str)
        return InstructionPreference(current.path, text, current.revision, "prompt" in live)

    def save(
        self, scope: InstructionScope, agent: str, prompt: str, *, present: bool, expected: str
    ) -> InstructionPreference:
        validate_instruction_text(prompt)
        self.document_store(scope).patch_live(
            self.name(scope, agent),
            {"prompt": prompt} if present else {},
            removed=() if present else ("prompt",),
            expected=expected,
        )
        return self.load(scope, agent)
