# Helicone, Braintrust, and W&B Weave

Three approaches to LLM observability: proxy-based (Helicone), eval-first (Braintrust), and experiment-centric (Weave).

---

## Part A — Helicone

**Source:** https://docs.helicone.ai | https://github.com/Helicone/helicone

### A.1 Architecture: Proxy-first

Helicone is a **reverse proxy** (AI Gateway) — you change one line of code (the API base URL) and every LLM request flows through Helicone's servers. Helicone logs the request and response, then forwards to the real provider. No SDK instrumentation required.

```
Your app → https://ai-gateway.helicone.ai → OpenAI / Anthropic / etc.
```

This makes Helicone fundamentally different from Phoenix/Langfuse/LangSmith. It sees everything (latency, token counts, response content) but cannot see your in-process logic (retrieval, prompt building, decision trees) unless you use their async logging SDK.

### A.2 Supported providers (proxy mode)

OpenAI, Anthropic, Azure OpenAI, Gemini, Vertex AI, AWS Bedrock, Groq, Together AI, Mistral, Cohere, OpenRouter, Anyscale, LiteLLM, Ollama, Perplexity, and many more — any OpenAI-compatible endpoint.

### A.3 Quick integration

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://ai-gateway.helicone.ai/v1",  # only change
    api_key=OPENAI_API_KEY,
    default_headers={
        "Helicone-Auth": f"Bearer {HELICONE_API_KEY}",
    },
)

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello"}],
)
```

### A.4 Header-based configuration

All Helicone features are controlled via **HTTP headers** sent with each request.

#### Identity headers
| Header | Type | Purpose |
|---|---|---|
| `Helicone-Auth` | `Bearer <key>` | **Required** authentication |
| `Helicone-User-Id` | string | User identifier (enables per-user analytics) |

#### Session / tracing headers
| Header | Type | Purpose |
|---|---|---|
| `Helicone-Session-Id` | UUID/string | Groups related requests into a session |
| `Helicone-Session-Path` | `/parent/child` | Hierarchical trace path (tree structure) |
| `Helicone-Session-Name` | string | Human-readable session type label |

Session paths use `/` separators to define parent-child relationships:
```
/abstract               → root trace
/abstract/outline       → child of abstract
/abstract/outline/step1 → grandchild
```

#### Custom properties (arbitrary metadata)
```
Helicone-Property-<Name>: <value>
```
Examples:
```
Helicone-Property-Environment: production
Helicone-Property-Version: v2.1.0
Helicone-Property-Feature: summarization
Helicone-Property-Conversation: support_issue_42
```
Properties appear as filterable columns in the dashboard. No schema — any name is valid.

#### Caching headers
| Header | Purpose |
|---|---|
| `Helicone-Cache-Enabled: true` | Enable semantic caching |
| `Cache-Control: max-age=3600` | TTL for cached response |
| `Helicone-Cache-Seed: <string>` | Bucket key for cache partitioning |

#### Rate limiting headers
| Header | Purpose |
|---|---|
| `Helicone-RateLimit-Policy: <N>;w=<seconds>;u=<user\|policy>;s=<cost\|request\|token>` | Rate limit rule |

#### Other control headers
| Header | Purpose |
|---|---|
| `Helicone-Prompt-Id: <name>` | Tag prompt for prompt management versioning |
| `Helicone-Prompt-Version: <version>` | Pin a specific prompt version |
| `Helicone-Retry-Enabled: true` | Enable automatic retries on failure |
| `Helicone-Retry-Num: 3` | Max retry count |
| `Helicone-Fallbacks: [...]` | JSON array of fallback models |

#### Getting the Helicone request ID back
```python
response_with_headers = client.chat.completions.create(...).with_response()
helicone_id = response_with_headers.response.headers.get("helicone-id")
# Use this to update properties post-request via REST API
```

### A.5 What gets logged per request

Each request logged to Helicone includes:
- Full request body (messages, model, parameters)
- Full response body
- Timing: `request_created_at`, `response_created_at`, total latency
- Token usage: `prompt_tokens`, `completion_tokens`, `total_tokens`
- Cost (USD, computed from token counts + provider pricing table)
- Status code, error if any
- All custom properties attached
- User ID, session ID, session path, session name
- Model name, provider
- Cache hit/miss status
- Prompt ID/version if set

### A.6 Async logging SDK (non-proxy)

For cases where you cannot use the proxy (self-hosted models, custom inference):
```python
from helicone import helicone_logger

