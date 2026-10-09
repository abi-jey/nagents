"""Immutable client requests and transport-neutral Live update callbacks."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ClientDelegationRequest:
    """A notice's original identity and context, captured before backend work.

    Public Live supplies transcript context, while ChatGPT login can also supply
    an explicit native request in ``text``. Neither is trusted instructions.
    """

    identifier: str
    text: str = ""
    transcript: str = "[]"
    offset_ms: float | None = None


ClientDelegationHandler = Callable[[ClientDelegationRequest], Awaitable[str]]
ClientDelegationObserver = Callable[[ClientDelegationRequest], None]
LiveAppendKind = Literal["thinking", "commentary", "instructions"]
LiveAppend = Callable[[LiveAppendKind, str, str], Awaitable[str]]
