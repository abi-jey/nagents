"""Global and workspace speech settings in assistant Markdown documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Literal

from pydantic import AfterValidator
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nagents.harness.agents.documents import agent_document_store
from nagents.harness.live_profiles import LIVE_FIELDS
from nagents.harness.providers import KINDS
from nagents.live import LiveConfig

from .store import validate_instruction_text

if TYPE_CHECKING:
    from nagents.harness.providers import ScopedProviderRegistryStore

MAX_VOICE_INSTRUCTION_CHARACTERS = 12000
MAX_VOICE_INSTRUCTION_BYTES = 24000
MAX_VOICE_BYTES = 40 * 1024
ModelId = str
Scope = Literal["global", "workspace"]
ContextMode = Literal["recent", "summary", "none"]

VoiceInstructions = Annotated[
    str, Field(max_length=MAX_VOICE_INSTRUCTION_CHARACTERS), AfterValidator(validate_instruction_text)
]


class VoicePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    enabled: bool = False
    connection_id: str = Field(default="", max_length=64, pattern=r"^$|^[a-z][a-z0-9_-]{0,63}$")
    backend_mode: Literal["assistant", "hosted"] = "assistant"
    model: ModelId = Field(
        default="gpt-live-1", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
    )
    backend_model: ModelId = Field(
        default_factory=lambda: LiveConfig().backend_model,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    )
    voice: Literal[
        "marin", "cedar", "arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"
    ] = "sol"
    context_mode: ContextMode = "recent"


class VoiceOverrides(VoicePreferences):
    """Only explicitly supplied fields override the global value."""

    def selected(self) -> dict[str, str | bool]:
        return self.model_dump(exclude_unset=True)


@dataclass(frozen=True)
class VoicePreferenceSnapshot:
    global_values: VoicePreferences
    overrides: VoiceOverrides
    global_revision: str
    local_revision: str


class VoicePreferenceStore:
    """Read speech settings and update them without replacing live.prompt."""

    def __init__(self, providers: ScopedProviderRegistryStore) -> None:
        self.providers = providers

    def to_live(self, values: VoicePreferences, scope: Scope, *, inherited: VoicePreferences) -> dict[str, object]:
        """Translate flattened speech settings to an atomic model binding."""
        selected: dict[str, object] = values.model_dump(exclude_unset=True)
        connection = selected.pop("connection_id", "")
        model = selected.pop("model", "")
        if {"connection_id", "model"} & values.model_fields_set:
            registry = self.providers.load_scope("global") if scope == "global" else self.providers.load()
            name = str(
                connection or registry.active
                if "connection_id" in values.model_fields_set
                else inherited.connection_id or registry.active
            )
            identifier = str(model or inherited.model)
            profile = registry.providers.get(name)
            if profile is not None and KINDS[profile.kind].live:
                selected["model"] = {"provider": name, "id": identifier}
            elif identifier not in {"gpt-live-1", "gpt-live-1-codex"} or connection:
                raise ValueError("Choose an existing GPT-Live provider connection for this model.")
        return selected

    @staticmethod
    def _from_live(live: object, *, workspace: bool) -> VoicePreferences | VoiceOverrides:
        if not isinstance(live, dict):
            raise ValueError("Invalid Voice preferences")
        selected = {key: value for key, value in live.items() if key != "prompt"}
        binding = selected.pop("model", None)
        if binding is not None:
            if not isinstance(binding, dict):
                raise ValueError("Invalid Voice model binding")
            selected.update(connection_id=binding["provider"], model=binding["id"])
        return (VoiceOverrides if workspace else VoicePreferences).model_validate(selected)

    def save(self, scope: Scope, values: VoicePreferences, *, expected: str, inherited: VoicePreferences) -> None:
        live = self.to_live(values, scope, inherited=inherited if scope == "workspace" else VoicePreferences())
        fields = LIVE_FIELDS - {"prompt"}
        agent_document_store(self.providers, scope).patch_live(
            "assistant", live, removed=tuple(sorted(fields - live.keys())), expected=expected
        )

    def snapshot(self, scope: Scope = "workspace") -> VoicePreferenceSnapshot:
        global_document = agent_document_store(self.providers, "global").load("assistant")
        global_values = self._from_live(global_document.preferences.get("live", {}), workspace=False)
        assert isinstance(global_values, VoicePreferences)
        if scope == "global":
            return VoicePreferenceSnapshot(global_values, VoiceOverrides(), global_document.revision, "0" * 64)
        local_document = agent_document_store(self.providers, "workspace").load("assistant")
        overrides = self._from_live(local_document.preferences.get("live", {}), workspace=True)
        assert isinstance(overrides, VoiceOverrides)
        return VoicePreferenceSnapshot(global_values, overrides, global_document.revision, local_document.revision)
