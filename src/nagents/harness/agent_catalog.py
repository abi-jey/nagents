"""Reload agent definitions without reloading host settings or granting trust."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import replace

from .agent_definitions import discover_agents
from .agent_definitions import import_agents
from .config import AgentDefinitionSource
from .config import AgentProfile
from .config import HarnessConfig
from .config import _install_agents
from .config import _install_assistant_metadata
from .config import _install_profile
from .config import _json_profile
from .providers import ScopedProviderRegistryStore


class AgentCatalog:
    """One runtime's catalog; accepted model generations keep separate snapshots."""

    def __init__(self, config: HarnessConfig) -> None:
        sources = config.agent_sources or (
            (AgentDefinitionSource("workspace", config.workspace),) if config.trust_project else ()
        )
        providers = ScopedProviderRegistryStore(config.workspace, paths=config.provider_paths)
        if not any(source.kind == "defaults" for source in sources):
            sources = (
                *(
                    AgentDefinitionSource("defaults", providers.model_store(scope).path.parent / "assistant.md")
                    for scope in ("global", "workspace")
                ),
                *sources,
            )
        canonical = providers.agent_models.directory
        self.sources = (
            sources
            if any(source.kind == "native" for source in sources)
            else (
                *sources,
                AgentDefinitionSource("native", canonical),
            )
        )
        # Explicit Python profiles and child routing overrides remain authoritative.
        self.overrides = {
            name: replace(profile)
            for name, profile in config.profiles.items()
            if name not in config.agent_source_profiles or profile != config.agent_source_profiles[name]
        }
        self._published = {name: replace(profile) for name, profile in config.profiles.items()}
        self._removed: set[str] = set()
        self.revision = self._revision(config.profiles, config.agent_diagnostics)

    @staticmethod
    def _revision(profiles: dict[str, AgentProfile], diagnostics: tuple[str, ...]) -> str:
        value = {"profiles": {name: asdict(profile) for name, profile in profiles.items()}, "diagnostics": diagnostics}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def refresh(self, config: HarnessConfig) -> bool:
        for name in self._published.keys() | config.profiles.keys():
            previous, current = self._published.get(name), config.profiles.get(name)
            if previous == current:
                continue
            if current is None:
                self.overrides.pop(name, None)
                self._removed.add(name)
            else:
                self.overrides[name] = replace(current)
                self._removed.discard(name)
        # Construct a profile-only accumulator: never reread providers, model
        # preferences, environment flags, endpoints, plugins or approval policy.
        candidate = HarnessConfig(config.workspace)
        # A programmatic catalog without reloadable sources owns its supplied
        # diagnostics too; there is no discovery pass that can replace them.
        diagnostics: list[str] = [] if self.sources else list(config.agent_diagnostics)
        failed = False
        authored = {name: replace(profile) for name, profile in self.overrides.items()}
        for source in self.sources:
            before = dict(candidate.profiles)
            notes_before = len(diagnostics)
            try:
                if source.kind == "defaults":
                    failed = _install_assistant_metadata(candidate, source.path, diagnostics) or failed
                    continue
                if source.kind == "native":
                    authored = {name: replace(profile) for name, profile in candidate.profiles.items()}
                    if source.path.exists():
                        batch = import_agents([{"path": str(source.path), "format": "native"}], source.path.parent)
                        failed = failed or batch.failed
                        _install_agents(candidate, batch, diagnostics, overlay=True)
                    continue
                if source.kind in {"personal", "workspace"}:
                    batch = discover_agents(source.path, personal=source.kind == "personal")
                    failed = failed or batch.failed
                    _install_agents(candidate, batch, diagnostics)
                    continue
                path = source.path.resolve() if source.resolve else source.path
                try:
                    with path.open(encoding="utf-8") as stream:
                        raw = stream.read(1048577)
                except FileNotFoundError:
                    if source.resolve and source.path.exists():
                        raise
                    continue
                if len(raw.encode()) > 1048576:
                    raise ValueError("Agent configuration exceeds 1 MiB")
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError("Agent configuration must be a mapping")
                if "agent_imports" in document:
                    batch = import_agents(document["agent_imports"], path.parent)
                    failed = failed or batch.failed
                    _install_agents(candidate, batch, diagnostics)
                profiles = document.get("profiles", {})
                if profiles is None:
                    profiles = {}
                if not isinstance(profiles, dict):
                    raise ValueError("Agent profiles must be a mapping")
                for name, fields in profiles.items():
                    _install_profile(candidate, name, _json_profile(fields, path), diagnostics)
                candidate.validate()
            except (OSError, ValueError, RuntimeError, UnicodeError) as error:
                failed = True
                # No invalid document contents or private profile bodies in diagnostics.
                candidate.profiles = before
                del diagnostics[notes_before:]
                diagnostics.append(f"{source.path}: agent definitions could not be loaded ({type(error).__name__})")
        if failed:
            # A partially written or invalid source never replaces an accepted catalog.
            # Diagnostics can change independently; admission uses the accepted revision.
            config.agent_diagnostics = tuple(diagnostics)
            return False
        source_profiles = {name: replace(profile) for name, profile in candidate.profiles.items()}
        candidate.profiles.update({name: replace(profile) for name, profile in self.overrides.items()})
        for name in self._removed:
            candidate.profiles.pop(name, None)
        notes = tuple(diagnostics)
        revision = self._revision(candidate.profiles, notes)
        changed = revision != self.revision
        config.profiles = candidate.profiles
        config.agent_source_profiles = source_profiles
        config.agent_authored_profiles = {**authored, **self.overrides}
        config.agent_diagnostics = notes
        # Source selection itself is immutable and inherited by child runtimes.
        config.agent_sources = self.sources
        self.revision = revision
        self._published = {name: replace(profile) for name, profile in config.profiles.items()}
        return changed
