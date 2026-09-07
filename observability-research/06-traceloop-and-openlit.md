# Traceloop (OpenLLMetry) and OpenLIT — Deep Research

Research notes on two OpenTelemetry-native LLM observability stacks. Both are Apache-2.0, both emit OTLP, both ship large sets of auto-instrumentors, and both co-publish attribute conventions via the `semantic-conventions-ai` package. They differ in focus: Traceloop is SDK-first with a SaaS backend and prompt/eval registry; OpenLIT ships a full self-hosted backend stack (ClickHouse + OTel Collector + UI) and leans into GPU/cost/guardrail features.

Sources consulted:
- GitHub raw: `traceloop/openllmetry` (sdk, semconv_ai, decorators, manual tracing, tracing.py), `traceloop/openllmetry-js`
- GitHub raw: `openlit/openlit` (sdk/python init, instrumentation dir listing, semcov, guard, evals, docker-compose)
- Docs sites (Traceloop docs, docs.openlit.io) partially — some pages were behind fetch restrictions
- PyPI `traceloop-sdk`, `openlit` pages blocked in this session

---

## Part A — Traceloop / OpenLLMetry

### A.1 What it is

OpenLLMetry is an OSS bundle maintained by Traceloop (Apache 2.0) consisting of:

1. **`traceloop-sdk`** (Python) / **`@traceloop/node-server-sdk`** (TS) — a thin wrapper over OpenTelemetry that bootstraps tracer/meter/logger providers, wires exporters, and registers dozens of `opentelemetry-instrumentation-*` packages in one call.
2. **A family of standalone instrumentors**, each a normal `opentelemetry.instrumentation.BaseInstrumentor` subclass. They are usable independently of `traceloop-sdk` — you can install only `opentelemetry-instrumentation-openai` and call `.instrument()` yourself.
3. **`opentelemetry-semantic-conventions-ai`** — a shared constants package (Python + JS) that defines the attribute names both Traceloop and OpenLIT emit.

Emission destinations:
- **Default**: Traceloop SaaS at `https://api.traceloop.com` (OTLP/HTTP, auth via `api_key` -> `Authorization` header).
- **Any OTLP endpoint**: set `TRACELOOP_BASE_URL` or pass `api_endpoint`/`exporter`. Traceloop documents 25+ backends (Datadog, Honeycomb, New Relic, Grafana Cloud, SigNoz, Splunk, Dynatrace, etc.) — all just OTLP.
- **Custom `SpanExporter`**: plug any exporter at init; SDK still wires instrumentors.

### A.2 `Traceloop.init` signature (Python)

Verbatim from `packages/traceloop-sdk/traceloop/sdk/__init__.py`:

```python
@staticmethod
def init(
    app_name: str = sys.argv[0],
    api_endpoint: str = "https://api.traceloop.com",
    api_key: Optional[str] = None,
    enabled: bool = True,
    headers: Dict[str, str] = {},
    disable_batch=False,
    telemetry_enabled: bool = True,
    exporter: Optional[SpanExporter] = None,
    metrics_exporter: MetricExporter = None,
    metrics_headers: Dict[str, str] = None,
    logging_exporter: LogExporter = None,
    logging_headers: Dict[str, str] = None,
    processor: Optional[Union[SpanProcessor, List[SpanProcessor]]] = None,
    propagator: TextMapPropagator = None,
    sampler: Optional[Sampler] = None,
    traceloop_sync_enabled: bool = False,
    should_enrich_metrics: bool = True,
    resource_attributes: dict = {},
    instruments: Optional[Set[Instruments]] = None,
    block_instruments: Optional[Set[Instruments]] = None,
    image_uploader: Optional[ImageUploader] = None,
    span_postprocess_callback: Optional[Callable[[ReadableSpan], None]] = None,
    endpoint_is_traceloop: Optional[bool] = False,
) -> Optional[Client]:
```

What it wires:

| Parameter | Purpose |
|---|---|
| `app_name` | Becomes `service.name` resource attribute; defaults to `sys.argv[0]`. |
| `api_endpoint` / `api_key` / `headers` | OTLP exporter target + `Authorization` header. |
| `enabled` | Master kill-switch; when false nothing is registered. |
| `disable_batch` | Swaps `BatchSpanProcessor` for `SimpleSpanProcessor` (dev mode). |
| `exporter` | User-supplied `SpanExporter`; overrides OTLP default. |
| `metrics_exporter`, `logging_exporter` | Same for metrics/logs pipelines. |
| `processor` | One or a list of `SpanProcessor`s added before the exporter. |
| `propagator` | Custom `TextMapPropagator` (default is composite W3C). |
| `sampler` | OTel `Sampler` implementation. |
| `traceloop_sync_enabled` | Periodic pull of prompt-registry + remote config from Traceloop SaaS. |
| `should_enrich_metrics` | Adds derived metrics (latency histograms, token counters). |
| `resource_attributes` | Merged into the OTel `Resource`. |
| `instruments` / `block_instruments` | Allow-list / deny-list over the built-in `Instruments` enum. |
| `image_uploader` | Strategy for exfiltrating image payloads out-of-band (for multimodal). |
| `span_postprocess_callback` | Hook called on every `ReadableSpan` before export. |
| `endpoint_is_traceloop` | Turns on Traceloop-only auth nuances. |

