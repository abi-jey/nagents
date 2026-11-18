# Python library configuration

These examples configure the Python `Agent`/`Provider` APIs, not either client's
JSON schema. Model IDs are illustrative, not a current catalog or entitlement
guarantee; choose an account-supported model and use [discovery](../guide/providers.md#explicit-catalog-discovery)
where available. For the Rust bundle, use [native configuration](../guide/native-ngn.md#configure-the-bundled-agent-runtime).

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
4. Whether an unknown tool stops the run with an error event instead of returning a recoverable tool error.

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
    config=config,
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

1. Install in your selected virtual environment with `python -m pip install python-dotenv`.

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

!!! warning "Sensitive diagnostic output"
    Library `log_file=` uses `FileHTTPLogger`, not the Python client's separate
    guarded `wire_log` facility. It logs payloads/URLs and only partially masks
    selected request authentication headers; it is not a universal secret
    scrubber. Choose a private log path, disable logging when no longer needed,
    and review/redact files before sharing them.

??? example "Sample Log Output"

    ```
    [2026-01-15T10:30:45.124+00:00] [example-session] >>> REQUEST {"method": "POST", "url": "https://example.invalid/v1/chat/completions", "headers": {}, "body": {"model": "YOUR_MODEL_ID", "messages": [{"role": "user", "content": "Hello"}]}}
    [2026-01-15T10:30:45.460+00:00] [example-session] <<< SSE {"url": "https://example.invalid/v1/chat/completions", "data": {"choices": [{"delta": {"content": "Hello"}}]}}
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

=== "Strict: Stop the Run"

    ```python
    from nagents import Agent, ErrorEvent

    agent = Agent(
        provider=provider,
        session_manager=session_manager,
        tools=[get_weather],
        fail_on_invalid_tool=True,  # (1)!
    )

    async for event in agent.run("Search for restaurants"):
        if isinstance(event, ErrorEvent):
            print(event.message)
    ```

    1. Emits an `ErrorEvent` and stops the text run when an unknown tool is called.

---

## Non-Streaming Mode

`Agent` defaults to **non-streaming**. Select streaming when constructing the
agent (`streaming=True`), not with a `stream` keyword on `run()`. For a complete
answer without chunk handling:

```python
from nagents import Agent, DoneEvent

agent = Agent(provider=provider, session_manager=session_manager, streaming=False)
async for event in agent.run("Hello"):
    if isinstance(event, DoneEvent):
        print(event.final_text)
```

!!! tip "When to Use Non-Streaming"
    - When you need the complete response before processing
    - For batch processing scenarios
    - When latency to first token isn't important