helicone_logger.log_request(
    request={...},  # raw request dict
    response={...},  # raw response dict
    metadata={
        "user_id": "user-123",
        "session_id": "session-456",
        "properties": {"env": "production"},
    },
)
```

### A.7 Features
- **Prompts**: versioned prompt management with linked requests
- **Datasets**: export filtered requests as datasets
- **Evals**: LLM-as-judge online scoring
- **Experiments**: A/B test prompts
- **Alerts**: threshold-based alerts on cost/latency/error-rate
- **Key Vault**: encrypt/mask provider API keys
- **Fine-tuning export**: export logged data for fine-tuning

---

## Part B — Braintrust

**Source:** https://www.braintrust.dev/docs | https://github.com/braintrustdata

### B.1 Positioning

Eval-first platform: tracing is there to feed the evaluation loop, not the primary product. Strong CI/CD integration. Also has playground, prompt management, and online scoring.

### B.2 Span type taxonomy

| Type | Description |
|---|---|
| `eval` | Root span for an evaluation run; wraps a `task` span. One per test case. |
| `task` | A unit of application logic — workflow, pipeline step, named operation. Root span in production logs. |
| `llm` | Single LLM call — shows model, messages, params, token usage, cost. |
| `function` | Named block of logic — retrieval, formatting, routing. |
| `tool` | Tool call by the model — external API, code execution, DB query. |
| `score` | Result of a scorer (online or offline). Contains score value, scorer name, reasoning. |

### B.3 Python SDK

#### Init
```python
import braintrust

# Option A: init_logger (for production logging)
logger = braintrust.init_logger(
    project="my-project",  # project name
    api_key="...",  # or BRAINTRUST_API_KEY env var
    async_flush=True,  # non-blocking
)

# Option B: init (for experiments/evals)
experiment = braintrust.init(
    project="my-project",
    experiment="experiment-v1",
    dataset=my_dataset,
)
```

#### @traced decorator
```python
from braintrust import traced


@traced  # creates a "task" span by default
def my_pipeline(input: str) -> str:
    result = call_llm(input)
    return result


@traced(type="tool", name="search")
def web_search(query: str) -> list: ...
```

#### Manual spans
```python
with braintrust.start_span(
    name="my-span",
    type="task",  # eval|task|llm|function|tool|score
    input={"query": "hello"},
    tags=["production"],
    metadata={"version": "v2"},
) as span:
    result = do_work()
    span.log(
        output=result,
        metadata={"extra": "info"},
        scores={"relevance": 0.9},
        metrics={"latency": 0.5, "tokens": 150},
    )
```

#### span.log() parameters
```python
span.log(
    input=...,  # any JSON-serializable
    output=...,  # any JSON-serializable
    expected=...,  # ground truth for eval
    scores={  # dict of score_name → float (0-1)
        "accuracy": 0.95,
        "relevance": 0.87,
    },
    metadata={},  # arbitrary dict
    metrics={  # numeric telemetry
        "tokens": 200,
        "latency": 1.2,
        "cost": 0.003,
    },
    tags=["tag1"],
    error=...,  # exception
)
```

#### Wrap providers (explicit instrumentation)
```python
import braintrust
from openai import OpenAI
from anthropic import Anthropic

# Wraps the client — all calls are traced
openai_client = braintrust.wrap_openai(OpenAI())
anthropic_client = braintrust.wrap_anthropic_client(Anthropic())

# Auto-instrument all supported providers at startup
braintrust.auto_instrument()  # patches openai, anthropic, etc.
```

#### TypeScript equivalents
```typescript
import { initLogger, wrapTraced, wrapOpenAI } from "braintrust";

const logger = initLogger({ projectName: "My Project" });