JS equivalent (`@traceloop/node-server-sdk`):

```ts
import * as traceloop from "@traceloop/node-server-sdk";
traceloop.initialize({ disableBatch: true });
```

### A.3 Decorators — `@workflow`, `@task`, `@agent`, `@tool`

Implemented as aliases over a single helper `entity_method` in `traceloop/sdk/decorators/base.py`:

```python
entity_method(
    name: Optional[str] = None,
    version: Optional[int] = None,
    tlp_span_kind: Optional[TraceloopSpanKindValues] = TraceloopSpanKindValues.TASK
)
```

| Decorator | `tlp_span_kind` | Side-effects beyond span attributes |
|---|---|---|
| `@workflow` | `WORKFLOW` | Calls `set_workflow_name(name)`, which seeds `traceloop.workflow.name` on every descendant span via OTel context. |
| `@task` | `TASK` | Sets `traceloop.entity.path` using chained-entity path helper. |
| `@agent` | `AGENT` | Same as workflow + `set_agent_name(name)`. |
| `@tool` | `TOOL` | Sets `gen_ai.tool.name` in addition to entity attrs. |

Span attributes each decorator sets:

| Attribute | Source |
|---|---|
| `traceloop.span.kind` | `tlp_span_kind.value` (`workflow`/`task`/`agent`/`tool`/`unknown`) |
| `traceloop.entity.name` | `name` argument (or function name) |
| `traceloop.entity.version` | `version` argument if provided |
| `traceloop.entity.path` | Dotted path of parent entities for task/tool |
| `traceloop.entity.input` | JSON-serialised args (when content tracing enabled) |
| `traceloop.entity.output` | JSON-serialised return value (when content tracing enabled) |
| `gen_ai.tool.name` | Only for `@tool` |
| `traceloop.workflow.name` | Propagated via context from nearest `@workflow`/`@agent` |

Supports sync, async, and async-generator functions. `enable_content_tracing` (global/allowlist) gates input/output serialization.

Example:

```python
from traceloop.sdk import Traceloop
from traceloop.sdk.decorators import workflow, task, agent, tool

Traceloop.init(app_name="joke_generation_service", disable_batch=True)


@tool(name="fetch_topic")
def fetch_topic() -> str: ...


@task(name="draft_joke")
def draft_joke(topic: str) -> str: ...


@agent(name="joke_writer")
def run_agent(topic: str) -> str:
    return draft_joke(topic)


@workflow(name="joke_creation")
def create_joke():
    return run_agent(fetch_topic())
```

### A.4 Manual span API — `traceloop.sdk.tracing.manual`

For paths not covered by auto-instrumentation. Core pieces:

```python
class LLMMessage(BaseModel):
    role: str
    content: str


class LLMUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None


class LLMSpan:
    def __init__(self, span: Span): ...
    def report_request(self, model: str, messages: list[LLMMessage]): ...
    def report_response(self, model: str, completions: list[str]): ...
    def report_usage(self, usage: LLMUsage): ...


@contextmanager
def track_llm_call(vendor: str, type: str) -> Iterator[LLMSpan]: ...
```

Usage:

```python
from traceloop.sdk.tracing.manual import track_llm_call, LLMMessage, LLMUsage

with track_llm_call(vendor="openai", type="chat") as span:
    span.report_request(model="gpt-4", messages=[LLMMessage(role="user", content="Hello")])
    # ... call your LLM ...
    span.report_response(model="gpt-4", completions=["Hi there!"])
    span.report_usage(LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
```

Under the hood this sets `gen_ai.system`, `llm.request.type`, indexed `gen_ai.prompt.{i}.role`/`.content`, `gen_ai.completion.{i}.role`/`.content`, and the token-usage attributes.

### A.5 Shipped instrumentors

All are published as independent PyPI packages of the form `opentelemetry-instrumentation-<vendor>` and enumerated in `Instruments` for init-time filtering. Observed list registered in `traceloop/sdk/tracing/tracing.py`:

