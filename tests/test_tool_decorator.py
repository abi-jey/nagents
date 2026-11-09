"""Public function decorator metadata and tool-executor error boundaries."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents import tool as nagents_tool
from nagents.events import ToolResultEvent
from nagents.tools import ToolExecutor
from nagents.tools import ToolRegistry
from nagents.tools import tool
from nagents.types import ToolCall

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator


def test_decorated_sync_and_async_functions_keep_signature_and_metadata() -> None:
    @nagents_tool
    def lookup(city: str, units: int = 1) -> str:
        """Look up a location.

        Additional details that are not part of the short description.
        """
        return f"{city}:{units}"

    @tool(name="city-summary", description="Describe a city for the caller.")
    async def summarize(city: str, limit: int) -> str:
        """Summarize a city."""
        return f"{city}:{limit}"

    assert nagents_tool is tool
    assert inspect.isfunction(lookup) and not inspect.iscoroutinefunction(lookup)
    assert inspect.isfunction(summarize) and inspect.iscoroutinefunction(summarize)
    assert str(inspect.signature(lookup)) == "(city: 'str', units: 'int' = 1) -> 'str'"
    assert str(inspect.signature(summarize)) == "(city: 'str', limit: 'int') -> 'str'"
    assert lookup.__annotations__ == {"city": "str", "units": "int", "return": "str"}

    registry = ToolRegistry()
    sync_definition = registry.register(lookup)
    async_definition = registry.register(summarize)
    assert (sync_definition.name, sync_definition.description) == ("lookup", "Look up a location.")
    assert (async_definition.name, async_definition.description) == (
        "city-summary",
        "Describe a city for the caller.",
    )
    assert sync_definition.parameters["properties"]["city"]["type"] == "string"
    assert sync_definition.parameters["properties"]["units"]["type"] == "integer"
    assert sync_definition.parameters["required"] == ["city"]


def test_explicit_registry_values_override_decorator_and_plain_functions_keep_defaults() -> None:
    @tool(name="decorated", description="Decorator description")
    def decorated(value: int) -> int:
        """Function description."""
        return value

    def ordinary(value: str) -> str:
        """Ordinary first paragraph.

        An ignored long description for the default registry path.
        """
        return value

    registry = ToolRegistry()
    overridden = registry.register(decorated, name="registered", description="Registry description")
    defaulted = registry.register(ordinary)

    assert (overridden.name, overridden.description) == ("registered", "Registry description")
    assert (defaulted.name, defaulted.description) == ("ordinary", "Ordinary first paragraph.")
    assert defaulted.parameters["properties"]["value"]["type"] == "string"


def test_tool_decorator_rejects_invalid_names_and_non_function_targets() -> None:
    for name in ("bad name", "bad/name", "x" * 65):
        with pytest.raises(ValueError, match="Tool name"):
            tool(name=name)
    with pytest.raises(TypeError, match="description"):
        tool(description=cast("str", 1))

    class CallableObject:
        def __call__(self) -> str:
            return "callable object"

    with pytest.raises(TypeError, match="Python function"):
        tool()(cast("Callable[..., object]", CallableObject()))

    def generated() -> Iterator[str]:
        yield "not a result"

    with pytest.raises(TypeError, match="generators"):
        tool()(generated)


def test_executor_converts_system_exit_and_continues_but_propagates_cancellation() -> None:
    def stops_process() -> str:
        raise SystemExit(7)

    async def healthy() -> str:
        return "still alive"

    async def cancelled() -> str:
        raise asyncio.CancelledError

    registry = ToolRegistry()
    registry.register(stops_process)
    registry.register(healthy)
    registry.register(cancelled)
    executor = ToolExecutor(registry)

    async def scenario() -> None:
        exited = await executor.execute(ToolCall("exit", "stops_process"))
        assert isinstance(exited, ToolResultEvent)
        assert exited.result is None
        assert exited.error == "Tool exited before returning a result."

        followed = await executor.execute(ToolCall("follow", "healthy"))
        assert followed.error is None and followed.result == "still alive"

        with pytest.raises(asyncio.CancelledError):
            await executor.execute(ToolCall("cancel", "cancelled"))

    asyncio.run(scenario())
