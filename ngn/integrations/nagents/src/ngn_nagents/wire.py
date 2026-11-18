"""Optional provider observation sink; the runtime never selects the file."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .protocol import ProtocolError

if TYPE_CHECKING:
    from nagents.wire_log import WireLog

CAPABILITY = "provider_wire_log"
PATH_ENV = "NGN_HOST_WIRE_LOG_PATH"
IDENTITY_ENV = "NGN_HOST_WIRE_LOG_ID"


def available() -> bool:
    # Do not require the new public sink for baseline v1 runtimes installed
    # against older versions of nagents.
    try:
        from nagents.wire_log import WireLog
    except ImportError:
        return False
    return WireLog.supported()


def open_host_log() -> WireLog:
    if not available():
        raise ProtocolError("Provider wire logging is not supported by this runtime")
    from nagents.wire_log import WireLog

    path = Path(os.environ.get(PATH_ENV, ""))
    identity = os.environ.get(IDENTITY_ENV, "")
    if (
        not path.is_absolute()
        or path.parent.name != "logs"
        or ".." in path.parts
        or re.fullmatch(r"wire-[0-9a-f]{32}\.jsonl", path.name) is None
        or re.fullmatch(r"[0-9]{1,20}:[0-9]{1,20}", identity) is None
    ):
        raise ProtocolError("Missing or invalid host-owned wire log binding")
    device, inode = identity.split(":")
    try:
        return WireLog.from_existing_file(path, identity=(int(device), int(inode)))
    except (OSError, ValueError):
        raise ProtocolError("Cannot open the private host wire log") from None
