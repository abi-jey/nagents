# Arize Phoenix

**Source:** https://github.com/Arize-ai/phoenix | https://arize.com/docs/phoenix

Phoenix is an **open-source AI observability platform** from Arize. It ingests OpenTelemetry spans annotated with **OpenInference** semantic conventions (see `01-openinference-spec.md`) and provides a UI for traces, evals, datasets, experiments, and prompt management. It runs locally (Jupyter, Docker, bare-metal) or in Arize's cloud (app.phoenix.arize.com).

## 1. Architecture

```
Your app
  │ OTel SDK + openinference-instrumentation-*
  │ OTLP (HTTP or gRPC)
  ▼
Phoenix server (port 6006 HTTP / 4317 gRPC OTLP)
  │
  ├── SQLite (default) or PostgreSQL
  ├── Optional S3/GCS for attachments
  └── Web UI (React, port 6006)
```

Phoenix is **not** a proxy — it's a passive sink. Spans land via OTLP and are stored; nothing is in the request path.

### Deployment options
- `pip install arize-phoenix && python -m phoenix.server.main` — local server
- `px.launch_app()` — in-process notebook server
- Docker: `docker run -p 6006:6006 arizephoenix/phoenix`
- Helm chart for Kubernetes
- Cloud: app.phoenix.arize.com (hosted SaaS, same OSS codebase)

## 2. Python packages

| Package | Purpose |
|---|---|
| `arize-phoenix` | Full server + client (heavy) |
| `arize-phoenix-otel` | Lightweight OTel setup helper (`register()`) |
| `arize-phoenix-client` | REST API client — no server dependency |
| `arize-phoenix-evals` | Evaluation library (LLM-as-judge, template evals) |
| `@arizeai/phoenix-otel` | TypeScript OTel helper |
| `@arizeai/phoenix-client` | TypeScript REST client |
| `@arizeai/phoenix-evals` | TypeScript evals (alpha) |
| `@arizeai/phoenix-mcp` | MCP server for Phoenix |
| `@arizeai/phoenix-cli` | CLI for fetching traces, datasets, experiments |

## 3. `arize-phoenix-otel` — the recommended setup entry point

### `register()` signature
```python
from phoenix.otel import register

tracer_provider = register(
    endpoint: Optional[str] = None,          # OTLP endpoint; env: PHOENIX_COLLECTOR_ENDPOINT
    project_name: Optional[str] = None,      # env: PHOENIX_PROJECT_NAME
    batch: bool = False,                     # True = BatchSpanProcessor, False = SimpleSpanProcessor
    set_global_tracer_provider: bool = True, # replaces OTel global provider
    headers: Optional[Dict[str, str]] = None,# merged with PHOENIX_CLIENT_HEADERS env var
    protocol: Literal["http/protobuf","grpc"] = None,  # auto-detected from endpoint
    verbose: bool = True,
    auto_instrument: bool = False,           # auto-discovers + activates installed openinference-instrumentation-* packages
    api_key: Optional[str] = None,           # env: PHOENIX_API_KEY
)
# returns a TracerProvider (OTel standard interface + Phoenix extensions)
```

### What `register()` wires internally
- Creates `HTTPSpanExporter` or `GRPCSpanExporter` pointed at the endpoint
- Wraps it in `SimpleSpanProcessor` or `BatchSpanProcessor`
- Builds a `TracerProvider` with a `Resource` containing `project_name`
- Optionally calls `_auto_instrument_installed_openinference_libraries()` which uses Python entry_points to discover `openinference-instrumentation-*` packages and calls `.instrument()` on each

### Public classes re-exported from `phoenix.otel`
```python
TracerProvider
SimpleSpanProcessor
BatchSpanProcessor
HTTPSpanExporter
GRPCSpanExporter
Resource
PROJECT_NAME  # str constant "phoenix_project_name"
```

## 4. Manual instrumentation — OpenInference tracer decorators

After `register()`, get a tracer and use span-kind decorators:

