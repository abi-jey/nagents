"""Opt-in provider HTTP payload logs, separate from metadata-only runtime logs."""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC
from datetime import datetime
from stat import S_ISREG
from typing import TYPE_CHECKING

from nagents.observation import json_default
from nagents.observation import observer
from nagents.protected_store import unsafe_ancestor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)
_active_capture: ContextVar[object] = ContextVar("ngn_wire_capture", default=None)


class WireLog:
    """Private JSONL sink for already credential-redacted HTTP observation events.

    Application payloads are deliberately not redacted: they are the purpose of
    this explicitly enabled diagnostic and may contain sensitive user content.
    """

    def __init__(self, data_dir: Path) -> None:
        self._require_posix()
        directory = data_dir / "logs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.path = directory / f"wire-{stamp}-{uuid.uuid4().hex[:12]}.jsonl"
        with self._directory() as directory_fd:
            descriptor = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            try:
                self._identity = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        self._failed = False

    @staticmethod
    def supported() -> bool:
        return os.name == "posix" and all(hasattr(os, name) for name in ("getuid", "O_NOFOLLOW", "O_DIRECTORY"))

    @classmethod
    def _require_posix(cls) -> None:
        if not cls.supported():
            raise ValueError(
                "Wire logging requires POSIX ownership and no-follow protections; disable wire_log on this platform"
            )

    @classmethod
    def from_existing_file(cls, path: Path, *, identity: tuple[int, int]) -> WireLog:
        """Append to a private file allocated by a trusted host, never create it."""
        cls._require_posix()
        sink = cls.__new__(cls)
        sink.path = path
        sink._failed = False
        with sink._open() as descriptor:
            sink._identity = os.fstat(descriptor)
            if (sink._identity.st_dev, sink._identity.st_ino) != identity:
                raise OSError("Wire log identity does not match the host file")
        return sink

    @contextmanager
    def _directory(self) -> Iterator[int]:
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except NotADirectoryError:
            raise ValueError("Wire log directory must be owner-controlled and not a symlink") from None
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or unsafe_ancestor(info):
                raise ValueError("Wire log directory must be owner-controlled and not a symlink")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _open(self) -> Iterator[int]:
        with self._directory() as directory_fd:
            descriptor = os.open(
                self.path.name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            try:
                info = os.fstat(descriptor)
                if (
                    not S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                ):
                    raise OSError("Wire log must be private and singly linked")
                yield descriptor
            finally:
                os.close(descriptor)

    @contextmanager
    def capture(self, session_id: str) -> Iterator[None]:
        previous = observer.get()
        capture_id = object()
        active_token = _active_capture.set(capture_id)

        def receive(kind: str, data: dict[str, object]) -> None:
            if previous is not None:
                previous(kind, data)
            if _active_capture.get() is not capture_id or not kind.startswith("http_") or self._failed:
                return
            entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": session_id,
                "event": kind,
                "data": data,
            }
            try:
                with self._open() as descriptor:
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) != (self._identity.st_dev, self._identity.st_ino):
                        raise OSError("Wire log identity changed")
                    # Many stdio runners may share one host log. Serialize each
                    # append across processes without retaining its descriptor.
                    import fcntl

                    with os.fdopen(os.dup(descriptor), "a", encoding="utf-8") as stream:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                        stream.write(json.dumps(entry, default=json_default, ensure_ascii=False) + "\n")
            except (OSError, ValueError):
                # Diagnostics must not break a request or leak its payload into
                # the ordinary runtime log when disk access fails.
                self._failed = True
                logger.warning("Wire logging stopped: could not write diagnostic file")

        token = observer.set(receive)
        try:
            yield
        finally:
            observer.reset(token)
            _active_capture.reset(active_token)
