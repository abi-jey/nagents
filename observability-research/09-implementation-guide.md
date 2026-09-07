# Implementation Guide — Building Your Own Observability Library

This guide synthesizes all the research in this folder into actionable design decisions for building an observability library (`nagents`) compatible with the ecosystem.

> **Design sketches only.** The snippets below are incomplete pseudocode, not
> runnable examples or implemented nagents APIs. Context propagation, streaming
> span lifetimes, error cleanup, and privacy controls require implementation and
> tests before use. See [research status](00-overview.md).

## 1. What you need to implement (feature checklist)

### Core tracing
- [ ] **Span creation** with start/end timestamps, name, kind/type, status
- [ ] **Trace tree** via `trace_id` + `parent_id` propagation (context-based, not explicit)
- [ ] **Context propagation** — automatic parent discovery when spans nest
- [ ] **Async support** — async/await, generators, streaming
- [ ] **Decorator API** — `@trace`, `@span`, or kind-specific `@agent`, `@tool`, `@chain`
- [ ] **Context manager API** — `with tracer.span("name") as span:`
- [ ] **Manual span API** — `span = tracer.start_span(...)` / `span.end()`

### Span payload
- [ ] **Typed input/output** — store as structured JSON, not just string
- [ ] **Message array** (for LLM spans) — role, content, tool_calls
- [ ] **Token counts** — prompt, completion, total, cache_read, cache_write, reasoning
- [ ] **Cost** — USD values from token counts × pricing table
- [ ] **Model parameters** — temperature, top_p, max_tokens, etc.
- [ ] **Tool definitions** — what was advertised to the model
- [ ] **Tool calls** — what the model called
- [ ] **Retrieval documents** — id, content, score, metadata
- [ ] **Reranker** — input/output docs, query, top_k
- [ ] **Embedding** — model, text, vector
- [ ] **Finish reason**
- [ ] **Error** — exception type, message, stacktrace

### Context attributes (cross-cutting)
- [ ] `session_id` — conversation grouping
- [ ] `user_id` — user tracking
- [ ] `tags` — categorical labels
- [ ] `metadata` — free-form dict
- [ ] `version` / `release` — app version
- [ ] Prompt template name, variables, version

### Privacy controls
- [ ] Mask inputs (`hide_inputs`)
- [ ] Mask outputs (`hide_outputs`)
- [ ] Mask message content (`hide_input_messages`, `hide_output_messages`)
- [ ] Mask images (`hide_input_images`)
- [ ] Mask embedding vectors (`hide_embeddings_vectors`)
- [ ] Truncate base64 images (`base64_image_max_length`)
- [ ] Redaction sentinel (`__REDACTED__`)

### Export
- [ ] **OTLP HTTP exporter** (minimum viable — accepted by Phoenix, Langfuse, Arize)
- [ ] **OTLP gRPC exporter** (optional, higher throughput)
- [ ] **Batch processor** (async, non-blocking SDK)
- [ ] **Simple processor** (sync, for dev/debug)
- [ ] **Configurable endpoint + headers + API key**

### Provider auto-instrumentation
- [ ] Monkeypatch **OpenAI** (`client.chat.completions.create`, streaming)
- [ ] Monkeypatch **Anthropic** (`messages.create`, streaming)
- [ ] Monkeypatch **LiteLLM** (catches many providers at once)
- [ ] Callback hook for **LangChain** (implement `BaseCallbackHandler`)
- [ ] Handler for **LlamaIndex**

### Evaluations / scoring
- [ ] Score objects linked to spans (name, value, label, explanation)
- [ ] Online eval hook (post-process spans to add scores)

---

## 2. Attribute schema to emit

Emit **both** OpenInference (for Phoenix/Arize) **and** OTel GenAI (for official OTel consumers). They're additive — the same span carries both namespaces.

### Span-level always-emit
```python
{
    # OpenInference — required
    "openinference.span.kind": "LLM",  # from your SpanKind enum
    # Generic I/O — usable by any OTel backend
    "input.value": str(input),
    "input.mime_type": "application/json",
    "output.value": str(output),
    "output.mime_type": "application/json",
    # OTel GenAI — required
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "openai",
}
```

