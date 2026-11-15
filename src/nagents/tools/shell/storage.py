"""Bounded shell output excerpts."""

from __future__ import annotations

import os
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def read_tail(path: Path, *, lines: int, limit: int) -> tuple[str, bool]:
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
    except OSError:
        return "", False
    data = b"".join(data.splitlines(keepends=True)[-lines:])
    text = data.decode("utf-8", "replace")
    text = "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else "?" for c in text)
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore"), True
