# Langfuse — Deep Reference for Observability Library Authors

Status: current as of 2026-04. Covers Python SDK v3 + v4 (March 2026 rewrite), JS/TS SDK v4 + v5 (March 2026 rewrite), ingestion API, OTLP endpoint, integrations, and self-hosted architecture.

Langfuse is an open-source LLM observability / evaluation platform. The Python v4 SDK and JS v5 SDK are complete rewrites on top of OpenTelemetry — SDK instrumentation now emits OTel spans that are translated into Langfuse "observations" server-side. The legacy ingestion endpoint (`POST /api/public/ingestion`) remains the authoritative wire format and is still used internally by the SDKs when operating in non-OTel mode; new deployments are encouraged to use the OTLP endpoint.

---

## 1. Data Model

### 1.1 Object hierarchy

```
Session  1 --- n  Trace  1 --- n  Observation (tree; parent_observation_id)
                                 Observation types: SPAN | GENERATION | EVENT
                                                    + semantic: agent, tool, chain,
                                                      retriever, evaluator,
                                                      embedding, guardrail
Trace    1 --- n  Score       (or Score -> Observation, or Score -> Session)
```

- A **Session** groups Traces by a string `sessionId`.
- A **Trace** is the top-level container for a single user request / agent run.
- An **Observation** is a node within a trace's execution tree. It has a polymorphic `type`:
  - `SPAN` — generic unit of work with a start/end.
  - `GENERATION` — LLM call; adds model, prompt/completion, usage, cost.
  - `EVENT` — instantaneous point-in-time event (no duration). Legacy; still accepted.
- Starting v4, the SDK model is **observation-centric**: trace-level attributes (user, session, tags, metadata, release, version, public) are propagated to every observation on the SDK side so any span can "own" the trace context. Server-side, the root span's attributes set the trace record.
- Semantic observation types (agent, tool, chain, retriever, evaluator, embedding, guardrail) are stored as `SPAN` with `langfuse.observation.type` set; the UI renders them distinctly.

### 1.2 Trace fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Client-generated; idempotent. V3 uses UUID, v4 uses 32-char OTel trace id hex. |
| `timestamp` | ISO-8601 | yes | Trace start. |
| `name` | string | no | Display name. |
| `userId` | string | no | End-user identifier. |
| `sessionId` | string ≤ 200 US-ASCII | no | Conversation / thread id. |
| `release` | string | no | Release tag (git sha, semver). |
| `version` | string | no | Application version. |
| `input` | any JSON | no | Top-level input (aggregated). |
| `output` | any JSON | no | Top-level output. |
| `metadata` | object | no | Arbitrary JSON. |
| `tags` | string[] | no | Free-form labels. |
| `public` | boolean | no | If true, trace is accessible via public link. |
| `environment` | string | no | `production`, `staging`, `development`, or custom. Must be ≤40 chars, `[a-z0-9_-]`, must NOT start with `langfuse`. |

### 1.3 Observation fields (base — applies to SPAN, GENERATION, EVENT)

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Idempotent id; for OTel path this is the OTel span id. |
| `traceId` | string | yes | Parent trace id. |
| `type` | enum | yes | `SPAN` \| `GENERATION` \| `EVENT`. |
| `name` | string | no | Display name. |
| `startTime` | ISO-8601 | yes (SPAN/GENERATION) | Start timestamp. |
| `endTime` | ISO-8601 | no | Set on `-update` event or span end. |
| `parentObservationId` | string | no | Tree parent; null = root. |
| `input` | any JSON | no | Stringified or object. |
| `output` | any JSON | no | Stringified or object. |
| `metadata` | object | no | Free-form. |
| `level` | enum | no | `DEFAULT` \| `DEBUG` \| `WARNING` \| `ERROR`. Default `DEFAULT`. |
| `statusMessage` | string | no | Free-form, typical on level=ERROR. |
| `version` | string | no | Mirrors trace.version; used for drift analytics. |
| `environment` | string | no | Same rules as trace.environment. |

### 1.4 Generation-only fields

Generation extends SPAN with LLM-specific attributes:

| Field | Type | Required | Notes |
|---|---|---|---|
| `model` | string | no | Model name, e.g. `gpt-4o`. Used to match the Model Registry for cost calc. |
| `modelParameters` | object | no | `{ temperature, top_p, max_tokens, ... }`. Values stringified in legacy ingestion; JSON in OTel path. |
| `completionStartTime` | ISO-8601 | no | Time first token was received (TTFT anchor). |
| `prompt` | `{ name, version }` | no | Link to Prompt Registry entry. |
| `usage` (v2, legacy) | `{ input, output, total, unit, inputCost, outputCost, totalCost }` | no | `unit` ∈ `TOKENS` \| `CHARACTERS` \| `MILLISECONDS` \| `SECONDS` \| `IMAGES` \| `REQUESTS`. |
| `usageDetails` (v3+) | `{ [key: string]: number }` | no | Free-form counters: `input`, `output`, `total`, `input_cache_read`, `input_cache_creation`, `input_audio`, `output_reasoning`, `cache_read_input_tokens`, `cache_creation_input_tokens`, etc. Keys drive pricing. |
| `costDetails` (v3+) | `{ [key: string]: number }` | no | USD per key, same shape as usageDetails. If omitted, server computes from Model Registry prices × usageDetails. |

