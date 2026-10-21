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
            for name, fields in agents.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or name == "assistant":
                    raise ValueError("Invalid remote agent name")
                if not isinstance(fields, dict) or fields.keys() - {
                    "description",
                    "connection",
                    "user_invocable",
                    "subagent_available",
                }:
                    raise ValueError("Invalid remote agent fields")
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
            return RemoteAgentCatalog(profiles, revision)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Remote agent JSON is invalid; check its names and connection fields") from None

    def save(self, agents: dict[str, AgentProfile], *, expected: str) -> RemoteAgentCatalog:
        values = {
            name: {
                "description": profile.description,
                "connection": profile.a2a.as_dict(),
                "user_invocable": profile.user_invocable,
                "subagent_available": profile.subagent_available,
            }
            for name, profile in agents.items()
        }
        raw = (json.dumps({"version": 1, "agents": values}, indent=2) + "\n").encode()
        if len(raw) > 262144:
            raise ValueError("Remote agent JSON exceeds 256 KiB")
        with self._directory(create=True) as directory:
            descriptor = os.open(".a2a.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
                if self.load().revision != expected:
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
                self._write_bytes(raw, prefix=".a2a-")
        return self.load()