// Traced wrapper
const myFn = wrapTraced(async function myFn(input: string) {
    return "output";
});

// Wrap OpenAI client
const client = wrapOpenAI(new OpenAI());

// Manual span
await logger.traced(async (span) => {
    span.log({ input: "hello", output: "world", metadata: { env: "prod" } });
}, { name: "my-span", type: "task" });
```

### B.4 Eval framework
```python
from braintrust import Eval
from autoevals import Factuality, Levenshtein

Eval(
    name="my-eval",
    data=lambda: [
        {"input": "What is 2+2?", "expected": "4"},
        {"input": "Capital of France?", "expected": "Paris"},
    ],
    task=lambda input: call_my_llm(input),
    scores=[Factuality, Levenshtein],
    experiment_name="experiment-v1",
    trial_count=1,
)
```

### B.5 Autoevals built-in scorers

**LLM-as-judge:** `Battle`, `ClosedQA`, `Humor`, `Factuality`, `Moderation`, `Security`, `Summarization`, `SQL`, `Translation`

**RAG:** `ContextPrecision`, `ContextRelevancy`, `ContextRecall`, `ContextEntityRecall`, `Faithfulness`, `AnswerRelevancy`, `AnswerSimilarity`, `AnswerCorrectness`

**Heuristic:** `Levenshtein`, `ExactMatch`, `NumericDiff`, `JSONDiff`

**Embedding:** `EmbeddingSimilarity`

**Composite:** `SemanticListContains`, `JSONValidity`

### B.6 Feedback / scores
```python
from braintrust import current_span

# Within a traced context
span = current_span()
span.log_feedback(
    scores={"thumbs_up": 1.0},
    comment="Great answer!",
    metadata={"source": "user"},
)
```

### B.7 Data model
- **Project** → org-level grouping
- **Experiment** → immutable eval snapshot, tied to a dataset + task + scores
- **Dataset** → versioned collection of `{input, expected, metadata}` rows
- **Log** → production trace stream (mutable, rolling)
- **Prompt** → versioned prompt stored in Braintrust registry

### B.8 OTel interop

Braintrust accepts OpenTelemetry spans via OTLP. Install `braintrust[otel]` and point OTel exporter at Braintrust's OTLP endpoint. LLM-specific OTel attributes (`gen_ai.*`) are translated to Braintrust's native schema.

---

## Part C — Weights & Biases Weave

**Source:** https://docs.wandb.ai/weave | https://github.com/wandb/weave

### C.1 Positioning

Part of W&B (MLOps platform). Weave focuses on tracing + evaluation with a tight integration into W&B's experiment tracking, artifact versioning, and model registry. `@weave.op` is the primary instrumentation primitive — it's minimal and language-idiomatic.

### C.2 Core concepts

| Concept | Description |
|---|---|
| **Op** | A versioned, tracked function — decorated with `@weave.op`. Its code is captured as a versioned artifact. |
| **Call** | A logged execution of an Op. Every `Call` has inputs, output, timing, parent-child links, errors. |
| **Trace** | A full tree of Calls sharing the same execution context (same `trace_id`). |
| **Thread** | A collection of Traces from a single session/conversation. Equivalent to `session.id` in other tools. |

Calls are analogous to OTel spans. Traces = OTel traces. Ops = instrumented functions.

### C.3 Init and basic usage

```python
import weave
from openai import OpenAI

# Init once per process; sends data to W&B project
weave.init("my-team/my-project")

client = OpenAI()


@weave.op()  # decorates any function; sync, async, generator all work
def call_model(prompt: str) -> str:
    response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}])
    return response.choices[0].message.content