Legacy cost fields on the `usage` object (`inputCost`, `outputCost`, `totalCost`) are still accepted but `costDetails` is preferred in v3+.

### 1.5 Event-only fields

Same as base observation minus `endTime` — events are instantaneous. In OTel mode, events map to zero-duration spans or to OTel span-events, depending on the attribute `langfuse.observation.type`.

### 1.6 Score fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Idempotent. |
| `name` | string | yes | Metric name. |
| `value` | number \| string | yes | Number for NUMERIC/BOOLEAN, string for CATEGORICAL/TEXT. |
| `dataType` | enum | no | `NUMERIC` \| `CATEGORICAL` \| `BOOLEAN` \| `TEXT`. Inferred from value if omitted. |
| `traceId` | string | conditional | Required unless `sessionId` or `datasetRunId` is set. |
| `observationId` | string | no | Attach score to specific observation. |
| `sessionId` | string | no | Session-level score. |
| `datasetRunId` | string | no | Dataset-experiment score. |
| `comment` | string | no | Human comment / reasoning. |
| `configId` | string | no | Reference to score config (schema, range). |
| `metadata` | object | no | |
| `timestamp` | ISO-8601 | no | |
| `environment` | string | no | |

Note: the earlier `CORRECTION` data type from v1 ingestion is deprecated in favor of TEXT.

---

## 2. Python SDK

### 2.1 Installation & versioning

```bash
pip install langfuse            # v3 (stable) / v4 (March 2026)
```

- v2: single-threaded client, manual trace()/span()/generation().
- v3: introduces `get_client()` singleton, `@observe` decorator, `start_as_current_observation`, context helpers. Works with or without OTel.
- v4 (March 2026): full rewrite on top of **OpenTelemetry**. Internally every instrumentation call starts an OTel span with Langfuse-prefixed attributes (`langfuse.*`, `gen_ai.*`); these are exported via `LangfuseSpanProcessor` -> OTLP HTTP to `/api/public/otel`. The public surface (`@observe`, `start_as_current_observation`, `update_current_*`) is preserved — only the wire path changed.

### 2.2 Client initialization

```python
from langfuse import Langfuse, get_client

# Env-based singleton (recommended):
#   LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL or LANGFUSE_HOST
langfuse = get_client()

# Direct construction:
langfuse = Langfuse(
    public_key="pk-lf-...",
    secret_key="sk-lf-...",
    base_url="https://cloud.langfuse.com",  # or LANGFUSE_HOST
    timeout=None,  # httpx timeout
    httpx_client=None,  # inject shared client
    debug=False,
    tracing_enabled=True,  # master switch
    flush_at=None,  # batch size (default 15)
    flush_interval=None,  # seconds (default 0.5)
    environment=None,  # stamped on every event
    release=None,
    media_upload_thread_count=None,
    sample_rate=None,  # 0.0..1.0 head-based sampling
    mask=None,  # MaskFunction: (data)->data
    blocked_instrumentation_scopes=None,  # OTel scope names to drop
    should_export_span=None,  # lambda ReadableSpan -> bool
    additional_headers=None,  # dict, added to every HTTP call
    tracer_provider=None,  # bring-your-own TracerProvider
    span_exporter=None,  # bring-your-own SpanExporter
)

langfuse.auth_check()  # -> bool
```

### 2.3 `@observe` decorator

```python
from langfuse import observe


@observe(
    name=None,  # defaults to func.__name__
    as_type=None,  # "span" (default) | "generation" | "agent" |
    # "tool" | "chain" | "retriever" |
    # "embedding" | "evaluator" | "guardrail"
    capture_input=None,  # default True; env LANGFUSE_CAPTURE_INPUT
    capture_output=None,  # default True
    transform_to_string=None,  # (Iterable) -> str for generators
)
def my_fn(x, y): ...
```

Special reserved kwargs handled by the decorator (stripped before the wrapped function is called):
- `langfuse_trace_id` — force a specific trace id (enables deterministic linking).
- `langfuse_parent_observation_id` — attach as child of an existing observation.
- `langfuse_public_key` — multi-tenant key override.

Behavior:
- Detects sync vs async vs sync-generator vs async-generator vs `StreamingResponse`. Generators are wrapped to collect chunks and set `output` on close.
- On exception: sets `level=ERROR`, `status_message=<exception repr>`, records span exception event, re-raises.
- Preserves signature via `functools.wraps`.

### 2.4 Low-level observation API (v3/v4)

```python
# Context-managed (auto-closes):
with langfuse.start_as_current_observation(
    as_type="span",  # or "generation", "tool", ...
    name="process-request",
    input=...,
    output=...,
    metadata=...,
    version=...,
    level="DEFAULT",
    status_message=None,
    completion_start_time=None,  # GENERATION
    model=None,
    model_parameters=None,  # GENERATION
    usage_details=None,
    cost_details=None,  # GENERATION
    prompt=None,  # GENERATION: PromptClient instance
    trace_context=None,  # {"trace_id": ..., "parent_span_id": ...}
    end_on_exit=True,
) as span:
    span.update(output="...", metadata={...})
    # Nested:
    with langfuse.start_as_current_observation(as_type="generation", name="llm") as gen:
        gen.update(model="gpt-4o", usage_details={"input": 10, "output": 42})

# Manual (must call .end() or .update(end_time=...)):
span = langfuse.start_observation(as_type="span", name="x")
span.end()

# Instantaneous event:
langfuse.create_event(name="cache_hit", input={...}, metadata={...})
```

