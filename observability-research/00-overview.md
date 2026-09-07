# AI Agent Observability — Landscape Overview

A reader's map for the rest of this folder. Every file in `observability-research/` is a reference dive; this one explains **how the pieces relate**.

> **Draft research, not shipped API documentation.** These notes collect design
> options and version-dependent vendor references. Examples are illustrative and
> have not been validated against every SDK version; check the linked upstream
> sources before implementation. Observability integration and the proposed APIs
> are not currently available on `nagents.Agent`.

## 1. The stack has three independent layers

```
┌────────────────────────────────────────────────────────────┐
│   L3  Platform / UI / storage                              │
│       Langfuse, Phoenix, LangSmith, Arize AX, Helicone,    │
│       Braintrust, W&B Weave, Traceloop, OpenLIT            │
├────────────────────────────────────────────────────────────┤
│   L2  Semantic conventions (attribute names + span kinds)  │
│       OpenInference  |  OTel GenAI semconv  |  ad-hoc      │
├────────────────────────────────────────────────────────────┤
│   L1  Wire protocol / transport                            │
│       OTLP (OpenTelemetry Protocol) | vendor HTTP ingest   │
└────────────────────────────────────────────────────────────┘
```

Most confusion in this space comes from conflating the layers. A tool like Phoenix is L3 **and** defines an L2 (OpenInference). A tool like Traceloop is L3 **and** publishes its own L2 (`traceloop.*`, `llm.*`). The official OTel GenAI semconv is only L2 — it doesn't ship a backend.

## 2. The two semantic-convention families

You'll see these attribute namespaces in the wild:

| Family | Namespace | Origin | Canonical consumer |
|---|---|---|---|
| OpenInference | `llm.*`, `tool.*`, `retrieval.*`, `openinference.span.kind` | Arize (Phoenix) | Phoenix, Arize AX, Langfuse (via mapping) |
| OTel GenAI | `gen_ai.*` | OpenTelemetry SIG (official) | Still stabilizing; adopted by official OTel instrumentors, OpenLIT, newer Traceloop |
| Traceloop/OpenLIT legacy | `llm.*` (different keys), `traceloop.*`, `vector_db.*` | Traceloop | Traceloop SaaS, OpenLIT; converging to `gen_ai.*` via `semantic-conventions-ai` |
| Langfuse native | `input`, `output`, `usage`, `model`, `metadata` (non-OTel) | Langfuse | Langfuse; also accepts OpenInference/OTel via OTLP ingest |

Details of each are in `01-openinference-spec.md` and `02-otel-genai-semconv.md`.

## 3. The span-kind taxonomy (converging)

Every platform has basically the same ten-ish types, under different names:

| Concept | OpenInference | OTel GenAI `operation.name` | LangSmith `run_type` | Langfuse observation type | Braintrust `span_attributes.type` |
|---|---|---|---|---|---|
| LLM chat call | `LLM` | `chat` | `llm` | `GENERATION` | `llm` |
| Text completion | `LLM` | `text_completion` | `llm` | `GENERATION` | `llm` |
| Embedding | `EMBEDDING` | `embeddings` | `embedding` | `GENERATION` | `llm` |
| Generic step | `CHAIN` | — | `chain` | `SPAN` | `task` |
| Tool call | `TOOL` | `execute_tool` | `tool` | `SPAN` | `tool` / `function` |
| Retrieval | `RETRIEVER` | `retrieval` | `retriever` | `SPAN` | `task` |
| Reranker | `RERANKER` | — | `chain` | `SPAN` | `task` |
| Agent loop | `AGENT` | `invoke_agent` | `chain`/tagged | `SPAN` | `task` |
| Guardrail | `GUARDRAIL` | — (use `execute_tool`) | `chain` | `SPAN` | `task` |
| Evaluation | `EVALUATOR` | (evaluation event) | `chain` | `SCORE` | `score` |
| Prompt render | `PROMPT` | — (`gen_ai.prompt.name`) | `prompt` | `SPAN` | `task` |

## 4. How instrumentation gets into your code

| Approach | Used by | Pros | Cons |
|---|---|---|---|
| **Provider auto-instrumentors** (monkeypatch OpenAI/Anthropic/... SDKs) | OpenInference, OpenLLMetry, OpenLIT, Langfuse's `langfuse.openai` wrapper | Zero-line change | Breaks on SDK updates; misses custom code |
| **Framework callbacks** | LangChain (CallbackHandler), LlamaIndex (handlers) | Captures app-level semantics | Framework-specific |
| **Decorator** (`@observe`, `@traceable`, `@weave.op`, `@workflow`) | Langfuse, LangSmith, Weave, Traceloop | Clear boundaries in user code | Requires edits |
| **Proxy** (swap API base URL) | Helicone | No code change at all | Adds network hop, doesn't see local logic |
| **Manual spans** (OTel API) | Everyone, as escape hatch | Full control | Verbose |