**LLM providers (16+):** OpenAI, Anthropic, Cohere, Bedrock, SageMaker, Google Generative AI, Vertex AI, Mistral AI, Groq, Together AI, Ollama, Replicate, Watsonx, Aleph Alpha, Transformers (HF), Voyage AI, Writer.

**Frameworks:** LangChain, LlamaIndex, Haystack, CrewAI, OpenAI Agents, Agno, MCP (Model Context Protocol).

**Vector DBs:** Pinecone, Chroma, Qdrant, Weaviate, Milvus, LanceDB, Marqo.

**Infra / core OTel passthroughs:** Requests, urllib3, SQLAlchemy, Redis, Threading.

Each instrumentor can also be used standalone without `traceloop-sdk`:

```python
from opentelemetry.instrumentation.openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument()
```

### A.6 Semantic conventions

Traceloop predates the OTel GenAI semconv. It defined `llm.*` and `traceloop.*`, then moved to `gen_ai.*` as OTel standardized. The current `opentelemetry-semantic-conventions-ai` package reflects this migration: newer constants point at `gen_ai.*` values, while legacy `LLM_*` constants are kept as aliases.

Key attribute families (from `semconv_ai/__init__.py`):

**Traceloop-namespaced (workflow/entity model):**

| Constant | Value |
|---|---|
| `TRACELOOP_SPAN_KIND` | `traceloop.span.kind` |
| `TRACELOOP_WORKFLOW_NAME` | `traceloop.workflow.name` |
| `TRACELOOP_ENTITY_NAME` | `traceloop.entity.name` |
| `TRACELOOP_ENTITY_PATH` | `traceloop.entity.path` |
| `TRACELOOP_ENTITY_VERSION` | `traceloop.entity.version` |
| `TRACELOOP_ENTITY_INPUT` | `traceloop.entity.input` |
| `TRACELOOP_ENTITY_OUTPUT` | `traceloop.entity.output` |
| `TRACELOOP_ASSOCIATION_PROPERTIES` | `traceloop.association.properties` |
| `TRACELOOP_CORRELATION_ID` | `traceloop.correlation.id` (deprecated) |

**Prompt registry:**

| Constant | Value |
|---|---|
| `TRACELOOP_PROMPT_MANAGED` | `traceloop.prompt.managed` |
| `TRACELOOP_PROMPT_KEY` | `traceloop.prompt.key` |
| `TRACELOOP_PROMPT_VERSION` | `traceloop.prompt.version` |
| `TRACELOOP_PROMPT_VERSION_NAME` | `traceloop.prompt.version_name` |
| `TRACELOOP_PROMPT_VERSION_HASH` | `traceloop.prompt.version_hash` |
| `TRACELOOP_PROMPT_TEMPLATE` | `traceloop.prompt.template` |
| `TRACELOOP_PROMPT_TEMPLATE_VARIABLES` | `traceloop.prompt.template_variables` |

**GenAI (aligned with OTel GenAI semconv):**

| Constant | Value |
|---|---|
| `LLM_SYSTEM` | `gen_ai.system` |
| `LLM_REQUEST_MODEL` | `gen_ai.request.model` |
| `LLM_REQUEST_MAX_TOKENS` | `gen_ai.request.max_tokens` |
| `LLM_REQUEST_TEMPERATURE` | `gen_ai.request.temperature` |
| `LLM_REQUEST_TOP_P` | `gen_ai.request.top_p` |
| `LLM_PROMPTS` | `gen_ai.prompt` (indexed `gen_ai.prompt.{i}.role`/`.content`) |
| `LLM_COMPLETIONS` | `gen_ai.completion` (indexed similarly) |
| `LLM_RESPONSE_MODEL` | `gen_ai.response.model` |
| `LLM_USAGE_COMPLETION_TOKENS` | `gen_ai.usage.completion_tokens` |
| `LLM_USAGE_PROMPT_TOKENS` | `gen_ai.usage.prompt_tokens` |
| `GEN_AI_USAGE_TOTAL_TOKENS` | `gen_ai.usage.total_tokens` |
| `GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS` | `gen_ai.usage.cache_creation.input_tokens` |
| `GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS` | `gen_ai.usage.cache_read.input_tokens` |
| `GEN_AI_IS_STREAMING` | `gen_ai.is_streaming` |
| `GEN_AI_RESPONSE_FINISH_REASON` | `gen_ai.response.finish_reason` |
| `GEN_AI_USAGE_REASONING_TOKENS` | `gen_ai.usage.reasoning_tokens` |
| `GEN_AI_REQUEST_REASONING_EFFORT` | `gen_ai.request.reasoning_effort` |
| `GEN_AI_REQUEST_N` | `gen_ai.request.n` |
| `GEN_AI_REQUEST_MAX_COMPLETION_TOKENS` | `gen_ai.request.max_completion_tokens` |
| `GEN_AI_REQUEST_STRUCTURED_OUTPUT_SCHEMA` | `gen_ai.request.structured_output_schema` |
| `GEN_AI_TASK_ID` | `gen_ai.task.id` |
| `GEN_AI_WORKFLOW_NODES` | `gen_ai.workflow.nodes` |
| `GEN_AI_WORKFLOW_EDGES` | `gen_ai.workflow.edges` |