### 2.5 Context helpers (ambient, no handle needed)

```python
langfuse.update_current_span(
    name=None, input=None, output=None, metadata=None, version=None, level=None, status_message=None
)

langfuse.update_current_generation(
    name=None,
    input=None,
    output=None,
    metadata=None,
    version=None,
    level=None,
    status_message=None,
    completion_start_time=None,
    model=None,
    model_parameters=None,
    usage_details=None,
    cost_details=None,
    prompt=None,
)

langfuse.update_current_trace(
    name=None,
    user_id=None,
    session_id=None,
    tags=None,
    metadata=None,
    release=None,
    version=None,
    input=None,
    output=None,
    public=None,
)

langfuse.get_current_trace_id()  # -> Optional[str]
langfuse.get_current_observation_id()  # -> Optional[str]
langfuse.create_trace_id(seed=None)  # -> str  (deterministic if seed given)
langfuse.create_observation_id(seed=None)
langfuse.get_trace_url(trace_id=None)  # -> UI deep link

langfuse.set_current_trace_as_public()
langfuse.set_current_trace_io(input, output)  # deprecated, use update_current_trace

langfuse.flush()  # blocking flush of pending events
langfuse.shutdown()  # flush + stop background threads; idempotent
```

### 2.6 Scoring from the SDK

```python
langfuse.create_score(
    name="helpfulness",
    value=0.8,
    trace_id=None,
    observation_id=None,
    session_id=None,
    dataset_run_id=None,
    score_id=None,
    data_type="NUMERIC",  # NUMERIC | CATEGORICAL | BOOLEAN | TEXT
    comment=None,
    config_id=None,
    metadata=None,
    timestamp=None,
)

langfuse.score_current_span(name="latency_ok", value=1, data_type="BOOLEAN")
langfuse.score_current_trace(name="overall", value=0.9)
```

### 2.7 V2 legacy method surface (still present for migration)

```python
trace = langfuse.trace(name="my-trace", user_id=..., session_id=..., metadata=..., tags=...)
span = trace.span(name="step1", input=...)
gen = span.generation(
    name="llm", model="gpt-4o", input=..., output=..., usage={"input": 10, "output": 20, "unit": "TOKENS"}
)
span.event(name="checkpoint")
trace.update(output=...)
```

These now forward to the v3/v4 observation model under the hood.

### 2.8 OTel mapping in v4

When the SDK runs, each call to `start_as_current_observation` does:
1. Start an OTel span via the underlying `TracerProvider`.
2. Set attributes:
   - `langfuse.observation.type` ← `as_type`
   - `langfuse.observation.input` / `.output` ← JSON-serialized values
   - `langfuse.observation.metadata.*` ← flattened metadata keys
   - `langfuse.observation.level`, `.status_message`, `.version`
   - `langfuse.observation.prompt.name` / `.prompt.version`
   - `gen_ai.request.model`, `gen_ai.request.*` for model params
   - `gen_ai.response.model`
   - `gen_ai.usage.input_tokens` / `.output_tokens` / custom `gen_ai.usage.*`
   - `langfuse.trace.name`, `.tags`, `.metadata.*`, `.public`
   - `user.id`, `session.id` (and their `langfuse.user.id` / `langfuse.session.id` aliases)
   - `langfuse.release`
3. On span end, `LangfuseSpanProcessor` exports via OTLP-HTTP to `/api/public/otel/v1/traces`.

The server parses these attributes with the rules in §5. OpenInference spans (`openinference.*`, `input.value`, `output.value`, `llm.model_name`, `llm.token_count.*`) are accepted alongside `gen_ai.*`.

---

## 3. JS/TS SDK

Packages (v5, March 2026):

| Package | Purpose |
|---|---|
| `@langfuse/client` | REST client (prompts, datasets, scores, ingestion fallback). Universal JS (Node, edge, browser). |
| `@langfuse/tracing` | OTel-based instrumentation (`observe`, `startActiveObservation`, …). Node 20+. |
| `@langfuse/otel` | `LangfuseSpanProcessor` for `@opentelemetry/sdk-node`. Node 20+. |
| `@langfuse/openai` | Drop-in wrapper for the OpenAI Node SDK. |
| `@langfuse/langchain` | `CallbackHandler` for LangChain.js. |

### 3.1 Setup

```ts
import { NodeSDK } from "@opentelemetry/sdk-node";
import { LangfuseSpanProcessor } from "@langfuse/otel";

const langfuseSpanProcessor = new LangfuseSpanProcessor({
  publicKey: process.env.LANGFUSE_PUBLIC_KEY!,
  secretKey: process.env.LANGFUSE_SECRET_KEY!,
  baseUrl:   process.env.LANGFUSE_BASE_URL,
  environment: "production",
  release: process.env.GIT_SHA,
  shouldExportSpan: (span) => !span.name.startsWith("ignore-"),
  mask: (payload) => payload, // PII redaction
  flushAt: 15,
  flushInterval: 500,
});

const sdk = new NodeSDK({ spanProcessors: [langfuseSpanProcessor] });
sdk.start();

// Flush for short-lived processes:
await langfuseSpanProcessor.forceFlush();
await langfuseSpanProcessor.shutdown();
```