### LLM span
```python
{
    # Model identity
    "llm.model_name": "gpt-4o",
    "llm.system": "openai",
    "llm.provider": "openai",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.response.model": "gpt-4o",  # actual model from response
    "gen_ai.response.id": response.id,
    "gen_ai.response.finish_reasons": ["stop"],
    # Invocation params (both styles)
    "llm.invocation_parameters": json.dumps({"temperature": 0.7, "max_tokens": 1000}),
    "gen_ai.request.temperature": 0.7,
    "gen_ai.request.max_tokens": 1000,
    "gen_ai.request.top_p": 1.0,
    # Token counts (both styles)
    "llm.token_count.prompt": 150,
    "llm.token_count.completion": 50,
    "llm.token_count.total": 200,
    "llm.token_count.prompt_details.cache_read": 100,
    "llm.token_count.prompt_details.cache_write": 50,
    "llm.token_count.completion_details.reasoning": 10,
    "gen_ai.usage.input_tokens": 150,
    "gen_ai.usage.output_tokens": 50,
    "gen_ai.usage.cache_read.input_tokens": 100,
    "gen_ai.usage.cache_creation.input_tokens": 50,
    # Cost
    "llm.cost.prompt": 0.00015,
    "llm.cost.completion": 0.00020,
    "llm.cost.total": 0.00035,
    # Messages — flat indexed (OpenInference style)
    "llm.input_messages.0.message.role": "system",
    "llm.input_messages.0.message.content": "You are helpful.",
    "llm.input_messages.1.message.role": "user",
    "llm.input_messages.1.message.content": "Hello",
    "llm.output_messages.0.message.role": "assistant",
    "llm.output_messages.0.message.content": "Hi there!",
    "llm.finish_reason": "stop",
    # OTel GenAI — messages as JSON opt-in blobs
    "gen_ai.input.messages": json.dumps([...]),  # when CAPTURE_CONTENT=true
    "gen_ai.output.messages": json.dumps([...]),
}
```

### Tool span
```python
{
    "openinference.span.kind": "TOOL",
    "gen_ai.operation.name": "execute_tool",
    "tool.name": "get_weather",
    "tool.description": "Gets weather for a city",
    "tool.parameters": json.dumps({"type": "object", "properties": {...}}),
    "gen_ai.tool.name": "get_weather",
    "gen_ai.tool.description": "Gets weather for a city",
    "input.value": json.dumps({"city": "Zurich"}),
    "output.value": json.dumps({"temp": 18, "condition": "cloudy"}),
    # opt-in
    "gen_ai.tool.call.arguments": json.dumps({"city": "Zurich"}),
    "gen_ai.tool.call.result": json.dumps({"temp": 18}),
}
```

### Retriever span
```python
{
    "openinference.span.kind": "RETRIEVER",
    "gen_ai.operation.name": "retrieval",
    "retrieval.documents.0.document.id": "doc-123",
    "retrieval.documents.0.document.content": "...",
    "retrieval.documents.0.document.score": 0.92,
    "retrieval.documents.0.document.metadata": json.dumps({...}),
    # OTel opt-in
    "gen_ai.retrieval.query.text": "user query",
    "gen_ai.retrieval.documents": json.dumps([...]),
}
```

### Agent span
```python
{
    "openinference.span.kind": "AGENT",
    "gen_ai.operation.name": "invoke_agent",
    "agent.name": "my-agent",
    "gen_ai.agent.name": "my-agent",
    "gen_ai.agent.id": "agent-123",
    "gen_ai.conversation.id": "session-456",
    "session.id": "session-456",
    "user.id": "user-789",
}
```

---

## 3. Span kind enum

```python
from enum import Enum


class SpanKind(str, Enum):
    LLM = "LLM"
    EMBEDDING = "EMBEDDING"
    CHAIN = "CHAIN"
    RETRIEVER = "RETRIEVER"
    RERANKER = "RERANKER"
    TOOL = "TOOL"
    AGENT = "AGENT"
    GUARDRAIL = "GUARDRAIL"
    EVALUATOR = "EVALUATOR"
    PROMPT = "PROMPT"


# Mapping to OTel GenAI operation names
OPERATION_NAME = {
    SpanKind.LLM: "chat",
    SpanKind.EMBEDDING: "embeddings",
    SpanKind.RETRIEVER: "retrieval",
    SpanKind.TOOL: "execute_tool",
    SpanKind.AGENT: "invoke_agent",
    SpanKind.CHAIN: "chain",  # not in OTel spec — use custom
    SpanKind.RERANKER: "reranker",
    SpanKind.GUARDRAIL: "guardrail",
    SpanKind.EVALUATOR: "evaluate",
    SpanKind.PROMPT: "prompt",
}
```

---

## 4. Context propagation architecture

Build on OTel's context API so your spans integrate with any OTel backend automatically:

