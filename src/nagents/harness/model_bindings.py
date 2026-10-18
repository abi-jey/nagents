"""Data-only model references and revisioned per-agent model preferences."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Literal

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping

    from .providers import ProviderProfile

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_AGENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_EMPTY_REVISION = "0" * 64
_MAX_BYTES = 65536
_EMPTY_MAPPING: Mapping[str, str] = MappingProxyType({})
_DOCUMENT_FIELDS = frozenset(
    {
        "name",
        "description",
        "mode",
        "model",
        "live",
        "prompt",
        "tools",
        "denied_tools",
        "allowed_agents",
        "user_invocable",
        "subagent_available",
        "reasoning_effort",
        "auto_update",
        "default_revision",
    }
)
ModelLayer = Literal["invocation", "agent", "workspace", "global", "builtin"]


class _AgentDumper(yaml.SafeDumper):
    """Keep multiline prompts readable and directly editable as Markdown YAML."""


def _text_block(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_AgentDumper.add_representer(str, _text_block)


@dataclass(frozen=True)
class ModelBinding:
    """A model reference; only an effective builtin may have an unnamed route."""

    provider: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not isinstance(self.id, str):
            raise ValueError("Model provider and id must be strings")
        if not self.provider and not self.id:
            return
        if self.provider and not _PROVIDER.fullmatch(self.provider):
            raise ValueError("Model provider must name a configured provider connection")
        if not self.id or self.id != self.id.strip() or len(self.id) > 200 or not self.id.isprintable():
            raise ValueError("Model id must be a nonempty, bounded identifier")

    @property
    def configured(self) -> bool:
        return bool(self.id)

    @classmethod
    def parse(cls, value: object) -> ModelBinding:
        if not isinstance(value, dict) or set(value) != {"provider", "id"}:
            raise ValueError("Model must contain exactly provider and id")
        provider, identifier = value["provider"], value["id"]
        if not isinstance(provider, str) or not isinstance(identifier, str):
            raise ValueError("Model provider and id must be strings")
        binding = cls(provider, identifier)
        if not binding.configured:
            raise ValueError("Omit the model preference to inherit defaults")
        if not binding.provider:
            raise ValueError("Model provider must name a configured provider connection")
        return binding

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "id": self.id}


_EMPTY_BINDING = ModelBinding()


@dataclass(frozen=True)
class ResolvedModelBinding:
    binding: ModelBinding
    layer: ModelLayer
    source: str = ""
    provider_origin: str = ""


def resolve_model_binding(
    *,
    providers: Mapping[str, ProviderProfile],
    invocation: ModelBinding = _EMPTY_BINDING,
    agent: ModelBinding = _EMPTY_BINDING,
    workspace: ModelBinding = _EMPTY_BINDING,
    global_default: ModelBinding = _EMPTY_BINDING,
    builtin: ModelBinding = _EMPTY_BINDING,
    origins: Mapping[str, str] = _EMPTY_MAPPING,
    sources: Mapping[str, str] = _EMPTY_MAPPING,
    require_available: bool = True,
) -> ResolvedModelBinding:
    """Select one complete pair; an unavailable reference never falls through."""
    layers: tuple[tuple[ModelLayer, ModelBinding], ...] = (
        ("invocation", invocation),
        ("agent", agent),
        ("workspace", workspace),
        ("global", global_default),
        ("builtin", builtin),
    )
    for layer, binding in layers:
        if binding.configured:
            if (
                require_available
                and binding.provider not in providers
                and (binding.provider or layer in {"invocation", "agent"})
            ):
                raise ValueError(f"Model references unavailable provider connection {binding.provider!r}")
            return ResolvedModelBinding(binding, layer, sources.get(layer, ""), origins.get(binding.provider, ""))
    return ResolvedModelBinding(ModelBinding(), "builtin", sources.get("builtin", ""))


@dataclass(frozen=True)
class AgentModelPreference:
    agent: str
    binding: ModelBinding
    revision: str
    path: Path
    model_present: bool = False
    preferences: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


class AgentModelStore:
    """Revisioned Markdown agent overlays; writes preserve unrelated sections.

    Tool selections remain restrictions under the harness permission ceiling.
    Overlays cannot define providers, credentials, hooks, or permissions.
    """

    def __init__(self, directory: Path, *, disable_auto_update: bool = False) -> None:
        self.directory = Path(os.path.abspath(directory))
        self.disable_auto_update = disable_auto_update

    def _path(self, agent: str) -> Path:
        if not isinstance(agent, str) or not _AGENT.fullmatch(agent):
            raise ValueError("Invalid agent name")
        return self.directory / f"{agent}.md"

    def _check_directory(self) -> None:
        if any(path.is_symlink() for path in (self.directory, *self.directory.parents)):
            raise ValueError("Agent model preferences must not use symlinks")

    def _read(self, path: Path) -> tuple[bool, bytes]:
        self._check_directory()
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                raise ValueError("Agent model preference must be a bounded regular file")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return False, b""
        except OSError:
            raise ValueError("Agent model preference is unreadable") from None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                raise ValueError("Agent model preference must be a bounded regular file")
            result = stream.read(_MAX_BYTES + 1)
        if len(result) > _MAX_BYTES:
            raise ValueError("Agent model preference is too large")
        return True, result

    @staticmethod
    def _validate(value: object) -> dict[str, object]:
        from .live_profiles import LiveProfile

        if not isinstance(value, dict) or set(value) - _DOCUMENT_FIELDS:
            raise ValueError("Agent preference contains unsupported fields")
        result: dict[str, object] = {}
        for name, item in value.items():
            if name == "model":
                binding = ModelBinding.parse(item)
                result[name] = binding.as_dict()
            elif name == "live":
                if isinstance(item, dict) and "instructions" in item:
                    raise ValueError("Use live.prompt for the full Voice prompt")
                result[name] = LiveProfile.parse(item).selected()
            elif name == "prompt":
                if (
                    not isinstance(item, str)
                    or len(item) > 24000
                    or len(item.encode("utf-8")) > 48000
                    or "\x00" in item
                ):
                    raise ValueError("Agent instructions must be a bounded string")
                result[name] = item
            elif name in {"tools", "denied_tools", "allowed_agents"}:
                if (
                    not isinstance(item, list)
                    or len(item) > 512
                    or any(
                        not isinstance(tool, str)
                        or not re.fullmatch(
                            r"[A-Za-z0-9_-]{1,128}" if name == "allowed_agents" else r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}",
                            tool,
                        )
                        for tool in item
                    )
                ):
                    raise ValueError("Agent tools must be a bounded array of tool names")
                result[name] = list(dict.fromkeys(item))
            elif name in {"auto_update", "user_invocable", "subagent_available"}:
                if type(item) is not bool:
                    raise ValueError(f"{name} must be a boolean")
                result[name] = item
            elif name == "default_revision":
                if not isinstance(item, str) or not _REVISION.fullmatch(item):
                    raise ValueError("default_revision must be a SHA-256 revision")
                result[name] = item
            elif name == "name":
                if not isinstance(item, str) or not _AGENT.fullmatch(item):
                    raise ValueError("Invalid agent name")
                result[name] = item
            elif name == "mode":
                if item not in {"build", "reviewer"}:
                    raise ValueError("Invalid agent mode")
                result[name] = item
            elif name == "reasoning_effort":
                from .reasoning import resolve_effort

                if not isinstance(item, str):
                    raise ValueError("reasoning_effort must be a string")
                resolve_effort(item)
                result[name] = item
            elif name == "description":
                if not isinstance(item, str) or len(item) > 8192 or "\x00" in item:
                    raise ValueError("Invalid agent description")
                result[name] = item
        return result

    @staticmethod
    def _preferences(raw: bytes) -> dict[str, object]:
        # Re-read files at every boundary, but parse identical content only once.
        # Each caller owns its nested values; cached settings must never be mutated.
        return deepcopy(AgentModelStore._parsed_preferences(raw))

    @staticmethod
    @lru_cache(maxsize=128)
    def _parsed_preferences(raw: bytes) -> dict[str, object]:
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            raise ValueError("Agent preference must contain UTF-8 text") from None
        if not lines or lines[0].strip() != "---":
            raise ValueError("Agent preference requires Markdown frontmatter")
        closing = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip() == "---"), 0)
        if not closing or any(line.strip() for line in lines[closing + 1 :]):
            raise ValueError("Agent preference must contain only frontmatter")
        header = "\n".join(lines[1:closing])
        try:
            for token in yaml.scan(header):
                if isinstance(token, yaml.tokens.AliasToken | yaml.tokens.AnchorToken | yaml.tokens.TagToken):
                    raise ValueError("Agent preference does not support YAML aliases, anchors or tags")
            depth = 0
            for event in yaml.parse(header, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.events.CollectionStartEvent):
                    depth += 1
                    if depth > 3:
                        raise ValueError("Agent preference is too deeply nested")
                elif isinstance(event, yaml.events.CollectionEndEvent):
                    depth -= 1
            node = yaml.compose(header, Loader=yaml.SafeLoader)

            def unique(node: yaml.Node) -> None:
                if isinstance(node, yaml.MappingNode):
                    seen: set[str] = set()
                    for key, child in node.value:
                        if (
                            not isinstance(key, yaml.ScalarNode)
                            or key.tag != "tag:yaml.org,2002:str"
                            or key.value in seen
                        ):
                            raise ValueError("Agent preference fields must be unique strings")
                        seen.add(key.value)
                        unique(child)
                elif isinstance(node, yaml.SequenceNode):
                    for child in node.value:
                        unique(child)

            if node is not None:
                unique(node)
            parsed = yaml.safe_load(header)
            return AgentModelStore._validate({} if parsed is None else parsed)
        except (yaml.YAMLError, RecursionError):
            raise ValueError("Invalid agent preference frontmatter") from None

    def load(self, agent: str) -> AgentModelPreference:
        path = self._path(agent)
        exists, raw = self._read(path)
        if not exists:
            return AgentModelPreference(agent, ModelBinding(), _EMPTY_REVISION, path)
        preferences = self._preferences(raw)
        if "name" in preferences and preferences["name"] != agent:
            raise ValueError("Agent name must match its Markdown filename")
        model = preferences.get("model", {"provider": "", "id": ""})
        binding = ModelBinding() if model == {"provider": "", "id": ""} else ModelBinding.parse(model)
        return AgentModelPreference(
            agent, binding, hashlib.sha256(raw).hexdigest(), path, "model" in preferences, MappingProxyType(preferences)
        )

    def save(self, agent: str, binding: ModelBinding, *, expected: str) -> AgentModelPreference:
        """Save an explicit model choice without replacing other preferences."""
        if binding.configured and not binding.provider:
            raise ValueError("Saved agent models must name a configured provider connection")
        return (
            self.patch(agent, {"model": binding.as_dict()}, expected=expected)
            if binding.configured
            else self.reset(agent, expected=expected)
        )

    def reset(self, agent: str, *, expected: str) -> AgentModelPreference:
        """Remove only the model override, revealing the source definition."""
        return self.remove(agent, ("model",), expected=expected)

    def patch(self, agent: str, changes: Mapping[str, object], *, expected: str) -> AgentModelPreference:
        return self._change(agent, self._validate(dict(changes)), (), expected=expected)

    def remove(self, agent: str, fields: Iterable[str], *, expected: str) -> AgentModelPreference:
        selected = tuple(fields)
        if set(selected) - _DOCUMENT_FIELDS:
            raise ValueError("Unsupported agent preference fields")
        return self._change(agent, {}, selected, expected=expected)

    def replace_fields(
        self, agent: str, fields: Iterable[str], values: Mapping[str, object], *, expected: str
    ) -> AgentModelPreference:
        """Atomically replace one section group, preserving all other preferences."""
        selected = tuple(fields)
        if set(selected) - _DOCUMENT_FIELDS or set(values) - set(selected):
            raise ValueError("Unsupported agent preference fields")
        return self._change(
            agent,
            self._validate(dict(values)),
            tuple(name for name in selected if name not in values),
            expected=expected,
        )

    def patch_live(
        self, agent: str, changes: Mapping[str, object], *, removed: Iterable[str] = (), expected: str
    ) -> AgentModelPreference:
        """Change Live fields under the same whole-document CAS as every editor."""
        current = self.load(agent)
        live = current.preferences.get("live", {})
        if not isinstance(live, dict):
            raise ValueError("Invalid Live profile")
        selected = {**live, **changes}
        for key in removed:
            selected.pop(key, None)
        return (
            self.patch(agent, {"live": selected}, expected=expected)
            if selected
            else self.remove(agent, ("live",), expected=expected)
        )

    def _change(
        self, agent: str, changes: Mapping[str, object], removed: tuple[str, ...], *, expected: str
    ) -> AgentModelPreference:
        from .providers import _exclusive_lock

        path = self._path(agent)
        if not isinstance(expected, str) or not _REVISION.fullmatch(expected):
            raise ValueError("Invalid agent model revision")
        self._check_directory()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_directory()
        lock = self.directory / ".models.lock"
        if lock.is_symlink() or (lock.exists() and not lock.is_file()):
            raise ValueError("Agent model lock must be a regular file")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError:
            raise ValueError("Agent model lock is unavailable") from None
        with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
            current = self.load(agent)
            if current.revision != expected:
                raise ValueError("Agent model changed. Refresh before saving.")
            preferences = dict(current.preferences)
            preferences.update(changes)
            for name in removed:
                preferences.pop(name, None)
            if preferences == current.preferences:
                return current
            if self.disable_auto_update:
                preferences["auto_update"] = False
            if not preferences:
                path.unlink(missing_ok=True)
                self._sync_directory()
                return self.load(agent)
            payload = (
                "---\n" + yaml.dump(preferences, Dumper=_AgentDumper, sort_keys=False, allow_unicode=True) + "---\n"
            ).encode()
            if len(payload) > _MAX_BYTES:
                raise ValueError("Agent preference exceeds the storage limit")
            descriptor, temporary = tempfile.mkstemp(prefix=".model-", dir=self.directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    if os.name != "nt":
                        os.fchmod(output.fileno(), 0o600)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                self._sync_directory()
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self.load(agent)

    def _sync_directory(self) -> None:
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