Most mature stacks offer **all five** and let the user mix.

## 5. What a platform actually stores per span

Minimum common schema (union of what every platform has):

```
id                     string        unique observation id
trace_id               string        the whole request
parent_id              string?       tree structure
name                   string        human label
kind/type              enum          from the taxonomy above
start_time, end_time   timestamp
status                 ok|error
input                  json          messages / args
output                 json          response / result
model                  string        llm.model_name / gen_ai.request.model
model_parameters       json          temperature, top_p, ...
usage                  {input,output,total,cache_read,cache_write,cost}
metadata               json          free-form
tags                   [string]
user_id                string        who asked
session_id             string        which conversation
version / release      string        app version
level                  enum          DEBUG|DEFAULT|WARNING|ERROR
status_message         string
```

Everything else is a platform-specific extension of this core.

## 6. Ingestion paths

```
┌─────────────────────┐
│  Your app code      │
└──────────┬──────────┘
           │
     ┌─────┴──────┐
     │            │
     ▼            ▼
OTel SDK      Platform SDK        Proxy
(OTLP)        (HTTP batch)        (replaces LLM URL)
     │            │                   │
     ▼            ▼                   ▼
  OTel          platform           platform
  collector     ingest API         ingest API
     │            │                   │
     └────┬───────┘                   │
          ▼                           ▼
     backend (Postgres/ClickHouse/S3)
```

**Langfuse, Phoenix, Braintrust, OpenLIT, Traceloop** all expose an **OTLP endpoint**, so any OTel-instrumented app can point at them without their SDK.

**LangSmith** runs a custom HTTP ingest (POST /runs) — OTel interop is limited.

**Helicone** is unique: it's in the data path as a proxy, not a sidecar.

## 7. Sessions, users, tags, metadata — the cross-cutting fields

These four appear everywhere and are how UIs let you filter. Names differ but all map to the same concept:

| Concept | OpenInference | OTel GenAI | Langfuse | LangSmith | Braintrust | Helicone |
|---|---|---|---|---|---|---|
| Session | `session.id` | `gen_ai.conversation.id` | `session_id` | `session_id` | `metadata.session_id` | `Helicone-Session-Id` header |
| User | `user.id` | *(not defined)* | `user_id` | `metadata.user_id` | `metadata.user_id` | `Helicone-User-Id` header |
| Tags | `tag.tags` | *(not defined)* | `tags` | `tags` | `tags` | `Helicone-Property-*` |
| Metadata | `metadata` | *(not defined)* | `metadata` | `extra.metadata` | `metadata` | `Helicone-Property-*` |

If you're designing a library, **expose all four as first-class arguments** — don't force users to know the attribute key.

## 8. Evaluation / scoring is its own mini-standard

Every platform has a `score` concept attached to traces/spans:

| Platform | API shape |
|---|---|
| OpenInference | `EVALUATOR` span kind; score as regular attribute |
| OTel GenAI | `gen_ai.evaluation.result` event: `evaluation.name`, `evaluation.score.value`, `evaluation.score.label`, `evaluation.explanation` |
| Langfuse | `score` object: `name`, `value` (number/category), `data_type`, `comment`, `trace_id`, `observation_id` |
| LangSmith | `feedback` object: `key`, `score`, `value`, `comment`, `correction`, `run_id` |
| Braintrust | `scores` dict on every span + Eval framework with `Autoevals` |
| Phoenix | `phoenix.trace.trace_dataset` + `arize-phoenix-evals` attaches `eval.*` attributes |

## 9. What to look at next

| File | Topic |
|---|---|
| `01-openinference-spec.md` | Full attribute reference for the Arize-originated spec |
| `02-otel-genai-semconv.md` | Full attribute reference for the official OTel GenAI spec |
| `03-langfuse.md` | Langfuse data model, SDK, integrations, OTLP ingest |
| `04-langchain-and-langsmith.md` | LangChain callbacks + LangSmith run tree |
| `05-arize-phoenix.md` | Phoenix OSS + Arize AX |
| `06-traceloop-and-openlit.md` | OTel-native stacks |
| `07-helicone-braintrust-weave.md` | Proxy + eval-first + W&B stacks |
| `08-span-taxonomy-comparison.md` | Full cross-reference tables |
| `09-implementation-guide.md` | How to implement these features in your own library |