```python
from opentelemetry import trace, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


# Core structure
class NagentsSpan:
    def __init__(self, otel_span):
        self._span = otel_span  # delegate to OTel

    def set_attribute(self, key, value):
        self._span.set_attribute(key, value)

    def set_input(self, value):
        self.set_attribute("input.value", _serialize(value))

    def set_output(self, value):
        self.set_attribute("output.value", _serialize(value))

    def record_exception(self, exc):
        self._span.record_exception(exc)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if args[0]:
            self._span.set_status(StatusCode.ERROR)
        self._span.end()


class NagentsTracer:
    def __init__(self, otel_tracer):
        self._tracer = otel_tracer

    def start_span(self, name, kind: SpanKind, **attrs):
        otel_span = self._tracer.start_span(name)
        span = NagentsSpan(otel_span)
        span.set_attribute("openinference.span.kind", kind.value)
        span.set_attribute("gen_ai.operation.name", OPERATION_NAME[kind])
        for k, v in attrs.items():
            span.set_attribute(k, v)
        return span

    # Decorator factories
    def agent(self, fn=None, *, name=None):
        return self._decorator(SpanKind.AGENT, fn, name)

    def tool(self, fn=None, *, name=None, description=None):
        return self._decorator(SpanKind.TOOL, fn, name)

    def chain(self, fn=None, *, name=None):
        return self._decorator(SpanKind.CHAIN, fn, name)

    def llm(self, fn=None, *, name=None, process_input=None, process_output=None):
        return self._decorator(SpanKind.LLM, fn, name, process_input=process_input, process_output=process_output)
```

---

## 5. TraceConfig (privacy controls)

```python
from dataclasses import dataclass, field


@dataclass
class TraceConfig:
    hide_inputs: bool = False
    hide_outputs: bool = False
    hide_input_messages: bool = False
    hide_output_messages: bool = False
    hide_input_text: bool = False
    hide_output_text: bool = False
    hide_input_images: bool = False
    hide_llm_invocation_parameters: bool = False
    hide_prompts: bool = False
    hide_choices: bool = False
    hide_embeddings_text: bool = False
    hide_embeddings_vectors: bool = False
    base64_image_max_length: int = 32_000


REDACTED = "__REDACTED__"


def apply_config(attrs: dict, cfg: TraceConfig) -> dict:
    result = dict(attrs)
    if cfg.hide_inputs:
        result["input.value"] = REDACTED
    if cfg.hide_outputs:
        result["output.value"] = REDACTED
    # ... per-field logic
    return result
```

---

## 6. Context attributes (session, user, metadata)

Implement as Python contextvars (thread- and async-safe):

```python
from contextlib import contextmanager
from contextvars import ContextVar

_session_id: ContextVar[str] = ContextVar("session_id", default=None)
_user_id: ContextVar[str] = ContextVar("user_id", default=None)
_tags: ContextVar[list] = ContextVar("tags", default=None)
_metadata: ContextVar[dict] = ContextVar("metadata", default=None)


@contextmanager
def using_attributes(
    session_id=None,
    user_id=None,
    tags=None,
    metadata=None,
    prompt_template=None,
    prompt_template_variables=None,
    prompt_template_version=None,
):
    tokens = []
    if session_id:
        tokens.append(_session_id.set(session_id))
    if user_id:
        tokens.append(_user_id.set(user_id))
    if tags:
        tokens.append(_tags.set(tags))
    if metadata:
        tokens.append(_metadata.set(metadata))
    try:
        yield
    finally:
        for tok in tokens:
            tok.var.reset(tok)


def inject_context_attributes(span):
    """Call at span-start to copy context vars into span attributes."""
    if sid := _session_id.get():
        span.set_attribute("session.id", sid)
        span.set_attribute("gen_ai.conversation.id", sid)
    if uid := _user_id.get():
        span.set_attribute("user.id", uid)
    if tags := _tags.get():
        span.set_attribute("tag.tags", tags)
    if meta := _metadata.get():
        span.set_attribute("metadata", json.dumps(meta))
```

---

## 7. OpenAI auto-instrumentor pattern

```python
import functools
from openai.resources.chat.completions import Completions

_original_create = Completions.create


def _instrumented_create(self, *args, **kwargs):
    model = kwargs.get("model", "unknown")
    messages = kwargs.get("messages", [])
    stream = kwargs.get("stream", False)

    with tracer.start_as_current_span(
        f"chat {model}",
        attributes={
            "openinference.span.kind": "LLM",
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": model,
            "llm.model_name": model,
            "llm.system": "openai",
            "llm.provider": "openai",
        },
    ) as span:
        # Set input messages
        _set_input_messages(span, messages)

        response = _original_create(self, *args, **kwargs)

        if stream:
            return _wrap_stream(span, response)  # accumulate chunks
        else:
            _set_output_messages(span, response)
            _set_token_counts(span, response.usage)
            span.set_attribute("llm.finish_reason", response.choices[0].finish_reason)
            return response


def instrument_openai():
    Completions.create = _instrumented_create


def uninstrument_openai():
    Completions.create = _original_create
```

---

## 8. LangChain callback handler