result = call_model("Hello world")
# → Call is logged to W&B Weave; link printed to terminal
```

### C.4 Call object schema

Every execution of a `@weave.op` function produces a `Call`:

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique call ID |
| `trace_id` | string | Shared across all calls in one request tree |
| `parent_id` | string? | Parent call ID (null for root) |
| `op_name` | string | Fully-qualified op name (e.g. `my_module.call_model`) |
| `display_name` | string | Human-readable name |
| `inputs` | dict | Function argument values |
| `output` | any | Return value |
| `exception` | string? | Exception traceback if call failed |
| `started_at` | datetime | Call start time |
| `ended_at` | datetime | Call end time |
| `attributes` | dict | Custom metadata (set via `weave.attributes`) |
| `summary` | dict | Aggregated stats: token usage, cost, child call counts |

### C.5 Threads (sessions)

```python
import weave

# Group calls into a conversation thread
with weave.attributes({"weave.thread_id": "session-abc"}):
    response1 = call_model("Hello")
    response2 = call_model("Follow up")
```

### C.6 Custom attributes

```python
with weave.attributes({"user_id": "user-123", "env": "production"}):
    result = call_model("Hello")
    # These attributes appear on all calls within the context
```

### C.7 Auto-patched providers

Weave automatically patches these when imported after `weave.init()`:

`openai`, `anthropic`, `mistralai`, `cohere`, `google-generativeai` (Gemini), `groq`, `litellm`, `huggingface_hub` (Inference Endpoints), `cerebras_cloud_sdk`, `nvidia-riva`, `ollama`, `vllm`, `together`

Plus frameworks: `langchain`, `llamaindex`, `dspy`, `instructor`, `openai-agents`, `smolagents`, `pydantic-ai`, `crewai`

### C.8 Model and Evaluation classes

```python
import weave
from weave import Model, Evaluation


# Define a model as a versioned artifact
class MyModel(Model):
    model_name: str = "gpt-4o"
    system_prompt: str = "You are a helpful assistant."

    @weave.op()
    def predict(self, question: str) -> str: ...


# Define a dataset
dataset = [
    {"question": "What is 2+2?", "answer": "4"},
    {"question": "Capital of France?", "answer": "Paris"},
]


# Define scorers
@weave.op()
def accuracy(answer: str, model_output: str) -> dict:
    return {"correct": answer.strip() == model_output.strip()}


# Run evaluation
evaluation = Evaluation(
    dataset=dataset,
    scorers=[accuracy],
)

model = MyModel()
results = await evaluation.evaluate(model)
```

### C.9 Feedback and annotations

```python
# Get a call reference
call = weave.get_current_call()  # within a traced context

# Add feedback programmatically
call.feedback.add("note", {"message": "Good answer!"})
call.feedback.add_reaction("👍")
call.feedback.add("score", {"value": 1.0, "name": "correctness"})
```

Human annotations are added in the Weave UI.

### C.10 Data model hierarchy
- **Entity** → W&B team
- **Project** → groups all Weave data
- **Traces** → individual request trees
- **Ops** → versioned function artifacts
- **Models** → versioned `Model` class instances
- **Datasets** → versioned data collections
- **Evaluations** → experiment runs (dataset × model × scorers)

---

## Part D — Comparison

| Dimension | Helicone | Braintrust | W&B Weave |
|---|---|---|---|
| **Primary model** | Proxy (AI Gateway) | Eval-first platform | Op/Call tracing + W&B eval |
| **Code change** | Change `base_url` only | `@traced` / `init_logger` | `@weave.op` + `weave.init` |
| **Sees internal logic** | No (proxy only) | Yes | Yes |
| **Span hierarchy** | Session path (flat tree via headers) | Nested spans (task/llm/tool/function/score) | Nested Calls (Op tree) |
| **Eval framework** | Basic (LLM-as-judge online) | Full (`Eval`, `autoevals`, experiments) | Full (`Evaluation`, scorers, W&B artifacts) |
| **Storage** | Helicone cloud / self-hosted (OSS) | Braintrust cloud | W&B cloud |
| **OTel / OpenInference** | No native OTel; OTLP not natively accepted | Accepts OTel via `braintrust[otel]` | Custom protocol; no OTel ingest |
| **Open-source** | Yes (helicone/helicone) | No (cloud SaaS; client SDK is OSS) | Yes (wandb/weave) |
| **Best for** | Zero-code logging, cost monitoring, caching | Offline eval, CI/CD regression testing | Research teams already using W&B |
