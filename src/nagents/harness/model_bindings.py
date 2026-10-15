"""Data-only model references and revisioned per-agent model preferences."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Literal

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .providers import ProviderProfile

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_AGENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_EMPTY_REVISION = "0" * 64
_MAX_BYTES = 16384
_EMPTY_MAPPING: Mapping[str, str] = MappingProxyType({})
ModelLayer = Literal["invocation", "agent", "workspace", "global", "builtin"]


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


class AgentModelStore:
    """Model-only Markdown overlays in the operator's workspace configuration.

    These files cannot define instructions, tools, permissions, or providers.
    They therefore also support the built-in assistant and imported profiles.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(os.path.abspath(directory))

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
    def _binding(raw: bytes) -> ModelBinding:
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            raise ValueError("Agent model preference must contain UTF-8 text") from None
        if not lines or lines[0].strip() != "---":
            raise ValueError("Agent model preference requires Markdown frontmatter")
        closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
        if not closing or any(line.strip() for line in lines[closing + 1 :]):
            raise ValueError("Agent model preference must contain only model frontmatter")
        header = "\n".join(lines[1:closing])
        try:
            for token in yaml.scan(header):
                if isinstance(token, yaml.tokens.AliasToken | yaml.tokens.AnchorToken | yaml.tokens.TagToken):
                    raise ValueError("Agent model preference does not support YAML aliases, anchors or tags")
            depth = 0
            for event in yaml.parse(header, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.events.CollectionStartEvent):
                    depth += 1
                    if depth > 2:
                        raise ValueError("Agent model preference must not contain nested model fields")
                elif isinstance(event, yaml.events.CollectionEndEvent):
                    depth -= 1
            node = yaml.compose(header, Loader=yaml.SafeLoader)
        except (yaml.YAMLError, RecursionError):
            raise ValueError("Invalid agent model frontmatter") from None
        if not isinstance(node, yaml.MappingNode) or len(node.value) != 1:
            raise ValueError("Agent model preference must contain only model")
        key, model = node.value[0]
        if (
            not isinstance(key, yaml.ScalarNode)
            or key.tag != "tag:yaml.org,2002:str"
            or key.value != "model"
            or not isinstance(model, yaml.MappingNode)
        ):
            raise ValueError("Agent model preference must contain a model mapping")
        fields: dict[str, str] = {}
        for key, value in model.value:
            if (
                not isinstance(key, yaml.ScalarNode)
                or key.tag != "tag:yaml.org,2002:str"
                or key.value in fields
                or not isinstance(value, yaml.ScalarNode)
                or value.tag != "tag:yaml.org,2002:str"
            ):
                raise ValueError("Model fields must be unique strings")
            fields[key.value] = value.value
        if fields == {"provider": "", "id": ""}:
            return ModelBinding()
        return ModelBinding.parse(fields)

    def load(self, agent: str) -> AgentModelPreference:
        path = self._path(agent)
        exists, raw = self._read(path)
        if not exists:
            return AgentModelPreference(agent, ModelBinding(), _EMPTY_REVISION, path)
        return AgentModelPreference(agent, self._binding(raw), hashlib.sha256(raw).hexdigest(), path)

    def save(self, agent: str, binding: ModelBinding, *, expected: str) -> AgentModelPreference:
        """Save an explicit choice; an empty pair explicitly inherits defaults."""
        return self._change(agent, binding, expected=expected, remove=False)

    def reset(self, agent: str, *, expected: str) -> AgentModelPreference:
        """Remove the UI override and reveal the agent definition's model choice."""
        return self._change(agent, ModelBinding(), expected=expected, remove=True)

    def _change(self, agent: str, binding: ModelBinding, *, expected: str, remove: bool) -> AgentModelPreference:
        from .providers import _exclusive_lock

        if binding.configured and not binding.provider:
            raise ValueError("Saved agent models must name a configured provider connection")
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
            if remove:
                path.unlink(missing_ok=True)
                self._sync_directory()
                return self.load(agent)
            if current.binding == binding and current.revision != _EMPTY_REVISION:
                return current
            payload = ("---\n" + yaml.safe_dump({"model": binding.as_dict()}, sort_keys=False) + "---\n").encode()
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
