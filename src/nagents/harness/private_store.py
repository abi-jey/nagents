"""POSIX-hardened private file storage shared by ngn credential stores.

The store is plaintext protected by POSIX ownership and mode checks (directory
700, file 600), not a keyring. Every open refuses symlinks, hard links,
irregular files, foreign ownership, and group/other access. Writes replace the
destination atomically from a temporary file inside the protected directory.
Callers own serialization and format validation; this module owns only the
filesystem boundary, so an unsafe or unreadable store never yields contents.
"""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class ProtectedStoreError(RuntimeError):
    """Safe, human-readable storage errors without file contents."""


class ProtectedFileStore:
    """One protected file below ``$XDG_DATA_HOME/ngn/auth`` by default."""

    def __init__(
        self,
        path: Path | None,
        *,
        filename: str,
        subject: str,
        error: type[ProtectedStoreError],
        malformed: str,
        max_bytes: int = 256 * 1024,
    ) -> None:
        if path is None:
            data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
            if not data_home.is_absolute():
                raise error("XDG_DATA_HOME must be an absolute path for credential storage.")
            path = data_home / "ngn" / "auth" / filename
        self.path = path.expanduser().absolute()
        self._error = error
        self._malformed = malformed
        self._max_bytes = max_bytes
        self._unsafe = (
            f"{subject} credential storage is unsafe; use an owned private directory (700) and regular file (600)."
        )
        self._save_failed = f"{subject} login could not be saved securely; check private storage permissions."
        self._remove_failed = f"{subject} login could not be removed safely; check private storage permissions."
        self._posix_only = f"Protected {subject} credential storage currently requires POSIX."

    @contextmanager
    def _directory(self, create: bool = False) -> Iterator[int]:
        if os.name != "posix":
            raise self._error(self._posix_only)
        descriptor = os.open(self.path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.path.parent.parts[1:]:
                if part in {".", ".."}:
                    raise self._error(self._unsafe)
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                writable = stat.S_IMODE(info.st_mode) & 0o022
                trusted_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
                if info.st_uid not in {0, os.getuid()} or (writable and not trusted_sticky):
                    raise self._error(self._unsafe)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise self._error(self._unsafe)
            yield descriptor
        finally:
            os.close(descriptor)

    def _read_bytes(self, directory: int) -> bytes | None:
        try:
            descriptor = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > self._max_bytes
            ):
                raise self._error(self._unsafe)
            data = file.read(self._max_bytes + 1)
            if len(data) > self._max_bytes:
                raise self._error(self._malformed)
            return data

    def _write_bytes(self, payload: bytes, *, prefix: str) -> None:
        temporary = f"{prefix}{secrets.token_hex(12)}.tmp"
        try:
            with self._directory(create=True) as directory:
                self._read_bytes(directory)  # Refuse unsafe existing destinations before replacement.
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
                )
                try:
                    with os.fdopen(descriptor, "wb") as file:
                        file.write(payload)
                        file.flush()
                        os.fsync(file.fileno())
                    self._read_bytes(directory)
                    os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=directory)
        except OSError:
            raise self._error(self._save_failed) from None

    def _unlink(self) -> None:
        try:
            with self._directory() as directory:
                if self._read_bytes(directory) is not None:
                    os.unlink(self.path.name, dir_fd=directory)
                    os.fsync(directory)
        except FileNotFoundError:
            pass
        except OSError:
            raise self._error(self._remove_failed) from None