### 3.2 Instrumentation API (`@langfuse/tracing`)

```ts
import {
  observe,
  startActiveObservation,
  startActiveSpan,
  startActiveGeneration,
  startObservation,
  updateActiveObservation,
  updateActiveTrace,
  getActiveTraceId,
  getActiveObservationId,
  createTraceId,
} from "@langfuse/tracing";

// Decorator-like wrapper:
const tracedFn = observe(async (x: number) => x * 2, {
  name: "double",
  asType: "span",           // "span" | "generation" | "agent" | "tool" | "chain" |
                            // "retriever" | "embedding" | "evaluator" | "guardrail"
  captureInput: true,
  captureOutput: true,
});

// Context-managed observation:
await startActiveObservation("process-request", async (span) => {
  span.update({ input, metadata: { foo: "bar" } });

  await startActiveGeneration("llm", async (gen) => {
    gen.update({
      model: "gpt-4o",
      modelParameters: { temperature: 0.2 },
      input: messages,
      output: completion,
      usageDetails: { input: 10, output: 42, total: 52 },
    });
  }, { asType: "generation" });
});

// Ambient updates (no handle):
updateActiveObservation({ output: "...", level: "ERROR", statusMessage: "boom" });
updateActiveTrace({ userId: "u_123", sessionId: "s_abc", tags: ["prod"] });

// Manual (no context):
const span = startObservation("raw-span", { asType: "span" });
span.end();
```

### 3.3 `LangfuseClient` — prompts, datasets, scores

```ts
import { LangfuseClient } from "@langfuse/client";
const langfuse = new LangfuseClient({ publicKey, secretKey, baseUrl });

// Prompts
await langfuse.prompt.create({ name, type: "text"|"chat", prompt, labels, tags, config });
const p = await langfuse.prompt.get("movie-critic", { version, label, cacheTtlSeconds });
p.compile({ movie: "Dune 2" });

// Scores
await langfuse.score.create({ name, value, traceId, observationId, dataType, comment, configId });

// Datasets
await langfuse.dataset.create({ name, description });
await langfuse.dataset.createItem({ datasetName, input, expectedOutput, metadata });
const ds = await langfuse.dataset.get("qa-set");
for (const item of ds.items) { /* run experiment */ }
```

### 3.4 LangChain.js CallbackHandler

```ts
import { CallbackHandler } from "@langfuse/langchain";
const handler = new CallbackHandler({
  userId: "u_1", sessionId: "s_1", tags: ["experiment"],
  metadata: { feature: "rag" },
});
await chain.invoke(input, { callbacks: [handler] });
```

Dynamic trace fields via run metadata: `langfuse_user_id`, `langfuse_session_id`, `langfuse_tags`.

---

## 4. Integrations (mechanism + usage)

### 4.1 OpenAI SDK drop-in (Python)

Mechanism: `langfuse.openai` subclasses `openai.OpenAI`/`AsyncOpenAI`/`AzureOpenAI`/`AsyncAzureOpenAI`, instruments every `chat.completions.create`, `completions.create`, `embeddings.create`, `responses.create`, including streaming (aggregates chunks; captures `completion_start_time` on first chunk).

```python
from langfuse.openai import openai  # module-level drop-in

# or
from langfuse.openai import OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI

client = OpenAI()
resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    # Extra kwargs consumed by the wrapper (not forwarded to OpenAI):
    name="classify",
    metadata={"feature": "x", "langfuse_session_id": "s1", "langfuse_user_id": "u1", "langfuse_tags": ["prod"]},
    trace_id=None,
    parent_observation_id=None,
    langfuse_prompt=prompt,  # PromptClient -> links trace to prompt version
)
```

### 4.2 LangChain CallbackHandler (Python)

Mechanism: a `BaseCallbackHandler` that listens to LangChain events (`on_llm_start`, `on_chain_start`, `on_tool_start`, `on_retriever_start`, …) and maps them to Langfuse spans/generations. v3+ pulls config from the singleton `get_client()`.

```python
from langfuse.langchain import CallbackHandler

handler = CallbackHandler()  # no args in v3+; uses ambient client

agent.invoke(
    {"messages": [...]},
    config={
        "callbacks": [handler],
        "metadata": {
            "langfuse_user_id": "u_1",
            "langfuse_session_id": "s_1",
            "langfuse_tags": ["tag-a"],
            "langfuse_prompt": prompt,  # links generations to prompt
        },
    },
)
```

### 4.3 LlamaIndex

Mechanism: not a native Langfuse package — uses `openinference-instrumentation-llama-index` to emit OpenInference-flavored OTel spans, which Langfuse's OTLP endpoint translates (see §5).

```python
from openinference.instrumentation.llama_index import LlamaIndexInstrumentor

LlamaIndexInstrumentor().instrument()
```

### 4.4 LiteLLM

Mechanism: LiteLLM has a built-in `"langfuse_otel"` callback that emits OTel spans to the LiteLLM OTel pipeline, shipped to `LANGFUSE_OTEL_HOST`. There is also a legacy `"langfuse"` callback that uses the ingestion API directly.

```yaml
# litellm_config.yaml
model_list:
  - model_name: gpt-4o
    litellm_params: { model: gpt-4o }
litellm_settings:
  callbacks: ["langfuse_otel"]
```
```bash
export LANGFUSE_PUBLIC_KEY=...
export LANGFUSE_SECRET_KEY=...
export LANGFUSE_OTEL_HOST=https://us.cloud.langfuse.com
```

