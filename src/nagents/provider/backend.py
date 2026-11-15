"""Generation backend contract used by the public Provider facade.

Backends borrow their owner's transport and configuration. The facade owns
transport lifetime, retries, session context and error normalization; backends
own wire-specific requests and response validation.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ..events import Event
    from ..types import GenerationConfig
    from ..types import Message
    from ..types import ToolDefinition
    from .base import Provider


class ProviderBackend(ABC):
    """One generation attempt, without retries or ownership of shared resources."""

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    @abstractmethod
    def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: GenerationConfig | None,
        stream: bool,
    ) -> AsyncIterator[Event]:
        """Serialize a request and yield validated provider events."""
        raise NotImplementedError