```python
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class NagentsCallbackHandler(BaseCallbackHandler):
    def __init__(self, tracer):
        self._tracer = tracer
        self._spans = {}  # run_id → span

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        span = self._tracer.start_span(
            serialized.get("name", "LLM"),
            kind=SpanKind.LLM,
        )
        span.set_attribute("llm.prompts", prompts)
        self._spans[str(run_id)] = span

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        span = self._tracer.start_span(serialized.get("name", "ChatModel"), kind=SpanKind.LLM)
        _set_langchain_messages(span, messages)
        self._spans[str(run_id)] = span

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        span = self._spans.pop(str(run_id), None)
        if span:
            _set_llm_result(span, response)
            span._span.end()

    def on_llm_error(self, error, *, run_id, **kwargs):
        span = self._spans.pop(str(run_id), None)
        if span:
            span._span.record_exception(error)
            span._span.set_status(StatusCode.ERROR)
            span._span.end()

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        span = self._tracer.start_span(serialized.get("name", "Chain"), kind=SpanKind.CHAIN)
        span.set_input(inputs)
        self._spans[str(run_id)] = span

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        span = self._spans.pop(str(run_id), None)
        if span:
            span.set_output(outputs)
            span._span.end()

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        span = self._tracer.start_span(serialized.get("name", "Tool"), kind=SpanKind.TOOL)
        span.set_attribute("tool.name", serialized.get("name", ""))
        span.set_input(input_str)
        self._spans[str(run_id)] = span

    def on_tool_end(self, output, *, run_id, **kwargs):
        span = self._spans.pop(str(run_id), None)
        if span:
            span.set_output(output)
            span._span.end()

    def on_retriever_start(self, serialized, query, *, run_id, **kwargs):
        span = self._tracer.start_span("Retriever", kind=SpanKind.RETRIEVER)
        span.set_input(query)
        self._spans[str(run_id)] = span

    def on_retriever_end(self, documents, *, run_id, **kwargs):
        span = self._spans.pop(str(run_id), None)
        if span:
            _set_retrieval_documents(span, documents)
            span._span.end()
```

---

## 9. Exporter setup (register function)

Modeled after `phoenix.otel.register()`:

```python
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GRPCExporter


def register(
    *,
    endpoint: str = None,
    project_name: str = None,
    batch: bool = True,
    headers: dict = None,
    api_key: str = None,
    protocol: str = "http/protobuf",  # "http/protobuf" | "grpc"
    set_global: bool = True,
    auto_instrument: bool = False,
    trace_config: TraceConfig = None,
) -> TracerProvider:
    endpoint = endpoint or os.environ.get("NAGENTS_ENDPOINT", "http://localhost:4318")
    api_key = api_key or os.environ.get("NAGENTS_API_KEY")
    project_name = project_name or os.environ.get("NAGENTS_PROJECT", "default")

    headers = headers or {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resource = Resource({"service.name": project_name, "phoenix_project_name": project_name})

    if protocol == "grpc":
        exporter = GRPCExporter(endpoint=endpoint, headers=headers)
    else:
        exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)

    processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(processor)

    if set_global:
        from opentelemetry import trace as otel_trace

        otel_trace.set_tracer_provider(provider)

    if auto_instrument:
        _auto_instrument_all()

    return provider
```

---

## 10. Streaming LLM support

Streaming requires accumulating chunks and emitting the full span only on stream completion:

```python
def _wrap_stream(span, stream_response):
    chunks = []
    try:
        for chunk in stream_response:
            chunks.append(chunk)
            yield chunk
    finally:
        # reconstruct full response from chunks
        content = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
        usage = _extract_usage_from_chunks(chunks)
        span.set_attribute("llm.output_messages.0.message.role", "assistant")
        span.set_attribute("llm.output_messages.0.message.content", content)
        if usage:
            _set_token_counts(span, usage)
        span._span.end()
```

---

## 11. Priority order for implementation

**Phase 1 — Core (minimum viable observability):**
1. `register()` → OTel provider + OTLP exporter + BatchSpanProcessor
2. `SpanKind` enum + attribute helpers
3. Decorator (`@trace`) + context manager that auto-injects `openinference.span.kind` + `gen_ai.operation.name`
4. `using_attributes(session_id, user_id, tags, metadata)` context manager
5. `TraceConfig` with `hide_inputs` / `hide_outputs`

**Phase 2 — LLM-specific richness:**
6. OpenAI auto-instrumentor (monkeypatch)
7. Anthropic auto-instrumentor
8. Streaming support
9. Token count + cost emission (both namespaces)
10. Message array flattening (indexed OI format + JSON blob OTel format)
11. Tool call attribute helpers

**Phase 3 — Ecosystem integrations:**
12. LangChain callback handler
13. LlamaIndex integration
14. LiteLLM monkeypatch

**Phase 4 — Advanced:**
15. Score/eval attachment API
16. Prompt template registry integration
17. Reranker, embedding span helpers
18. Guard/suppression API (`suppress_tracing()`)
19. OTLP gRPC exporter
20. Metrics emission (`gen_ai.client.token.usage`, etc.)
