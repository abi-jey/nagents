"""Native Agent and Harness execution adapters for A2A tasks."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import AgentContext as AgentContext
    from .runtime import AgentFactory as AgentFactory
    from .runtime import AgentRuntime as AgentRuntime
    from .runtime import ApprovedToolExecutor as ApprovedToolExecutor
    from .runtime import NativeAgentRuntime as NativeAgentRuntime

__all__ = ["AgentContext", "AgentFactory", "AgentRuntime", "ApprovedToolExecutor", "NativeAgentRuntime"]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".runtime", __name__), name)
    globals()[name] = value
    return value