### 4.5 Vercel AI SDK

Mechanism: AI SDK emits OTel spans natively when `experimental_telemetry` is enabled; `LangfuseSpanProcessor` picks them up.

```ts
import { generateText } from "ai";
await generateText({
  model: openai("gpt-4o"),
  prompt,
  experimental_telemetry: {
    isEnabled: true,
    functionId: "classify",
    metadata: {
      userId: "u1", sessionId: "s1",
      langfusePrompt: promptClient.toJSON(),
    },
  },
});
```

### 4.6 Haystack

Mechanism: `openinference-instrumentation-haystack` (OTel auto-instrumentation).

```python
from openinference.instrumentation.haystack import HaystackInstrumentor

HaystackInstrumentor().instrument()
```

### 4.7 Instructor

Mechanism: Instructor wraps an OpenAI client; use `langfuse.openai.OpenAI` as the wrapped client.

```python
import instructor
from langfuse.openai import OpenAI, AsyncOpenAI

client = instructor.patch(OpenAI())
aclient = instructor.apatch(AsyncOpenAI())
```

### 4.8 DSPy

Mechanism: `openinference-instrumentation-dspy` -> OTel.

```python
from openinference.instrumentation.dspy import DSPyInstrumentor

DSPyInstrumentor().instrument()
```

### 4.9 Mirascope

Mechanism: native `@with_langfuse()` decorator from `mirascope.integrations.langfuse` stacks on top of Mirascope's provider call decorators.

```python
from mirascope.integrations.langfuse import with_langfuse


@with_langfuse()
@anthropic.call(model="claude-3-5-sonnet-20240620")
@prompt_template("Recommend a {genre} book.")
def recommend_book(genre: str): ...
```

### 4.10 AWS Bedrock

Mechanism: no official auto-wrapper; idiomatic usage is `@observe(as_type="generation")` around a Bedrock Converse/`invoke_model` call, then `update_current_generation(model=..., usage_details=..., input=..., output=...)`.

```python
@observe(as_type="generation")
def bedrock_converse(messages):
    resp = bedrock_runtime.converse(modelId="anthropic.claude-3-sonnet-...", messages=messages)
    langfuse.update_current_generation(
        model="anthropic.claude-3-sonnet-...",
        input=messages,
        output=resp["output"]["message"],
        usage_details={
            "input": resp["usage"]["inputTokens"],
            "output": resp["usage"]["outputTokens"],
            "total": resp["usage"]["totalTokens"],
        },
    )
    return resp
```

Alternative: OpenInference's `openinference-instrumentation-bedrock` plus the OTLP endpoint.

### 4.11 Ollama

Mechanism: Ollama speaks the OpenAI wire protocol; use `langfuse.openai.OpenAI` pointed at `http://localhost:11434/v1`.

```python
from langfuse.openai import OpenAI

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
```

### 4.12 OpenTelemetry / OTLP endpoint

**Endpoint:** `POST {LANGFUSE_HOST}/api/public/otel/v1/traces` (HTTP/JSON or HTTP/protobuf; **gRPC not supported**).

**Auth:** HTTP Basic with `<public_key>:<secret_key>` base64-encoded in `Authorization: Basic ...`.

**Recommended headers:**
- `x-langfuse-ingestion-version: 4` — enables Fast Preview real-time display.
- `x-langfuse-sdk-name`, `x-langfuse-sdk-version`, `x-langfuse-public-key` — used by the native SDKs.

**Env setup for any OTel SDK:**
```bash
OTEL_EXPORTER_OTLP_ENDPOINT="https://cloud.langfuse.com/api/public/otel"   # EU
# OTEL_EXPORTER_OTLP_ENDPOINT="https://us.cloud.langfuse.com/api/public/otel" # US
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(echo -n "$PK:$SK" | base64 -w0),x-langfuse-ingestion-version=4"
```

**Attribute mapping (trace-level; set on any span and Langfuse pulls them to the Trace record):**

| Langfuse | OTel attributes (first match wins) |
|---|---|
| `name` | `langfuse.trace.name`, else root span name |
| `userId` | `langfuse.user.id`, `user.id` |
| `sessionId` | `langfuse.session.id`, `session.id` |
| `tags` | `langfuse.trace.tags` (string[]) |
| `metadata` | `langfuse.trace.metadata.*` (flattened) |
| `release` | `langfuse.release` |
| `version` | `langfuse.version` |
| `public` | `langfuse.trace.public` (bool) |
| `input` | `langfuse.trace.input` |
| `output` | `langfuse.trace.output` |
| `environment` | `langfuse.environment` |

**Attribute mapping (observation-level; per span):**