**Provider-specific extensions** (still under `gen_ai.*` or `llm.<vendor>.*`):

| Constant | Value |
|---|---|
| `GEN_AI_OPENAI_API_BASE` | `gen_ai.openai.api_base` |
| `GEN_AI_OPENAI_API_VERSION` | `gen_ai.openai.api_version` |
| `GEN_AI_OPENAI_API_TYPE` | `gen_ai.openai.api_type` |
| `LLM_OPENAI_RESPONSE_SYSTEM_FINGERPRINT` | `gen_ai.openai.system_fingerprint` |
| `GEN_AI_WATSONX_DECODING_METHOD` | `llm.watsonx.decoding_method` |
| `GEN_AI_WATSONX_RANDOM_SEED` | `llm.watsonx.random_seed` |
| `GEN_AI_WATSONX_MAX_NEW_TOKENS` | `llm.watsonx.max_new_tokens` |
| `LLM_FREQUENCY_PENALTY` | `llm.frequency_penalty` |
| `LLM_PRESENCE_PENALTY` | `llm.presence_penalty` |
| `LLM_TOP_K` | `llm.top_k` |
| `LLM_CHAT_STOP_SEQUENCES` | `llm.chat.stop_sequences` |
| `LLM_REQUEST_FUNCTIONS` | `llm.request.functions` |
| `LLM_REQUEST_TYPE` | `llm.request.type` |

**Vector DB (shared):**

| Constant | Value |
|---|---|
| `VECTOR_DB_VENDOR` | `db.system` |
| `VECTOR_DB_OPERATION` | `db.operation` |
| `VECTOR_DB_QUERY_TOP_K` | `db.vector.query.top_k` |

Plus 100+ vendor-specific VDB attrs: `PINECONE_QUERY_*`, `PINECONE_USAGE_READ_UNITS`/`WRITE_UNITS`, `CHROMADB_*` (~37), `MILVUS_*` (~60), `QDRANT_*`, `MARQO_*`.

**MCP:** `MCP_METHOD_NAME`, `MCP_RESPONSE_VALUE`, and three more.

**LangGraph:** `LANGGRAPH_COMMAND_SOURCE_NODE`, `LANGGRAPH_COMMAND_GOTO_NODE`, `LANGGRAPH_COMMAND_GOTO_NODES`.

**Enums:**

| Class | Values |
|---|---|
| `TraceloopSpanKindValues` | `WORKFLOW`, `TASK`, `AGENT`, `TOOL`, `UNKNOWN` |
| `LLMRequestTypeValues` | `COMPLETION`, `CHAT`, `RERANK`, `EMBEDDING`, `UNKNOWN` |
| `GenAISystem` | `OPENAI`, `ANTHROPIC`, `COHERE`, `MISTRALAI`, `OLLAMA`, `GROQ`, `ALEPH_ALPHA`, `REPLICATE`, `TOGETHER_AI`, `WATSONX`, `HUGGINGFACE`, `FIREWORKS`, `AZURE`, `AWS`, `GOOGLE`, `OPENROUTER`, `LANGCHAIN`, `CREWAI` |
| `GenAICustomOperationName` | `EXECUTE_TASK`, `LLM_REQUEST`, `VECTOR_DB_RETRIEVE` |
| `GenAITaskStatus` | `SUCCESS`, `FAILURE` |

**Metric names (from `Meters`):**

| Constant | Metric name |
|---|---|
| `LLM_GENERATION_CHOICES` | `gen_ai.client.generation.choices` |
| `LLM_TOKEN_USAGE` | `gen_ai.client.token.usage` |
| `LLM_OPERATION_DURATION` | `gen_ai.client.operation.duration` |
| `LLM_COMPLETIONS_EXCEPTIONS` | `llm.openai.chat_completions.exceptions` |
| `LLM_STREAMING_TIME_TO_GENERATE` | `llm.chat_completions.streaming_time_to_generate` |
| `DB_QUERY_DURATION` | `db.client.query.duration` |
| `DB_SEARCH_DISTANCE` | `db.client.search.distance` |
| `LLM_WATSONX_COMPLETIONS_DURATION` | `llm.watsonx.completions.duration` |

