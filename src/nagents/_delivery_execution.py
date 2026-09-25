"""Optional private Agent hooks, independent of Harness and UI packages."""

from typing import TYPE_CHECKING
from typing import Protocol

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from .events import ToolCallEvent
    from .types import Message
    from .types import ToolCall


class _DeliveryExecution(Protocol):
    """Only the lifecycle boundaries needed by the core text Agent loop.

    Persistence capture and delivery authorization belong to the host. Ordinary
    Agents leave this hook unset; session adapters need no additional methods.
    """

    def turn(self, session_id: str | None) -> "AbstractContextManager[None]": ...

    def reserve(self, message: "Message", events: tuple["ToolCallEvent", ...]) -> "AbstractContextManager[None]": ...

    async def abandon(self, events: tuple["ToolCallEvent", ...]) -> None: ...

    def executing(self, call: "ToolCall", position: int) -> "AbstractContextManager[None]": ...