| Langfuse | OTel attributes |
|---|---|
| `type` | `langfuse.observation.type` (span/generation/agent/tool/chain/retriever/evaluator/embedding/guardrail/event) |
| `level` | `langfuse.observation.level` (DEFAULT/DEBUG/WARNING/ERROR), or derived from `span.status.code` (ERROR→ERROR, OK→DEFAULT) |
| `statusMessage` | `langfuse.observation.status_message`, else `span.status.message` |
| `version` | `langfuse.observation.version` |
| `input` | `langfuse.observation.input`, `gen_ai.prompt`, `input.value` (OpenInference), `llm.input_messages` |
| `output` | `langfuse.observation.output`, `gen_ai.completion`, `output.value`, `llm.output_messages` |
| `metadata` | `langfuse.observation.metadata.*` |
| `model` | `gen_ai.request.model`, `gen_ai.response.model`, `llm.model_name` (OpenInference) |
| `modelParameters` | `gen_ai.request.temperature`, `.top_p`, `.max_tokens`, `.presence_penalty`, `.frequency_penalty`, `.stop_sequences`; `llm.invocation_parameters.*` |
| `usageDetails.input` | `gen_ai.usage.input_tokens`, `gen_ai.usage.prompt_tokens`, `llm.token_count.prompt` |
| `usageDetails.output` | `gen_ai.usage.output_tokens`, `gen_ai.usage.completion_tokens`, `llm.token_count.completion` |
| `usageDetails.total` | `gen_ai.usage.total_tokens`, `llm.token_count.total` |
| `usageDetails.cache_read_input_tokens` | `gen_ai.usage.cache_read_input_tokens` |
| `usageDetails.cache_creation_input_tokens` | `gen_ai.usage.cache_creation_input_tokens` |
| `costDetails.total` | `gen_ai.usage.cost` |
| `prompt` | `langfuse.observation.prompt.name` + `.prompt.version` |
| `completionStartTime` | `langfuse.observation.completion_start_time` |

The parent/child tree is reconstructed from the standard OTel `parent_span_id`. OpenInference spans are recognized by presence of `openinference.span.kind` (LLM/CHAIN/AGENT/TOOL/RETRIEVER/EMBEDDING/RERANKER) and mapped to the corresponding Langfuse observation type.

Baggage: Langfuse recommends `BaggageSpanProcessor` so that `user.id`, `session.id`, `langfuse.trace.*` propagate across async boundaries.

---

## 5. Prompt management

- Registry keyed by `name`; each `create` increments `version` monotonically.
- Types: `text` (string with `{{var}}` placeholders) and `chat` (array of `{role, content}`).
- Labels: free-form strings; `production` is the default fetched by SDK when no label/version specified. Labels are unique across versions (promoting moves the label).
- Prompts can carry arbitrary `config` JSON (model, temperature, tools) consumed by the application.

**Create / get / compile (Python):**
```python
langfuse.create_prompt(
    name="movie-critic",
    type="text",
    prompt="As a {{criticlevel}} movie critic, do you like {{movie}}?",
    labels=["production"],
    tags=["film"],
    config={"model": "gpt-4o", "temperature": 0.3},
    commit_message="Initial version",
)

prompt = langfuse.get_prompt(
    "movie-critic",
    version=None,  # int, exact version
    label=None,  # str, e.g. "production" or "staging"
    cache_ttl_seconds=60,  # client-side cache
    fallback=None,  # PromptClient used if network fails
    max_retries=2,
    fetch_timeout_seconds=20,
)
compiled = prompt.compile(criticlevel="expert", movie="Dune 2")
```

**Linking to a generation** (so the UI and analytics join cost/latency per prompt version):
```python
with langfuse.start_as_current_observation(
    as_type="generation",
    name="llm",
    prompt=prompt,  # Python
) as gen:
    ...
```
In LangChain/OpenAI wrappers, pass `langfuse_prompt=prompt` (Python) or `langfusePrompt: prompt.toJSON()` in metadata (TS).

---

## 6. Scores / Evaluations

### 6.1 Score Configs
Optional schemas describing a metric: `name`, `dataType`, `isArchived`, and type-specific ranges (`minValue`/`maxValue` for NUMERIC, `categories: [{label, value}]` for CATEGORICAL). Each config has an `id` referenced by scores via `configId`.

### 6.2 Score object
See §1.6. Scores are written via `langfuse.create_score(...)` (Python), `langfuse.score.create(...)` (TS), or the ingestion event `score-create`. Idempotent on `id`.

### 6.3 LLM-as-judge (managed evaluators)
Configured via UI:
1. LLM Connection (OpenAI/Anthropic/Bedrock/Azure/custom OpenAI-compatible).
2. Evaluator (managed template or custom prompt).
3. Target: live traces, live observations, or dataset experiments.
4. Variable map (JSONPath expressions into trace JSON → prompt vars).
5. Sampling rate + filter.

The worker periodically pulls eligible traces, calls the judge LLM (requires structured output support), parses result, writes a `score-create` with `configId` pointing to the evaluator config.

### 6.4 Datasets & experiments

```python
ds = langfuse.get_dataset("qa-set")
# or: langfuse.create_dataset(...); langfuse.create_dataset_item(...)


def task(item):
    return my_app(item.input)


def evaluator(input, output, expected_output, metadata):
    return {"name": "exact_match", "value": 1.0 if output == expected_output else 0.0}


langfuse.run_experiment(
    name="qa-set",
    run_name="v12",
    description="baseline",
    data=ds.items,
    task=task,
    evaluators=[evaluator],
    composite_evaluator=None,
    run_evaluators=[],
    max_concurrency=5,
    metadata={"git_sha": "abc"},
)
```

Each item spawns a trace linked via `datasetRunId`; scores are auto-attached.

---

## 7. Sessions and users