**v0.5.0 migration note (important for consumers):** Many `LLM_*` constants were either remapped to `gen_ai.*` values (string value changed) or kept their `llm.*` value with a renamed Python symbol. Old ingestion pipelines should double-check — e.g. `LLM_USAGE_CACHE_CREATION_INPUT_TOKENS` changed value from `llm.usage.cache_creation_input_tokens` to `gen_ai.usage.cache_creation_input_tokens`.

### A.7 Association properties

Primary correlation mechanism. Call:

```python
from traceloop.sdk import Traceloop

Traceloop.set_association_properties(
    {
        "user_id": "u_123",
        "session_id": "s_456",
        "tenant_id": "acme",
    }
)
```

Internals (`traceloop/sdk/tracing/tracing.py`): each key/value becomes a span attribute `traceloop.association.properties.<key>=<value>` on every span created within the current OTel context. Works as a scoped OTel context attach, so async tasks inherit it via `contextvars`. The Traceloop backend then uses these for slicing dashboards by user/session/tenant; any OTLP backend can do the same by attribute filtering.

Unlike OTel baggage, these are set as attributes (not propagated across process boundaries by default), though users can also add them to baggage manually.

### A.8 Prompt registry (Traceloop SaaS)

Enabled with `Traceloop.init(traceloop_sync_enabled=True)`. SaaS features observed through the attribute schema:

- Prompt templates are authored/versioned in the Traceloop web UI.
- Client SDK periodically pulls compiled templates.
- When a registered prompt is rendered, the emitting span carries `traceloop.prompt.managed=true`, `traceloop.prompt.key`, `traceloop.prompt.version`, `traceloop.prompt.version_name`, `traceloop.prompt.version_hash`, `traceloop.prompt.template`, and `traceloop.prompt.template_variables` (JSON).
- This closes the loop from prompt-source-of-truth to observed run, enabling A/B analysis by version.

### A.9 Evaluation / online monitoring

Traceloop SaaS layers online evals on top of ingested spans:
- Dashboards group by `traceloop.association.properties.*`, `traceloop.workflow.name`, `traceloop.entity.name`, `gen_ai.request.model`.
- "Monitors" trigger on token cost, latency, error rate, or custom eval scores.
- Online evaluators (faithfulness, QA relevance, toxicity) are executed server-side on sampled spans.
- The SDK's content allowlist governs which span inputs/outputs are exported in full (required for eval).

This is the primary feature separating the OSS SDK from the SaaS product. If you export to a non-Traceloop backend you get the spans and must run evals elsewhere.

---

## Part B — OpenLIT

### B.1 What it is

OpenLIT (Apache 2.0, openlit/openlit) is an OSS "AI Engineering Platform". Unlike Traceloop, it ships a full self-hostable backend:

- **SDK** (`openlit` on PyPI; also TypeScript, Go) — OTel-native, emits traces/metrics/logs via OTLP.
- **OpenTelemetry Collector** (configured in compose) — aggregates and routes to ClickHouse.
- **ClickHouse** — storage (native port 9000, HTTP 8123).
- **OpenLIT UI** — visualisation / dashboard / prompt hub / vault (port 3000; also hosts OTLP receivers on 4317/4318). Default login `user@openlit.io / openlituser`.

Feature pillars: analytics, cost tracking, GPU monitoring, Guardrails, Evals, Prompt Hub, Vault (secrets), OpenGround (model comparison), Fleet Hub (OpAMP collector management).

### B.2 `openlit.init` signature

From `sdk/python/src/openlit/__init__.py`:

```python
def init(
    environment="default",
    application_name="default",
    service_name="default",
    otlp_endpoint=None,
    otlp_headers=None,
    disable_batch=False,
    capture_message_content=True,
    disabled_instrumentors=None,
    disable_metrics=False,
    disable_events=False,
    pricing_json=None,
    collect_gpu_stats=False,
    collect_system_metrics=False,
    capture_db_parameters=False,
    evals_logs_export=True,
    max_content_length=None,
    custom_span_attributes=None,
    custom_metrics_attributes=None,
):
```

