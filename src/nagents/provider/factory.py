"""Select a generation backend by wire protocol, independently of credentials."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .anthropic import AnthropicBackend
from .base import ProviderType
from .chat_completions import ChatCompletionsBackend
from .gemini import GeminiBackend
from .responses import ResponsesBackend

if TYPE_CHECKING:
    from .backend import ProviderBackend
    from .base import Provider


def create_backend(provider: Provider) -> ProviderBackend:
    """Read current routing on each attempt so reconfiguration stays effective."""
    if provider.api == "responses":
        return ResponsesBackend(provider)
    if provider.api == "messages":
        return AnthropicBackend(provider)
    if provider.provider_type == ProviderType.GEMINI_NATIVE:
        return GeminiBackend(provider)
    return ChatCompletionsBackend(provider)
