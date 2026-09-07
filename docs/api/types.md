# Types API

These public types are exported from `nagents`. Generation configuration belongs
on `Agent.run(config=...)` or `Provider.generate(config=...)`. A field's presence
does not guarantee support by every provider, contract, or model.

::: nagents.GenerationConfig

::: nagents.RetryConfig

::: nagents.Message

## Content Parts

Use `str` for plain message content or a list of supported content parts.
Media support depends on the destination model and adapter; constructing a
content part alone does not establish support.

::: nagents.TextContent

::: nagents.ImageContent

::: nagents.AudioContent

::: nagents.DocumentContent

## Tools and Schemas

::: nagents.ToolCall

::: nagents.ToolDefinition

::: nagents.JsonSchema

::: nagents.JsonSchemaProperty