| Parameter | Notes |
|---|---|
| `environment` / `application_name` / `service_name` | Resource attrs (`deployment.environment`, `service.name`, etc.). |
| `otlp_endpoint` / `otlp_headers` | OTLP HTTP target. If `None`, uses OTel env vars. |
| `disable_batch` | Simple vs batch processor. |
| `capture_message_content` | Master toggle for prompt/completion text in spans (PII knob). |
| `disabled_instrumentors` | List of names to skip (`["openai", "pinecone"]`). |
| `disable_metrics` / `disable_events` | Separate toggles for metric and event pipelines. |
| `pricing_json` | URL or local path to a JSON with `{model: {input: $, output: $}}`. Used to compute `gen_ai.usage.cost`. |
| `collect_gpu_stats` | Starts a background thread using NVML for GPU metrics. |
| `collect_system_metrics` | Wires `opentelemetry-instrumentation-system-metrics`. |
| `capture_db_parameters` | Includes query bodies on VDB spans. |
| `evals_logs_export` | Emits eval results as OTel logs. |
| `max_content_length` | Truncates captured prompt/completion. |
| `custom_span_attributes` / `custom_metrics_attributes` | Dict merged onto every span/metric. |

Exposed helpers beyond `init`:

| Function | Purpose |
|---|---|
| `trace(fn)` | Decorator to wrap any callable in a span. |
| `start_trace(name)` | Context manager yielding `TracedSpan`. |
| `agent_context(name)` | Context manager tagging inner LLM calls with agent identity. |
| `using_attributes(attrs)` | Decorator/CM adding custom span attributes. |
| `inject_additional_attributes(fn, attrs)` | Run `fn` with attrs attached. |
| `log_agent_invocation(source, target, system)` | Records agent-to-agent hop. |
| `log_agent_tool_error(agent_name, tool_name, system, model)` | Structured tool failure. |
| `get_prompt()` | Pulls from OpenLIT Prompt Hub. |
| `get_secrets()` | Pulls from OpenLIT Vault. |
| `evaluate_rule()` | Evaluates a rule via Rule Engine. |

### B.3 Auto-instrumentors

Enumerated from `sdk/python/src/openlit/instrumentation/`:

**LLM/Gen providers:** `openai`, `anthropic`, `cohere`, `mistral`, `groq`, `google_ai_studio`, `vertexai`, `bedrock`, `azure_ai_inference`, `together`, `ollama`, `vllm`, `gpt4all`, `transformers`, `ai21`, `premai`, `reka`, `sarvam`, `elevenlabs`, `assemblyai`, `litellm`.

**Agent frameworks:** `ag2`, `agent_framework`, `agno`, `browser_use`, `claude_agent_sdk`, `controlflow`, `crewai`, `dynamiq`, `google_adk`, `julep`, `langchain`, `langgraph`, `letta`, `llamaindex`, `haystack`, `multion`, `openai_agents`, `pydantic_ai`, `smolagents`, `strands`.

**Memory/retrieval/crawl:** `mem0`, `crawl4ai`, `firecrawl`, `mcp`.

**Vector DBs:** `astra`, `chroma`, `milvus`, `pinecone`, `qdrant`, `psycopg` (pgvector).

**System:** `gpu` (NVML poller).

Plus via pyproject: web-server and HTTP-client passthroughs from the upstream OTel contrib (`asgi`, `django`, `fastapi`, `flask`, `pyramid`, `starlette`, `falcon`, `tornado`, `aiohttp-client`, `httpx`, `requests`, `urllib`, `urllib3`).

Overlap with Traceloop is substantial (OpenAI, Anthropic, etc.) but OpenLIT covers more agentic frameworks (Pydantic AI, smolagents, Agno, LangGraph first-class, Letta, Strands, browser_use) and more audio/media providers.

### B.4 Semantic conventions

OpenLIT uses a `SemanticConvention` class in `sdk/python/src/openlit/semcov/__init__.py`. Three tiers:

