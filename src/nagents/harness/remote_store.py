"""Revisioned JSON remote agents in the host's trusted workspace settings."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
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
            return RemoteAgentCatalog(profiles, revision, frozenset(trusted_connections))
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
