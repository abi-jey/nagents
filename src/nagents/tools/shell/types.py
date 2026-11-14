"""Host-independent contracts for managed POSIX shell jobs."""

from __future__ import annotations

import math
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypeAlias

if TYPE_CHECKING:
    from pathlib import Path

ShellState: TypeAlias = Literal[
    "starting", "running", "completed", "failed", "cancelled", "timed_out", "output_limit", "storage_failure"
]


@dataclass(frozen=True)
class ShellPolicy:
    allow_background: bool = False
    timeout: float = 600.0
    max_output: int = 32768
    max_log_bytes: int = 64 * 1024 * 1024
    max_total_log_bytes: int = 256 * 1024 * 1024
    tail_lines: int = 200
    tail_bytes: int = 32768
    max_concurrent: int = 8
    max_concurrent_per_owner: int = 4
    subscriber_queue_size: int = 32

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        for value in (
            self.max_output,
            self.max_log_bytes,
            self.max_total_log_bytes,
            self.tail_lines,
            self.tail_bytes,
            self.max_concurrent,
            self.max_concurrent_per_owner,
            self.subscriber_queue_size,
        ):
            if value <= 0:
                raise ValueError("shell resource limits must be positive")


@dataclass(frozen=True)
class ShellOwner:
    workspace: Path
    session_id: str
    run_id: str
    tool_call_id: str = ""
    profile: str = ""
    provider: str = ""
    model: str = ""
    root_id: str = ""


@dataclass(frozen=True)
class ShellStorage:
    root: Path | None = None
    persistent: bool = False

    def __post_init__(self) -> None:
        if self.persistent and self.root is None:
            raise ValueError("persistent shell storage requires a root")


@dataclass(frozen=True)
class ShellJob:
    job_id: str
    owner: ShellOwner
    command: str
    background: bool
    state: ShellState = "starting"
    output: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    truncated: bool = False
    output_path: Path | None = None
    retention: Literal["temporary", "persistent"] = "temporary"
    output_available: bool = False
    bytes_written: int = 0
    output_complete: bool = False
    reason: str = ""
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.state not in ("starting", "running")


@dataclass(frozen=True)
class ShellEvent:
    kind: Literal["started", "output", "completed"]
    job: ShellJob
    text: str = ""


ShellCallback: TypeAlias = Callable[[ShellEvent], Awaitable[None]]
ShellJobCallback: TypeAlias = Callable[[ShellJob], Awaitable[None]]
ShellOutputCallback: TypeAlias = Callable[[str], Awaitable[None]]