1. **Tier 1 — official OTel GenAI semconv** re-exported from `opentelemetry.semconv._incubating.attributes`: `gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, `gen_ai.server.request.duration`, `gen_ai.user.message`, `gen_ai.system.message`, `gen_ai.assistant.message`, `gen_ai.choice`, operation names (`text_completion`, `chat`, `embeddings`, `image`, `audio`, `translate`, `speech_to_text`), token types (`input`, `output`, `reasoning`), system names (`anthropic`, `openai`, `aws.bedrock`, `az.ai.openai`, `cohere`, `gemini`, `groq`, ...), plus `error.type` and `server.address`/`server.port`.

2. **Tier 2 — extensions overlapping with `semantic-conventions-ai`:** `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.retrieval.query.text`, `gen_ai.retrieval.documents`, `gen_ai.request.temperature`, `gen_ai.request.top_k`, `gen_ai.request.max_tokens`, and framework systems `langchain`, `llama_index`, `crewai`, `ag2`, `julep`, `langgraph`, `claude_agent_sdk`, plus OpenAI specifics `openai.request.service_tier`, `openai.response.system_fingerprint`, `openai.api.type`.

3. **Tier 3 — OpenLIT-specific:**

| Concern | Attributes |
|---|---|
| Cost | `gen_ai.usage.cost`, `gen_ai.token.cost.input`, `gen_ai.token.cost.output` |
| Agents | `gen_ai.agent.id`, `gen_ai.agent.tools`, `gen_ai.agent.max_retry_limit` |
| Vector DB | `db.system.name`, `db.collection.name`, `db.operation.name`, `db.query.text` |
| RAG/Eval | `gen_ai.rag.strategy`, `gen_ai.eval.context_relevancy`, `gen_ai.eval.groundedness` |
| Crawling | `gen_ai.crawl.depth`, `gen_ai.crawl.browser_type`, `gen_ai.extraction.strategy.type` |
| GPU | `gpu.utilization`, `gpu.memory.used`, `gpu.temperature`, `gpu.power.draw` |
| MCP | `mcp.method`, `mcp.tool.name`, `mcp.resource.uri`, `mcp.transport.type` |

Total declared >1000 constants.

Alignment: OpenLIT tracks OTel GenAI semconv closely (uses `db.system.name` — the newer db namespace — rather than legacy `db.system`). Where OTel has no attribute, they sit in `gen_ai.*` extensions that mirror or co-evolve with `semantic-conventions-ai`.

### B.5 GPU stats + cost

**GPU:** `collect_gpu_stats=True` starts a poller (NVML via `pynvml`) that emits metrics/gauges with attribute set `gpu.*`: `gpu.utilization`, `gpu.memory.used`, `gpu.memory.free`, `gpu.memory.total`, `gpu.temperature`, `gpu.power.draw`, plus tags `gpu.index`, `gpu.uuid`, `gpu.name`.

**Cost:** `pricing_json` points at a JSON file (defaults to OpenLIT's hosted one):

```json
{
  "chat": {
    "gpt-4o": {"promptPrice": 0.0025, "completionPrice": 0.01},
    "claude-3-5-sonnet-20241022": {"promptPrice": 0.003, "completionPrice": 0.015}
  },
  "embeddings": { "text-embedding-3-small": {"promptPrice": 0.00002} }
}
```

At span finish, SDK multiplies `gen_ai.usage.prompt_tokens` / `completion_tokens` by pricing and sets `gen_ai.usage.cost`, `gen_ai.token.cost.input`, `gen_ai.token.cost.output`. Self-hosted + fine-tuned models: supply your own pricing file.

### B.6 Guardrails / PII / prompt-injection

`openlit.guard` ships four detectors:

| Class | File |
|---|---|
| `PromptInjection` | `openlit/guard/prompt_injection.py` |
| `SensitiveTopic` | `openlit/guard/sensitive_topic.py` |
| `TopicRestriction` | `openlit/guard/restrict_topic.py` |
| `All` | `openlit/guard/all.py` — composite |

`PromptInjection.__init__` signature:

```python
PromptInjection(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    custom_rules: Optional[List[dict]] = None,
    custom_categories: Optional[Dict[str, str]] = None,
    threshold_score: float = 0.25,
    collect_metrics: Optional[bool] = None,
)
```

`detect(text: str) -> JsonOutput` returns `{score, verdict, classification, explanation}`. Runs deterministic rules first; if `provider` configured, also runs LLM-as-judge and picks the higher-confidence verdict. When `collect_metrics=True`, results are emitted as OTel metrics so you can alert on guard hit-rates.

### B.7 Evals

`openlit.evals` exports:

| Class | Purpose |
|---|---|
| `Hallucination` | Judges groundedness against provided context. |
| `BiasDetector` | Scores bias categories. |
| `ToxicityDetector` | Toxicity score + category. |
| `All` | Composite runner. |

Emission: each eval creates `emit_evaluation_event(...)` (from `openlit.evals.utils`) which writes an OTel log record / event with `gen_ai.eval.*` attributes. Backend (OpenLIT UI) then aggregates eval scores per trace.

The OpenLIT docs list 11 built-in eval types — in code four classes cover the main three (hallucination, bias, toxicity) plus `All`; the remaining (safety, instruction-following, completeness, conciseness, sensitivity, relevance, coherence, faithfulness) are implemented as sub-checks of `Hallucination`/`All` or as "LLM-as-judge" prompts.

### B.8 Self-hosted stack

From `docker-compose.yml`:

| Service | Ports | Role |
|---|---|---|
| `clickhouse` | 9000 (native), 8123 (HTTP) | Storage for traces/metrics/logs. |
| `openlit` | 3000 (UI), 4317 (OTLP gRPC), 4318 (OTLP HTTP) | UI + collector + API; depends on ClickHouse. Supports OAuth (Google/GitHub) + optional TLS. |

The OpenLIT container internally runs the OTel Collector plus Next.js UI; users often also deploy a standalone `otel-collector` as an extra tier in front for tail sampling, redaction, and fan-out to other backends.

Grafana dashboards are published (JSON) for users who prefer to query ClickHouse from Grafana directly rather than the OpenLIT UI.

---

## C. `semantic-conventions-ai` — shared convention package

Published twice:
- Python: `opentelemetry-semantic-conventions-ai` (lives in `traceloop/openllmetry/packages/opentelemetry-semantic-conventions-ai/`)
- JS/TS: `@traceloop/node-server-sdk` re-exports equivalents; also standalone `@traceloop/semantic-conventions-ai`

Owned by Traceloop's repo, but both Traceloop and OpenLIT instrumentors consume it (OpenLIT re-exports in its own `semcov` along with new constants). This is how the ecosystem converged on stable `traceloop.span.kind`, `traceloop.association.properties.*`, `traceloop.entity.*`, and `traceloop.prompt.*` — anything outside the OTel-GenAI charter.

Practical consequence: a backend reading one set can broadly render traces from the other, except:
- OpenLIT emits extra `gen_ai.usage.cost`, `gpu.*`, crawling/RAG eval attrs.
- Traceloop emits the full `traceloop.prompt.*` prompt-registry set.
- OpenLIT uses `db.system.name` / `db.operation.name` (newer OTel) vs Traceloop's `db.system` / `db.operation`.

## D. Relationship to OTel GenAI semconv and OpenInference

**OTel GenAI semconv** (in `opentelemetry-semantic-conventions` incubating):
- Defines `gen_ai.system`, `gen_ai.request.*`, `gen_ai.response.*`, `gen_ai.usage.*`, `gen_ai.operation.name`, plus events `gen_ai.user.message` / `gen_ai.assistant.message` / `gen_ai.system.message` / `gen_ai.choice`, metrics `gen_ai.client.token.usage` / `gen_ai.client.operation.duration`.
- Traceloop and OpenLIT both align core LLM request/response attrs to this. Traceloop still emits indexed `gen_ai.prompt.{i}.*` / `gen_ai.completion.{i}.*` on spans (legacy shape) in addition to (or instead of) the OTel-recommended log-event shape. OpenLIT more aggressively uses the log-events shape for prompts/completions.

**OpenInference** (Arize-maintained, used by Phoenix/Arize):
- Parallel but separate convention, centred on `input.value`/`output.value`/`input.mime_type` and `llm.model_name`, `llm.token_count.*`, `llm.prompt_template.template`, etc.
- Traceloop and OpenLIT do **not** use OpenInference attributes; some Arize instrumentors dual-emit both sets.
- Practical: a Traceloop span in Phoenix shows up but lacks the OpenInference mime/input fields Phoenix's UI expects — viewers fall back to the GenAI event shape.

Net: there are three overlapping attribute systems in flight. `semantic-conventions-ai` (shared by Traceloop/OpenLIT) is the de-facto OSS alignment for decorator/workflow/prompt/VDB concepts; OTel GenAI is authoritative for core request/response; OpenInference is Arize-specific.

---

## Implementation takeaways for a new library

- Adopt `gen_ai.*` OTel semconv for core LLM request/response and `db.*` (new) for VDB — it's what both camps converge on.
- Reuse `TRACELOOP_*` names from `semantic-conventions-ai` if you want a workflow/agent/tool model; it is already the de-facto shared vocabulary and most backends understand it.
- Set association properties as span attributes under `traceloop.association.properties.*` via OTel context — this is a lightweight, interoperable correlation pattern (user_id/session_id/tenant_id).
- Gate prompt/completion content capture with a single flag (Traceloop: `enable_content_tracing`; OpenLIT: `capture_message_content` + `max_content_length`). Default on in dev, off or truncated in prod.
- Cost calculation via a pluggable pricing JSON keyed by model id is the pragmatic approach; emit `gen_ai.usage.cost`.
- Keep instrumentors as standalone packages; the "SDK" should be a pure wiring layer.
- Ship a `disable_batch` path for dev so spans flush immediately.
- Distinguish `exporter` (custom final exporter) from `processor` (middle of pipeline) — both projects expose both.
- Provide a `span_postprocess_callback` / hook — Traceloop does, OpenLIT does it through `custom_span_attributes`; this is the user's only clean redaction seam before export.