```python
tracer = tracer_provider.get_tracer(__name__)


# Decorator per span kind — auto-captures input/output/status
@tracer.chain
def my_chain(input: str) -> str: ...


@tracer.agent
def my_agent(input: str) -> str: ...


@tracer.tool
def my_tool(input1: str, input2: int) -> None:
    """tool-description"""


@tracer.llm(process_input=..., process_output=...)
def call_llm(messages, model, temperature=None):
    ...
    # process_input/process_output translate native types → OI attributes


# Context manager form
from opentelemetry.trace import Status, StatusCode

with (
    tracer.start_as_current_span(
        "my-span",
        openinference_span_kind="chain",  # one of: llm, chain, agent, tool, retriever, reranker, embedding, guardrail, evaluator, prompt
    ) as span
):
    span.set_input("input text or dict")
    span.set_output("output text or dict")
    span.set_status(Status(StatusCode.OK))

# Tool span has extra method
with tracer.start_as_current_span("my-tool", openinference_span_kind="tool") as span:
    span.set_tool(name="tool-name", description="...", parameters={...})
```

### Override span name
```python
@tracer.chain(name="my-custom-name")
def fn(input: str) -> str: ...
```

### Suppress tracing
```python
from openinference.instrumentation import suppress_tracing

with suppress_tracing():
    ...  # spans inside here are not emitted
```

## 5. Context attributes (session, user, metadata, tags)

From `openinference.instrumentation`:
```python
from openinference.instrumentation import using_attributes

with using_attributes(
    session_id="session-123",
    user_id="user-456",
    metadata={"app": "my-app"},
    tags=["production"],
    prompt_template="You are {role}",
    prompt_template_variables={"role": "assistant"},
    prompt_template_version="v2",
):
    # All spans created here inherit these attributes
    result = my_chain("hello")
```

These inject the corresponding OpenInference attributes (`session.id`, `user.id`, `metadata`, `tag.tags`, `llm.prompt_template.*`) into every span created within the context.

## 6. Attribute helper functions (for LLM spans)

From `openinference.instrumentation`:
```python
get_llm_attributes(
    provider=None, system=None, model_name=None,
    input_messages=None, output_messages=None,
    invocation_parameters=None, tools=None,
    token_count=None,
) -> Dict[str, AttributeValue]

get_input_attributes(value: Any) -> Dict[str, AttributeValue]
get_output_attributes(value: Any) -> Dict[str, AttributeValue]
```

OpenInference typed objects for constructing messages:
```python
import openinference.instrumentation as oi

msg = oi.Message(role="user", content="hello")
msg_with_tool_calls = oi.Message(
    role="assistant",
    tool_calls=[
        oi.ToolCall(id="call_123", function=oi.ToolCallFunction(name="get_weather", arguments='{"city":"Zurich"}'))
    ],
)
tool = oi.Tool(json_schema={"type": "function", "function": {...}})
token_count = oi.TokenCount(prompt=100, completion=50, total=150)

# Multimodal
image = oi.Image(url="https://...")
contents = [
    oi.TextMessageContent(type="text", text="describe this"),
    oi.ImageMessageContent(type="image", image=image),
]
msg = oi.Message(role="user", contents=contents)
```

## 7. Auto-instrumentors (from openinference-instrumentation-* packages)

When `auto_instrument=True` or called manually via `.instrument()`:

**LLM providers:**
`openai`, `openai-agents`, `anthropic`, `google-genai`, `google-adk`, `bedrock`, `vertexai`, `mistralai`, `groq`, `litellm`, `portkey`

**Agent frameworks:**
`langchain`, `llama-index`, `haystack`, `dspy`, `crewai`, `autogen`, `autogen-agentchat`, `agno`, `beeai`, `smolagents`, `strands-agents`, `pydantic-ai`, `claude-agent-sdk`, `mcp`

**Other:**
`guardrails`, `instructor`, `pipecat`, `promptflow`, `openlit`, `openllmetry`

Each instrumentor monkeypatches the target library to emit OpenInference-compliant spans automatically.

