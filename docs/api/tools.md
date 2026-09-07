# Tools API

Pass Python callables to `Agent(tools=[...])`. `ToolRegistry` extracts schemas
and supports explicit name, description, and parameter overrides. Its
`register()` method returns a `ToolDefinition`, not a wrapper to pass back into
`Agent(tools=...)`. See [Tool Usage](../examples/tool-usage.md).

Schema extraction is not runtime input validation or a security sandbox.
Validate inputs inside the callable, constrain side effects, and inspect
`ToolResultEvent.error` for execution failures. A custom executor must use a
registry matching the agent's tools.

::: nagents.ToolRegistry
    options:
      members:
        - register
        - get
        - get_all
        - names
        - has_tools
        - clear

::: nagents.ToolExecutor
    options:
      members:
        - __init__
        - execute

The underlying definition and schema types are in the [Types API](types.md).
