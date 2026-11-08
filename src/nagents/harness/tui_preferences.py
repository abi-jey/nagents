"""Non-secret terminal preferences, independent of project configuration."""

import json
import os
import stat
import tempfile
from pathlib import Path

from .private_store import ProtectedStoreError
from .providers.registry import _exclusive_lock

MAX_BYTES = 256 * 1024


class TuiPreferenceStore:
    """Bounded, atomic JSON in ordinary user-owned configuration directories.

    Unlike credentials, preferences do not require private directory modes or
    POSIX. Existing directories are never chmodded. ProtectedStoreError remains
    the public storage-error type for compatibility with terminal callers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()

    def _check_directories(self) -> None:
        for directory in reversed(self.path.parent.parents):
            self._check_directory(directory)
        self._check_directory(self.path.parent)

    @staticmethod
    def _check_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ProtectedStoreError("Terminal preference directories must not be symlinks.")
        if os.name == "posix":
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky_root):
                raise ProtectedStoreError("Terminal preference directories must not be writable by other users.")

    @staticmethod
    def _check_file(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ProtectedStoreError("Terminal preferences require a regular, bounded, unlinked file.")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ProtectedStoreError("Terminal preference files must be owned and not writable by other users.")

    def load(self) -> dict[str, object]:
        # An unused preference location must not restrict normal config loading.
        if not self.path.exists() and not self.path.is_symlink():
            return {}
        self._check_directories()
        self._check_file(self.path)
        try:
            with self.path.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {}
        if len(raw) > MAX_BYTES:
            raise ProtectedStoreError("Terminal preferences exceed their size limit.")
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProtectedStoreError("Terminal preferences are invalid.") from None
        if not isinstance(document, dict):
            raise ProtectedStoreError("Terminal preferences must contain an object.")
        if "tui_performance" in document and type(document["tui_performance"]) is not bool:
            raise ProtectedStoreError("tui_performance must be a boolean.")
        return document

    def save_performance(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("tui_performance must be a boolean")
        self._check_directories()
        directory = self.path.parent
        for parent in (*reversed(directory.parents), directory):
            parent.mkdir(mode=0o700, exist_ok=True)
        self._check_directories()
        lock_path = directory / ".tui.lock"
        self._check_file(lock_path)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "a+b") as lock, _exclusive_lock(lock):
            document = self.load()
            document["tui_performance"] = enabled
            payload = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            if len(payload) > MAX_BYTES:
                raise ProtectedStoreError("Terminal preferences exceed their size limit.")
            descriptor, temporary = tempfile.mkstemp(prefix=".tui-", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                self._check_directories()
                self._check_file(self.path)
                os.replace(temporary, self.path)
                if os.name == "posix":
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
