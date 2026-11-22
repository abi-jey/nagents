"""Private engine discovery and the single owner of an engine state directory."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from nagents.protected_store import ProtectedFileLock
from nagents.protected_store import ProtectedFileStore
from nagents.protected_store import ProtectedStoreError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

REGISTRATION_NAME = "engine.json"
LOCK_NAME = "engine.lock"


def workspace_state_dir(data_dir: Path, workspace: Path) -> Path:
    """Use the canonical workspace's POSIX path bytes, not a lossy Unicode rendering."""
    key = hashlib.sha256(os.fsencode(workspace.expanduser().resolve())).hexdigest()
    return data_dir / "engine" / "workspaces" / key


@dataclass(frozen=True, slots=True)
class Registration:
    url: str
    token: str = field(repr=False)
    pid: int
    protocol: int

    def encode(self) -> bytes:
        return (json.dumps(asdict(self), separators=(",", ":")) + "\n").encode("ascii")


class EngineStateError(ProtectedStoreError):
    """An unsafe, unreadable or already owned engine state directory."""


class RegistrationStore(ProtectedFileStore):
    def __init__(self, directory: Path) -> None:
        super().__init__(
            directory / REGISTRATION_NAME,
            filename=REGISTRATION_NAME,
            subject="Engine registration",
            error=EngineStateError,
            malformed="Invalid private engine registration.",
            max_bytes=4096,
        )

    def publish(self, registration: Registration) -> None:
        self._write_bytes(registration.encode(), prefix=".engine-")

    def clear(self) -> None:
        self._unlink()

    def check_directory(self) -> None:
        """Reject unsafe existing paths without creating a directory or opening the registration."""
        with suppress(FileNotFoundError), self._directory():
            pass


class _OwnerLock(ProtectedFileLock):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory / LOCK_NAME, subject="Engine state", error=EngineStateError)
        self._busy = "Another Python engine already owns this state directory."


@contextmanager
def own_state(directory: Path) -> Iterator[RegistrationStore]:
    """Do not touch discovery metadata until the owner lock is held."""
    lock = _OwnerLock(directory)
    with lock.try_acquire():
        registration = RegistrationStore(directory)
        registration.clear()  # The prior owner may have been killed.
        try:
            yield registration
        finally:
            registration.clear()
    # Keep the private lock inode: another process may already have opened it.
