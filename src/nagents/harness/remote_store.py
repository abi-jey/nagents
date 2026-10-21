"""Revisioned JSON remote agents in the host's trusted workspace settings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from .config import AgentProfile
from .private_store import ProtectedFileStore
from .private_store import ProtectedStoreError
from .providers import _exclusive_lock
from .remote_profiles import RemoteProfile

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class RemoteAgentCatalog:
    agents: dict[str, AgentProfile]
    revision: str
    trusted_connections: frozenset[str] = frozenset()
    advertised: dict[str, RemoteAgentMetadata] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteAgentMetadata:
    fingerprint: str
    name: str
    description: str
    observed_at: float


@dataclass(frozen=True)
class RemoteMetadataCatalog:
    entries: dict[str, RemoteAgentMetadata]
    revision: str


class RemoteMetadataStore(ProtectedFileStore):
    """Protected remote AgentCard labels, separate from connection settings."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            filename="a2a-metadata.json",
            subject="Remote agent metadata",
            error=ProtectedStoreError,
            malformed="Remote agent metadata could not be loaded.",
            max_bytes=1_048_576,
        )

    def load(self) -> RemoteMetadataCatalog:
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
        except FileNotFoundError:
            raw = None
        revision = hashlib.sha256(raw).hexdigest() if raw is not None else "0" * 64
        try:
            document = json.loads(raw) if raw is not None else {"version": 1, "agents": {}}
            if (
                not isinstance(document, dict)
                or set(document) != {"version", "agents"}
                or type(document["version"]) is not int
                or document["version"] != 1
                or not isinstance(document["agents"], dict)
                or len(document["agents"]) > 256
            ):
                raise ValueError("Invalid remote metadata document")
            entries: dict[str, RemoteAgentMetadata] = {}
            for alias, value in document["agents"].items():
                if (
                    not isinstance(alias, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", alias)
                    or not isinstance(value, dict)
                    or set(value) != {"fingerprint", "name", "description", "observed_at"}
                ):
                    raise ValueError("Invalid remote metadata entry")
                fingerprint, name, description, observed_at = (
                    value["fingerprint"],
                    value["name"],
                    value["description"],
                    value["observed_at"],
                )
                if (
                    not isinstance(fingerprint, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                    or not isinstance(name, str)
                    or len(name) > 200
                    or not isinstance(description, str)
                    or len(description) > 2000
                    or type(observed_at) not in {int, float}
                    or not math.isfinite(observed_at)
                    or observed_at < 0
                ):
                    raise ValueError("Invalid remote metadata fields")
                entries[alias] = RemoteAgentMetadata(fingerprint, name, description, float(observed_at))
            return RemoteMetadataCatalog(entries, revision)
        except (TypeError, ValueError, UnicodeError):
            raise ProtectedStoreError("Remote agent metadata is invalid.") from None

    def save(self, entries: dict[str, RemoteAgentMetadata], *, expected: str) -> RemoteMetadataCatalog:
        with self._directory(create=True) as directory:
            descriptor = os.open(".a2a-metadata.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
                current = self.load()
                if current.revision != expected:
                    raise ValueError("Remote metadata changed. Reload before saving.")
                if len(entries) > 256:
                    raise ValueError("Remote metadata exceeds the entry limit")
                values: dict[str, dict[str, object]] = {}
                for alias, entry in entries.items():
                    if (
                        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", alias)
                        or not re.fullmatch(r"[0-9a-f]{64}", entry.fingerprint)
                        or len(entry.name) > 200
                        or len(entry.description) > 2000
                        or not math.isfinite(entry.observed_at)
                        or entry.observed_at < 0
                    ):
                        raise ValueError("Invalid remote metadata entry")
                    values[alias] = {
                        "fingerprint": entry.fingerprint,
                        "name": entry.name,
                        "description": entry.description,
                        "observed_at": entry.observed_at,
                    }
                raw = (json.dumps({"version": 1, "agents": values}, indent=2) + "\n").encode()
                self._write_bytes(raw, prefix=".a2a-metadata-")
        return self.load()


class RemoteAgentStore(ProtectedFileStore):
    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            filename="a2a.json",
            subject="Remote agents",
            error=ProtectedStoreError,
            malformed="Remote agent JSON could not be loaded.",
            max_bytes=262144,
        )
        self.metadata_store = RemoteMetadataStore(self.path.with_name("a2a-metadata.json"))

    def load(self) -> RemoteAgentCatalog:
        try:
            self.path.lstat()
        except FileNotFoundError:
            return RemoteAgentCatalog({}, "0" * 64)
        try:
            with self._directory() as directory:
                raw = self._read_bytes(directory)
        except FileNotFoundError:
            raw = None
        revision = hashlib.sha256(raw).hexdigest() if raw is not None else "0" * 64
        try:
            document = json.loads(raw) if raw is not None else {"version": 1, "agents": {}}
            if (
                not isinstance(document, dict)
                or set(document) != {"version", "agents"}
                or type(document["version"]) is not int
                or document["version"] != 1
            ):
                raise ValueError("Remote agent JSON requires version: 1 and agents")
            agents = document["agents"]
            if not isinstance(agents, dict) or len(agents) > 256:
                raise ValueError("Remote agents must be a bounded mapping")
            profiles: dict[str, AgentProfile] = {}
            trusted_connections: set[str] = set()
            for name, fields in agents.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or name == "assistant":
                    raise ValueError("Invalid remote agent name")
                if not isinstance(fields, dict) or fields.keys() - {
                    "description",
                    "connection",
                    "user_invocable",
                    "subagent_available",
                    "trust_remote_tools",
                }:
                    raise ValueError("Invalid remote agent fields")
                trust_remote_tools = fields.get("trust_remote_tools", False)
                if type(trust_remote_tools) is not bool:
                    raise ValueError("Remote tool trust must be a boolean")
                profile = AgentProfile(
                    description=fields.get("description", ""),
                    a2a=RemoteProfile.parse(fields.get("connection", {})),
                    user_invocable=fields.get("user_invocable", True),
                    subagent_available=fields.get("subagent_available", True),
                    source=str(self.path),
                    source_format="a2a",
                )
                profile.validate()
                if not profile.a2a.configured:
                    raise ValueError("Remote agent requires a connection URL")
                profiles[name] = profile
                if trust_remote_tools:
                    trusted_connections.add(profile.a2a.fingerprint)
            try:
                metadata = self.metadata_store.load()
            except (OSError, ProtectedStoreError):
                # Presentation metadata is disposable; unsafe/corrupt cache data
                # must not make the configured connection unavailable.
                metadata = RemoteMetadataCatalog({}, "0" * 64)
            advertised = {
                name: item
                for name, item in metadata.entries.items()
                if name in profiles and item.fingerprint == profiles[name].a2a.fingerprint
            }
            return RemoteAgentCatalog(profiles, revision, frozenset(trusted_connections), advertised)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Remote agent JSON is invalid; check its names and connection fields") from None

    def save(
        self,
        agents: dict[str, AgentProfile],
        *,
        expected: str,
        trust_updates: tuple[tuple[str, bool], ...] = (),
    ) -> RemoteAgentCatalog:
        with self._directory(create=True) as directory:
            descriptor = os.open(".a2a.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
                current = self.load()
                if current.revision != expected:
                    raise ValueError("Remote agents changed. Reload before saving.")
                # Reuse parsing validation before installing any bytes.
                for name, profile in agents.items():
                    if (
                        name == "assistant"
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name)
                        or not profile.a2a.configured
                    ):
                        raise ValueError("Invalid remote agent")
                    profile.validate()
                updates: dict[str, bool] = {}
                for name, trusted in trust_updates:
                    if type(trusted) is not bool:
                        raise ValueError("Remote tool trust updates must be booleans")
                    if name in updates or name not in agents:
                        raise ValueError("Invalid remote tool trust update")
                    updates[name] = trusted
                fingerprints = {profile.a2a.fingerprint for profile in agents.values()}
                trusted_connections = set(current.trusted_connections) & fingerprints
                for name, trusted in updates.items():
                    fingerprint = agents[name].a2a.fingerprint
                    if trusted:
                        trusted_connections.add(fingerprint)
                    else:
                        trusted_connections.discard(fingerprint)
                values = {
                    name: {
                        "description": profile.description,
                        "connection": profile.a2a.as_dict(),
                        "user_invocable": profile.user_invocable,
                        "subagent_available": profile.subagent_available,
                        "trust_remote_tools": profile.a2a.fingerprint in trusted_connections,
                    }
                    for name, profile in agents.items()
                }
                raw = (json.dumps({"version": 1, "agents": values}, indent=2) + "\n").encode()
                if len(raw) > 262144:
                    raise ValueError("Remote agent JSON exceeds 256 KiB")
                self._write_bytes(raw, prefix=".a2a-")
        return self.load()

    def update_metadata(self, fingerprint: str, remote_name: str, description: str) -> bool:
        """Store card labels for every currently configured alias with this binding."""
        if len(remote_name) > 200 or len(description) > 2000:
            raise ValueError("Remote AgentCard metadata exceeds supported limits")
        with self._directory(create=True) as directory:
            descriptor = os.open(".a2a.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
                catalog = self.load()
                aliases = [alias for alias, item in catalog.agents.items() if item.a2a.fingerprint == fingerprint]
                if not aliases:
                    return False
                current = self.metadata_store.load()
                entries = {
                    alias: item
                    for alias, item in current.entries.items()
                    if alias in catalog.agents and item.fingerprint == catalog.agents[alias].a2a.fingerprint
                }
                observed = RemoteAgentMetadata(fingerprint, remote_name, description, time.time())
                for alias in aliases:
                    entries[alias] = observed
                self.metadata_store.save(entries, expected=current.revision)
        return True