- `sessionId` is a string ≤ 200 US-ASCII chars (dropped if longer). Set via `langfuse.update_current_trace(session_id=...)`, trace `sessionId` field, `langfuse.session.id` / `session.id` OTel attribute, or `metadata.langfuse_session_id` in integrations.
- `userId` works the same way (`user.id` / `langfuse.user.id`).
- In v3/v4 the SDK propagates these attributes across child observations so filtering works server-side even on partial trees.
- Sessions page in the UI supports session replay, public-link sharing, bookmarking, human annotation scores (attached via `sessionId` only, without `traceId`).
- `propagate_attributes()` context manager sets ambient trace attributes for all observations started inside it.

---

## 8. Self-hosting architecture

Components (typical Helm or docker-compose):

| Component | Role |
|---|---|
| **langfuse-web** (Next.js) | UI, public REST API, ingestion REST endpoint, OTLP endpoint. Stateless. |
| **langfuse-worker** (Node) | Consumes queues: ingestion→ClickHouse, evaluations, batch exports, blob uploads, dataset experiment runner. Stateless. |
| **Postgres** | Transactional: users, projects, API keys, prompts, score configs, datasets, evaluators. |
| **ClickHouse** | OLAP: traces, observations, scores — columnar, partitioned by project+day. Queried by UI aggregations. |
| **Redis / Valkey** | BullMQ queues (ingestion, events, evals, cloud-usage-metering) + rate-limit + auth cache. |
| **S3 / Blob** | Raw event envelopes (ingestion), multi-modal blobs (images/audio linked via `@@@langfuseMedia:*` references in input/output), exports. |
| **LLM Gateway** (optional) | For playground + LLM-as-judge if running without direct provider keys. |

**Ingestion pipeline:**
1. SDK POSTs batch to `/api/public/ingestion` (or OTel exporter posts to `/api/public/otel/v1/traces`).
2. `langfuse-web` validates JWT-style API key via Postgres+Redis cache.
3. Raw batch is uploaded to S3 under `projects/{id}/events/{date}/{eventId}.json`.
4. A reference `{eventId, s3Key, projectId}` is enqueued on Redis.
5. `langfuse-worker` consumes, parses, deduplicates on `(type, id)`, upserts into ClickHouse.
6. For OTel: spans are first normalized into the internal ingestion event shape (one OTel span → trace-create + span-create or generation-create, depending on `langfuse.observation.type`), then handed to the same pipeline.
7. Cost calc: worker resolves `model` against the Model Registry (Postgres), computes `costDetails` from `usageDetails` if not supplied.

**SDK-side batching:**
- Python v3/v4 flushes every `flush_at=15` events or `flush_interval=0.5s`, whichever first. Background thread (v3) or `BatchSpanProcessor` (v4 OTel).
- JS flushes via `LangfuseSpanProcessor` (wraps OTel BatchSpanProcessor) with configurable `flushAt`, `flushInterval`.
- Both support `mask` (PII redaction), sampling (`sample_rate`), and span-filter predicates (`should_export_span`).

---

## 9. Ingestion REST API

### 9.1 Endpoint

```
POST {host}/api/public/ingestion
Authorization: Basic base64(public_key:secret_key)
Content-Type: application/json
x-langfuse-sdk-name: python | js | ...
x-langfuse-sdk-version: 4.0.0
x-langfuse-public-key: pk-lf-...
```

Body:
```json
{
  "batch": [ IngestionEvent, IngestionEvent, ... ],
  "metadata": { /* optional */ }
}
```

**Response:** `207 Multi-Status`
```json
{
  "successes": [{ "id": "<eventId>", "status": 201 }],
  "errors":    [{ "id": "<eventId>", "status": 400, "message": "...", "error": "..." }]
}
```

Each event is processed independently; partial failures are per-event. Idempotent on event `id` AND on body `id` — re-delivering the same event is safe.

### 9.2 Event envelope

Every event in `batch` has:

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (uuid) | yes | Event id (envelope id). |
| `type` | string enum | yes | See §9.3. |
| `timestamp` | ISO-8601 | yes | Event emission time (used for audit/ordering). |
| `metadata` | any | no | SDK-only; not stored on the target entity. |
| `body` | object | yes | Shape depends on `type`. See §9.4. |

### 9.3 Event types

| Event `type` | Purpose | Body shape |
|---|---|---|
| `trace-create` | Create/upsert a Trace | TraceBody |
| `span-create` | Create a SPAN observation | SpanBody |
| `span-update` | Update a SPAN observation | SpanBody (id required) |
| `generation-create` | Create a GENERATION | GenerationBody |
| `generation-update` | Update a GENERATION | GenerationBody (id required) |
| `event-create` | Create an EVENT observation | EventBody |
| `observation-create` | (Legacy) Create any observation (discriminated by body.type) | ObservationBody |
| `observation-update` | (Legacy) Update any observation | ObservationBody |
| `score-create` | Create/upsert a Score | ScoreBody |
| `sdk-log` | Diagnostic log for SDK debugging | `{ log: string, level?: string }` |
| `agent-create` / `tool-create` / `chain-create` / `retriever-create` / `evaluator-create` / `embedding-create` / `guardrail-create` | Semantic span subtypes; server stores as SPAN with type set | SpanBody |

### 9.4 Body shapes

