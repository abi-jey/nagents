"""Private timestamped runtime metadata for a terminal launch; payload logging stays off."""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType
    from typing import TextIO

logger = logging.getLogger("nagents.tui")

# This is a closed schema, not a list of fields whose arbitrary values are safe.
# Session IDs and provider/model/tool/version labels are intentionally omitted,
# even when they happen to resemble ordinary identifiers or version numbers.
_LEVELS = {20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL"}
_CATEGORIES = {
    "phase": frozenset(
        {
            "starting",
            "idle",
            "working",
            "tool",
            "delegating",
            "delegated",
            "returned",
            "approval",
            "sleeping",
            "waking",
            "responding",
            "done",
            "error",
            "performance",
        }
    ),
    "status": frozenset({"started", "completed", "failed", "degraded", "recovered"}),
    "metric": frozenset({"loop_lag", "callback"}),
}
_MEASUREMENTS = (
    "duration_ms",
    "threshold_ms",
    "lag_p95_ms",
    "lag_max_ms",
    "cpu_percent",
    "peak_memory_mb",
)
_COUNTS = ("sample_count", "running_tools", "running_agents", "queued_prompts", "backlog_chars")
_EXCEPTIONS: dict[type[BaseException], str] = {
    ValueError: "ValueError",
    TypeError: "TypeError",
    RuntimeError: "RuntimeError",
    OSError: "OSError",
    TimeoutError: "TimeoutError",
    PermissionError: "PermissionError",
    FileNotFoundError: "FileNotFoundError",
    KeyError: "KeyError",
    AttributeError: "AttributeError",
    ImportError: "ImportError",
}


class _Formatter(logging.Formatter):
    def __init__(self, started: float) -> None:
        super().__init__()
        self.started = started

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((monotonic() - self.started) * 1000, 3),
            "level": _LEVELS.get(record.levelno, "OTHER"),
            "logger": "nagents.tui",
            # Never render messages, arguments, stack traces or arbitrary names.
            "message": "TUI runtime event",
        }
        for key, choices in _CATEGORIES.items():
            value: object = record.__dict__.get(key)
            if type(value) is str and value in choices:
                entry[key] = value
        for key in _MEASUREMENTS:
            value = record.__dict__.get(key)
            if (type(value) is int or type(value) is float) and 0 <= value <= 1e12 and math.isfinite(value):
                entry[key] = value
        for key in _COUNTS:
            value = record.__dict__.get(key)
            if type(value) is int and 0 <= value <= 1_000_000_000:
                entry[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            # Class names are arbitrary too (including dynamically named classes).
            # Identity matching preserves common built-in types without trusting names.
            entry["exception_type"] = _EXCEPTIONS.get(record.exc_info[0], "other")
        return json.dumps(entry, ensure_ascii=True, allow_nan=False)


class RuntimeLog:
    def __init__(self, data_dir: Path) -> None:
        directory = data_dir / "logs"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = datetime.now(UTC).strftime("tui-%Y%m%dT%H%M%S") + f"-{uuid4().hex[:8]}.jsonl"
        self.path = directory / name
        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.stream: TextIO = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
        self.started = monotonic()
        formatter = _Formatter(self.started)
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(formatter)
        self._level, self._propagate = logger.level, logger.propagate

    def __enter__(self) -> RuntimeLog:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(self.handler)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        logger.removeHandler(self.handler)
        logger.setLevel(self._level)
        logger.propagate = self._propagate
        self.handler.close()
        self.stream.close()
