"""
Tool execution with asyncio.to_thread for sync functions.
"""

import asyncio
import functools
import inspect
import logging
import time

from ..events import ToolResultEvent
from ..types import ToolCall
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    """
    Executes tool calls.

    Handles both sync and async functions:
    - Async functions are awaited directly
    - Sync functions are run in a thread pool via asyncio.to_thread()

    Example:
        executor = ToolExecutor(registry)
        result_event = await executor.execute(tool_call)
    """

    def __init__(self, registry: ToolRegistry):
        """
        Initialize the executor.

        Args:
            registry: Tool registry to look up functions
        """
        self._registry = registry

    async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
        """
        Execute a tool call and return result event.

        Sync functions are automatically run in a thread pool to avoid
        blocking the event loop.

        Args:
            tool_call: The tool call to execute

        Returns:
            ToolResultEvent with result or error
        """
        start = time.monotonic()
        tool = self._registry.get(tool_call.name)

        if not tool or not tool.func:
            return ToolResultEvent(
                id=tool_call.id,
                name=tool_call.name,
                error=f"Unknown tool: {tool_call.name}",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        try:
            func = tool.func
            kwargs = tool_call.arguments

            # Call the function (handle both sync and async)
            if inspect.iscoroutinefunction(func):
                # Async function - await directly
                result = await func(**kwargs)
            else:
                # Sync function - run in thread pool to avoid blocking
                result = await asyncio.to_thread(functools.partial(func, **kwargs))

            duration = (time.monotonic() - start) * 1000
            logger.debug(f"Tool {tool_call.name} executed in {duration:.2f}ms")

            return ToolResultEvent(
                id=tool_call.id,
                name=tool_call.name,
                result=result,
                duration_ms=duration,
            )

        except Exception as e:
            duration = (time.monotonic() - start) * 1000
            logger.exception(f"Tool {tool_call.name} failed")

            return ToolResultEvent(
                id=tool_call.id,
                name=tool_call.name,
                error=str(e),
                duration_ms=duration,
            )
