# Tool Usage

Pass Python functions directly to `Agent(tools=[...])`. nagents registers the
callables and extracts a JSON Schema from their type hints and docstrings.
There is no `Tool.from_function()` API.

## Basic Tool Definition

Set `OPENAI_API_KEY` and `OPENAI_MODEL` to your credentials and an available
model that supports tool calling.

```python title="basic_tools.py" linenums="1"
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from nagents import (
    Agent,
    ErrorEvent,
    Provider,
    ProviderType,
    SessionManager,
    TextChunkEvent,
    ToolCallEvent,
    ToolResultEvent,
)


def get_current_time() -> str:
    """Get the current date and time in UTC."""
    return datetime.now(timezone.utc).isoformat()


def percentage(percent: float, amount: float) -> float:
    """Calculate a percentage of an amount.

    Args:
        percent: The percentage to calculate, such as 15.
        amount: The original amount.
    """
    return percent * amount / 100


async def main() -> None:
    agent = Agent(
        provider=Provider(
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ["OPENAI_MODEL"],
        ),
        session_manager=SessionManager(Path("sessions.db")),
        tools=[get_current_time, percentage],
        system_prompt="Use the tools for time and percentage questions.",
        streaming=True,
        max_tool_rounds=5,
        save_tool_outputs=False,
    )
    try:
        async for event in agent.run("What time is it, and what is 15% of 85?"):
            if isinstance(event, TextChunkEvent):
                print(event.chunk, end="", flush=True)
            elif isinstance(event, ToolCallEvent):
                print(f"\n[Calling: {event.name}]")
            elif isinstance(event, ToolResultEvent):
                if event.error:
                    print(f"[Tool failed: {event.error}]")
                else:
                    print(f"[Result: {event.result}]")
            elif isinstance(event, ErrorEvent):
                print(f"\nError: {event.message}")
        print()
    finally:
        await agent.close()


if __name__ == "__main__":
    asyncio.run(main())
```

The model chooses whether to call a tool. `ToolCallEvent` reports the request;
`ToolResultEvent` reports execution. Tool exceptions are reported through
`ToolResultEvent.error`, not a separate `tool_error` event.

The example disables the library's reserved `_save_to` convention, which would
otherwise let a model request that a tool result be written to a file. This
setting does not sandbox the tool functions themselves.

## Async Tools

Use `async def` for I/O. This function can be added to the `tools` list above;
it uses the existing `aiohttp` dependency and a fixed destination rather than
accepting an unrestricted URL from the model:

```python
from urllib.parse import quote

import aiohttp


async def fetch_weather(city: str) -> str:
    """Fetch a short weather report from wttr.in.

    Args:
        city: City name to look up.
    """
    if not city.strip() or len(city) > 100:
        raise ValueError("Provide a city name between 1 and 100 characters")
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(
            f"https://wttr.in/{quote(city, safe='')}?format=3",
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Weather service returned HTTP {response.status}")
            return (await response.content.read(4096)).decode("utf-8", errors="replace")
```

This is a third-party network service, not bundled weather data; availability
and response format are outside nagents' control.

## Parameters and Validation

Schema extraction supports basic types such as `str`, `int`, `float`, `bool`,
`list[T]`, and `dict[K, V]`. Do not assume arbitrary annotations or nested
Pydantic models are converted into schemas or validated at runtime. Validate
inputs in the callable, and return JSON-serializable values.

```python
def divide(a: float, b: float) -> float:
    """Divide a by b.

    Args:
        a: Numerator.
        b: Nonzero denominator.
    """
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
```

Raising a descriptive exception lets the agent report the failed tool call to
the model. Your application should also inspect `ToolResultEvent.error`, as in
the complete example.

## Manual Tool Schema

For schema overrides, register the callable on the agent's registry. This
fragment assumes an existing `agent`; register before starting a run:

```python
def format_greeting(name: str, style: str = "casual") -> str:
    """Format a greeting."""
    if style not in ("casual", "formal"):
        raise ValueError("style must be casual or formal")
    return f"Hello, {name}." if style == "formal" else f"Hi {name}!"


agent.tool_registry.register(
    format_greeting,
    name="greet",
    description="Create a casual or formal greeting",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "style": {"type": "string", "enum": ["casual", "formal"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)
```

`ToolRegistry.register()` returns a `ToolDefinition`. Its callable field is
`func`, and it has no `strict` parameter. `Agent(tools=...)` takes callables,
not `ToolDefinition` objects. If you supply a custom tool executor, ensure it
uses the matching registry. See the [Tools API](../api/tools.md).

## Combining Tools

A research assistant can register several callables with the same mechanism:
`tools=[web_search, save_note, list_notes]`. Implement and test each callable
first. Mock search results and in-memory note lists are demonstrations, not a
real search service or persistent storage; session history does not persist
arbitrary Python globals.

## Security and Resource Limits

- Do not use `eval()` on model-provided expressions. Expose bounded operations
  such as `percentage()` instead.
- Restrict filesystem and network destinations, validate inputs, and apply
  timeouts and output-size limits within tools.
- Tools execute with the host process's privileges. The bare library agent
  does not provide the coding harness's approval policy or an OS sandbox.
- Inspect uncertain outcomes before retrying side-effecting tools. Cancellation
  does not guarantee that a synchronous operation has stopped.

## Next Steps

- Read the [Tools Guide](../guide/tools.md) for execution details
- Handle [Events](../guide/events.md) and manage [Sessions](../guide/sessions.md)
- Review [Providers](../guide/providers.md) for contract-specific limitations
