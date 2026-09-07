# Configuration

## Provider Configuration

Configure your LLM provider with the appropriate credentials and settings.

=== "OpenAI"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.OPENAI_COMPATIBLE,
        api_key="sk-...",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",  # (1)!
    )
    ```

    1. Optional - defaults to OpenAI's API endpoint.

=== "Anthropic Claude"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.ANTHROPIC,
        api_key="sk-ant-...",
        model="claude-3-5-sonnet-20241022",
    )
    ```

=== "Google Gemini"

    ```python
    from nagents import Provider, ProviderType

    provider = Provider(
        provider_type=ProviderType.GEMINI_NATIVE,
        api_key="...",
        model="gemini-2.0-flash",
    )
    ```

---

## Agent Configuration

!!! important "Required: Session Manager"
    The `session_manager` parameter is **required** when creating an Agent.

```python
from pathlib import Path
from nagents import Agent, SessionManager

session_manager = SessionManager(Path("sessions.db"))

agent = Agent(
    provider=provider,
    session_manager=session_manager,
    system_prompt="You are a helpful assistant.",  # (1)!
    tools=[...],  # (2)!
    log_file="http_traffic.log",  # (3)!
    fail_on_invalid_tool=False,  # (4)!
)
```

1. Optional system prompt to define agent behavior.
2. Optional list of tool functions.
3. Optional HTTP traffic logging for debugging.
4. Whether to raise an exception when the LLM calls an unknown tool.

---

## Generation Configuration

Control generation parameters for fine-tuned responses:

```python
from nagents import GenerationConfig

config = GenerationConfig(
    temperature=0.7,  # (1)!
    max_tokens=1000,  # (2)!
    top_p=0.9,  # (3)!
    stop=["END"],  # (4)!
)

async for event in agent.run(
    "Write a story",
    generation_config=config,
):
    ...
```

1. Creativity level (0.0-2.0). Higher = more creative, lower = more focused.
2. Maximum output tokens. Limits response length.
3. Nucleus sampling threshold. Alternative to temperature.
4. Stop sequences. Generation stops when these are encountered.

??? tip "Temperature Guidelines"

    | Temperature | Use Case |
    |-------------|----------|
    | 0.0 - 0.3 | Factual responses, code generation |
    | 0.4 - 0.7 | Balanced creativity and coherence |
    | 0.8 - 1.2 | Creative writing, brainstorming |
    | 1.3 - 2.0 | Highly creative, experimental |

---

## Environment Variables

!!! warning "Security Best Practice"
    Never hardcode API keys in your source code. Use environment variables instead.

Create a `.env` file:

```bash title=".env"
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

Load and use in your code:

```python title="config.py"
import os
from dotenv import load_dotenv  # (1)!
from nagents import Provider, ProviderType

load_dotenv()

provider = Provider(
    provider_type=ProviderType.OPENAI_COMPATIBLE,
    api_key=os.getenv("OPENAI_API_KEY"),
    model="gpt-4o-mini",
)
```

1. Install with `pip install python-dotenv`

---

## HTTP Traffic Logging

For debugging, you can log all HTTP requests, responses, and SSE chunks to a file:

```python hl_lines="6"
from pathlib import Path
from nagents import Agent, SessionManager

agent = Agent(
    provider=provider,
    session_manager=SessionManager(Path("sessions.db")),
    log_file="http_traffic.log",  # Enable HTTP logging
)
```

!!! info "What Gets Logged"
    - **Requests**: Method, URL, headers, and body
    - **Responses**: Status code, headers, and body
    - **SSE chunks**: Individual server-sent events during streaming

??? example "Sample Log Output"

    ```
    ================================================================================
    [2024-01-15 10:30:45.123] SESSION: a1b2c3d4
    ================================================================================

    --- REQUEST ---
    [10:30:45.124] POST https://api.openai.com/v1/chat/completions
    Headers: {
      "Authorization": "Bearer sk-...",
      "Content-Type": "application/json"
    }
    Body: {
      "model": "gpt-4o-mini",
      "messages": [{"role": "user", "content": "Hello"}],
      "stream": true
    }

    --- RESPONSE ---
    [10:30:45.456] Status: 200
    Headers: {...}

    --- SSE CHUNK ---
    [10:30:45.460] data: {"choices":[{"delta":{"content":"Hello"}}]}

    --- SSE CHUNK ---
    [10:30:45.465] data: {"choices":[{"delta":{"content":"!"}}]}
    ```

---

## Tool Hallucination Handling

When an LLM tries to call a tool that doesn't exist, you can control the behavior:

=== "Default: Self-Correct"

    ```python
    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_weather],
        fail_on_invalid_tool=False,  # (1)!
    )
    # LLM receives: "Unknown tool: search. Available tools: get_weather"
    # LLM can then retry with a valid tool
    ```

    1. Default behavior - returns error to LLM, allowing it to self-correct.

=== "Strict: Raise Exception"

    ```python
    from nagents import Agent, ToolHallucinationError

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_weather],
        fail_on_invalid_tool=True,  # (1)!
    )

    try:
        async for event in agent.run("Search for restaurants"):
            ...
    except ToolHallucinationError as e:
        print(f"Unknown tool: {e.tool_name}")
        print(f"Available: {e.available_tools}")
    ```

    1. Raises `ToolHallucinationError` immediately when an unknown tool is called.

---

## Non-Streaming Mode

By default, nagents streams responses. For non-streaming mode:

```python
async for event in agent.run("Hello", stream=False):
    if isinstance(event, TextDoneEvent):
        print(event.text)  # Complete response at once
```

!!! tip "When to Use Non-Streaming"
    - When you need the complete response before processing
    - For batch processing scenarios
    - When latency to first token isn't important