**TraceBody**
```
id           string     required (idempotent)
timestamp    ISO-8601   required
name         string     optional
userId       string     optional
sessionId    string     optional (<=200 ASCII)
input        any        optional
output       any        optional
metadata     any        optional
release      string     optional
version      string     optional
public       bool       optional
tags         string[]   optional
environment  string     optional (<=40, [a-z0-9_-], not "langfuse*")
```

**SpanBody**
```
id                    string     required
traceId               string     required (or the event must be paired with trace-create of same id)
parentObservationId   string     optional
name                  string     optional
startTime             ISO-8601   required on create
endTime               ISO-8601   optional (on update, usually set)
input                 any        optional
output                any        optional
metadata              any        optional
level                 enum       DEFAULT|DEBUG|WARNING|ERROR
statusMessage         string     optional
version               string     optional
environment           string     optional
```

**GenerationBody** (extends SpanBody)
```
completionStartTime   ISO-8601   optional
model                 string     optional
modelParameters       object     optional (keys: temperature, top_p, max_tokens, ...)
usage                 object     optional (legacy v2 shape):
  input | output | total         integer
  unit                           TOKENS|CHARACTERS|MILLISECONDS|SECONDS|IMAGES|REQUESTS
  inputCost | outputCost | totalCost  number
usageDetails          { [k]: number }   optional (v3+)
costDetails           { [k]: number }   optional (v3+)
promptName            string     optional
promptVersion         integer    optional
```

**EventBody** (extends SpanBody minus endTime; events are instantaneous — only startTime matters)

**ScoreBody**
```
id             string   required
name           string   required
value          number | string   required
dataType       NUMERIC|CATEGORICAL|BOOLEAN|TEXT   optional (inferred)
traceId        string   required unless sessionId or datasetRunId present
observationId  string   optional
sessionId      string   optional
datasetRunId   string   optional
comment        string   optional
configId       string   optional
metadata       any      optional
timestamp      ISO-8601 optional
environment    string   optional
```

**ObservationBody (legacy union)**
```
type           SPAN | GENERATION | EVENT   required
<all Span/Generation/Event fields as above, polymorphic on type>
```

Limits: batch max 3.5 MB (Cloud) / configurable self-hosted; event body should be <1 MB (large inputs go via multi-modal blob refs).

### 9.5 Media (multi-modal) sidechannel

Large binary content (images/audio/pdf) is not embedded in events. The SDK uploads via `POST /api/public/media` (returns a presigned URL), then embeds a reference token `@@@langfuseMedia:type=image/png|id=<mediaId>|source=base64_data_uri@@@` inside an `input`/`output` string. The UI resolves the reference at read time.

---

## 10. Summary cheat-sheet for library authors

- **Wire formats:** either (a) OTLP-HTTP to `/api/public/otel/v1/traces` with Langfuse + `gen_ai.*` attributes, or (b) JSON batch to `/api/public/ingestion`. Cloud accepts both. OTLP is the recommended forward path; ingestion remains the canonical model server-side.
- **Primary entities:** Trace → Observation (SPAN / GENERATION / EVENT). Extra semantic types (agent/tool/chain/retriever/evaluator/embedding/guardrail) are SPANs with `langfuse.observation.type`.
- **Cost/usage:** supply `usageDetails` (arbitrary keys including `input`, `output`, `total`, `input_cache_read`, `input_cache_creation`, `input_audio`, `output_reasoning`, …) and either `costDetails` (override) or rely on the Model Registry keyed on `model` string.
- **Identity:** idempotency on entity `id`. Event envelopes also have their own `id` — reposting the same batch is safe.
- **Batching:** 15 events / 500 ms defaults; mask hook, sampling, and span-filter predicate available at SDK level.
- **Auth:** HTTP Basic with `public_key:secret_key`. Projects are single-tenant per key pair.
- **Levels:** `DEFAULT | DEBUG | WARNING | ERROR`. ERROR-level observations feed the error dashboards and eval filters.
- **Session/User:** propagate `session.id` / `user.id` on every span (SDK does this automatically via OTel baggage); server unions them to the Trace record.
- **Prompts:** `name + version` (or `label`) identifies a prompt; link to a GENERATION to unlock prompt-level analytics.
- **Scores:** attach to `traceId`, `observationId`, `sessionId`, or `datasetRunId`; dataType NUMERIC/CATEGORICAL/BOOLEAN/TEXT; referenced by optional `configId`.

---

## Source references

- https://langfuse.com/docs/observability/data-model
- https://langfuse.com/docs/observability/features/observation-types
- https://langfuse.com/docs/sdk/python/sdk-v3
- https://langfuse.com/docs/sdk/python/decorators
- https://langfuse.com/docs/sdk/typescript/guide
- https://langfuse.com/docs/opentelemetry/get-started
- https://langfuse.com/docs/prompts/get-started
- https://langfuse.com/docs/scores/overview, /scores/model-based-evals
- https://langfuse.com/docs/integrations/{openai,langchain,llama-index,litellm,vercel-ai-sdk,haystack,instructor,dspy,mirascope,amazon-bedrock,ollama}
- https://langfuse.com/self-hosting
- https://api.reference.langfuse.com/
- https://github.com/langfuse/langfuse (server ingestion schemas)
- https://github.com/langfuse/langfuse-python (client.py, observe.py)
- https://github.com/langfuse/langfuse-js (packages/client, packages/tracing, packages/otel)
