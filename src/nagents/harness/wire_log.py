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
        if os.name != "posix" or not hasattr(os, "getuid") or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError(
                "Wire logging requires POSIX ownership and no-follow protections; disable wire_log on this platform"
            )
        directory = data_dir / "logs"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("Wire log directory must be owner-controlled and not a symlink")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.path = directory / f"wire-{stamp}-{uuid.uuid4().hex[:12]}.jsonl"
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._identity = os.fstat(descriptor)
        os.close(descriptor)
        self._failed = False

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
                descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not S_ISREG(info.st_mode)
                        or (info.st_dev, info.st_ino) != (self._identity.st_dev, self._identity.st_ino)
                        or info.st_mode & 0o077
                    ):
                        raise OSError("Wire log identity or permissions changed")
                    stream.write(json.dumps(entry, default=json_default, ensure_ascii=False) + "\n")
            except OSError:
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