## 8. arize-phoenix-evals

### Built-in evaluators (via `create_evaluator` / `ClassificationEvaluator`)

The library provides LLM-as-judge evaluators via template prompts:

| Evaluator | What it checks |
|---|---|
| Hallucination | Is the answer grounded in the context? |
| QA Correctness | Is the answer correct given the question? |
| Relevance | Is the retrieved document relevant to the query? |
| Toxicity | Does the response contain harmful content? |
| Summarization | Is the summary accurate? |
| SQL Generation Correctness | Is the generated SQL correct? |

### Running evals
```python
from phoenix.evals import (
    LLMEvaluator,
    ClassificationEvaluator,
    evaluate_dataframe,
    async_evaluate_dataframe,
)

# On a pandas DataFrame of spans
results = evaluate_dataframe(
    dataframe=df,  # traces as rows
    evaluators=[hallucination_eval],
    model=LLM(...),  # the judge model
)
```

Eval results are attached back to spans as `eval.*` attributes (e.g. `eval.hallucination.label`, `eval.hallucination.score`, `eval.hallucination.explanation`).

## 9. Projects

Spans are routed to projects via the `Resource` attribute `phoenix_project_name` (set by `register(project_name=...)`). Multiple projects live in one Phoenix server. The default project is `"default"`.

## 10. Sessions and users in the UI

Phoenix filters by:
- `session.id` → "Sessions" view (groups traces from one conversation)
- `user.id` → "Users" view
- `tag.tags` → tag filter
- `metadata` → metadata filter

These all come from the OpenInference attributes on spans — Phoenix does no special session management; it's pure attribute filtering.

## 11. Annotations / feedback API

Via `arize-phoenix-client` or REST:
```python
from phoenix.client import Client

client = Client(endpoint="http://localhost:6006")

# Add a score annotation to a span
client.annotations.create(
    span_id="...",
    name="thumbs_up",
    annotator_kind="HUMAN",
    result={"label": "correct", "score": 1.0, "explanation": "..."},
)
```

## 12. Datasets and experiments

```python
# Upload a dataset
client.datasets.upload(
    name="my-dataset",
    dataframe=df,  # columns: input, expected_output, metadata
)

# Run an experiment
from phoenix.experiments import run_experiment

experiment = run_experiment(
    dataset=client.datasets.get("my-dataset"),
    task=my_agent_function,
    evaluators=[relevance_eval, hallucination_eval],
    experiment_name="experiment-v1",
)
```

## 13. Prompt management

- Prompts stored and versioned in Phoenix server
- Tagged with environment labels (production, staging)
- Pulled via `client.prompts.get(name="...", tag="production")`
- Playground UI for interactive testing

## 14. What Phoenix renders per span kind

| Span kind | Phoenix UI shows |
|---|---|
| `LLM` | Messages table (role/content), tool calls, token counts, model, invocation params, cost |
| `EMBEDDING` | Embedding text, vector dimension, model |
| `RETRIEVER` | Retrieved documents with score and content |
| `RERANKER` | Input/output documents, query, top_k |
| `TOOL` | Tool name, description, parameters, input/output |
| `CHAIN` | Generic input/output, latency |
| `AGENT` | Same as CHAIN, but child spans are agent sub-steps |
| `GUARDRAIL` | Input/output |
| `EVALUATOR` | Eval scores |
| `PROMPT` | Template + variables |

## 15. Arize AX SaaS vs Phoenix OSS

| Feature | Phoenix OSS | Arize AX SaaS |
|---|---|---|
| Hosting | Self-managed | Fully managed |
| Storage | SQLite/Postgres | Proprietary ClickHouse-backed |
| Scale | Single-node | Multi-tenant, large scale |
| Evals | `arize-phoenix-evals` | Same + hosted eval jobs |
| Datasets | Local | Managed versioned datasets |
| Ingestion | OTLP | OTLP + Arize SDK |
| Price | Free/OSS | Enterprise |

Both accept the same OpenInference span format.
