# OpenInference Specification

**Source:** https://github.com/Arize-ai/openinference (spec lives under `/spec`).

OpenInference is a vendor-neutral set of **semantic conventions** on top of OpenTelemetry for GenAI / LLM / agent workloads. It's what Arize Phoenix consumes natively, and what many other vendors accept as an input format. It is **not** a protocol; it is a schema — the wire format is OTLP (OpenTelemetry Protocol), and the spec just defines span names, a `openinference.span.kind` attribute, and a namespaced attribute dictionary.

## 1. Design goals

1. **Structured LLM data.** A single `input.value` string cannot represent multi-turn messages, tool definitions, tool calls, or multimodal content. The spec defines indexed list attributes for these.
2. **First-class token/cost metrics.** Prompt/completion/cache/reasoning token breakdowns and USD cost details.
3. **Agentic workflows.** A consistent span-kind taxonomy across agents, chains, tools, retrievers, rerankers, guardrails and evaluators — so nested loops are traceable.
4. **Privacy controls.** Per-field masking (`hide_inputs`, `hide_output_messages`, `hide_input_images`, ...), with a `"__REDACTED__"` sentinel so masked fields are distinguishable from missing ones.
5. **Reproducibility.** Enough context (invocation parameters, prompt template, version) to explain a stochastic run.

## 2. Trace / span model

Pure OpenTelemetry. Every span has:

| Field | Required | Notes |
|---|---|---|
| Name | ✓ | Free-form; instrumentors usually pick something like `ChatOpenAI`, `retrieve`, `my_tool` |
| SpanContext (trace_id, span_id) | ✓ | |
| Start/End timestamps | ✓ | |
| Status code | ✓ | `OK` / `ERROR` |
| `openinference.span.kind` | ✓ (by convention) | One of the 10 kinds below |
| Parent span ID | Optional (null for root) | |
| Attributes | Optional | Key/value, dot-separated namespaces |
| Events | Optional | Used for exceptions |
| Status message | Optional | |

Attribute values must be `string | bool | int | float` or arrays of these — same constraint as OTel. List-valued structured data (messages, tool calls, documents) is represented with **indexed flat keys** rather than nested objects, so every attribute remains a leaf. The convention is `<prefix>.<index>.<field>`, e.g. `llm.input_messages.0.message.role`.

## 3. Span kinds

The value of the required `openinference.span.kind` attribute. Ten kinds:

| Kind | Purpose |
|---|---|
| `LLM` | One LLM completion/chat API call |
| `EMBEDDING` | Call to an embedding model |
| `CHAIN` | Deterministic orchestration step / linkage |
| `RETRIEVER` | Vector store, search engine, KB lookup |
| `RERANKER` | Reorder retrieved documents |
| `TOOL` | External tool/function call (from an agent) |
| `AGENT` | Encompasses LLM + tool loops; usually a parent span |
| `GUARDRAIL` | Jailbreak / PII / moderation check |
| `EVALUATOR` | Automated eval call |
| `PROMPT` | Prompt template rendering |

## 4. Attribute reference

Keys are **verbatim** from the spec. All keys are typed; JSON-typed attributes are serialized strings.

### 4.1 Generic I/O
```
input.value              String
input.mime_type          String    (e.g. "application/json", "text/plain")
output.value             String
output.mime_type         String
```

### 4.2 Session / user / metadata
```
session.id               String
user.id                  String
tag.tags                 [String]
metadata                 JSON      (free-form span metadata)
openinference.span.kind  String    ← required
```

### 4.3 LLM core
```
llm.model_name           String    e.g. "gpt-4o", "claude-3-5-sonnet"
llm.system               String    AI product vendor (well-known: anthropic, openai, vertexai,
                                   cohere, mistralai, xai, deepseek, amazon, meta, ai21)
llm.provider             String    Hosting provider (anthropic, openai, cohere, mistralai,
                                   azure, google, aws, xai, deepseek, groq, fireworks,
                                   moonshot, cerebras, perplexity, together, ...)
llm.invocation_parameters JSON     Model call config (temperature, top_p, max_tokens, ...)
llm.prompts              [String]  Completions-API inputs
llm.choices              [String]  Completions-API outputs
llm.function_call        JSON      Function invocation details
llm.input_messages       List      Chat-API request messages (see §4.4)
llm.output_messages      List      Chat-API response messages
llm.finish_reason        String    e.g. "stop", "length", "tool_calls"
llm.tools                List      Tools advertised to the LLM (see §4.6)
```

### 4.4 Message (indexed list)
A single message at index `i` is flattened as:
```
llm.input_messages.{i}.message.role                          String   "user" | "system" | "assistant" | "tool" | "function"
llm.input_messages.{i}.message.content                       String   text content
llm.input_messages.{i}.message.contents                      List     multimodal parts (see §4.5)
llm.input_messages.{i}.message.name                          String   name of producing tool/function
llm.input_messages.{i}.message.function_call_name            String
llm.input_messages.{i}.message.function_call_arguments_json  JSON
llm.input_messages.{i}.message.tool_calls                    List     (see §4.6)
llm.input_messages.{i}.message.tool_call_id                  String   id of tool result
```
Same shape for `llm.output_messages.{i}.*`.

### 4.5 Message contents (multimodal)
```
…message.contents.{j}.message_content.type    String  "text" | "image" | "audio"
…message.contents.{j}.message_content.text    String
…message.contents.{j}.message_content.image   JSON    image.url or base64
…message.contents.{j}.message_content.audio   JSON    audio object
```

Plus top-level image/audio helpers:
```
image.url           String  URL or base64 data URI
audio.url           String
audio.mime_type     String  audio/mpeg, audio/wav, ...
audio.transcript    String
```

### 4.6 Tool calls & tool definitions
Tool call emitted by the model (inside `message.tool_calls.{k}`):
```
tool_call.id                   String
tool_call.function.name        String
tool_call.function.arguments   JSON
```
Tool definition advertised to the LLM (inside `llm.tools.{k}`):
```
tool.name           String
tool.description    String
tool.parameters     JSON
tool.json_schema    JSON
tool.id             String   (on tool result)
```

### 4.7 Token counts
```
llm.token_count.prompt                         Int
llm.token_count.completion                     Int
llm.token_count.total                          Int
llm.token_count.completion_details.reasoning   Int   chain-of-thought / reasoning tokens
llm.token_count.completion_details.audio       Int
llm.token_count.prompt_details.cache_read      Int   cache-hit input tokens
llm.token_count.prompt_details.cache_write     Int   Anthropic prompt-cache write
llm.token_count.prompt_details.audio           Int
```

### 4.8 Cost (USD)
```
llm.cost.prompt                             Float
llm.cost.completion                         Float
llm.cost.total                              Float
llm.cost.prompt_details.input               Float
llm.cost.prompt_details.cache_write         Float
llm.cost.prompt_details.cache_read          Float
llm.cost.prompt_details.cache_input         Float
llm.cost.prompt_details.audio               Float
llm.cost.completion_details.output          Float
llm.cost.completion_details.reasoning       Float
llm.cost.completion_details.audio           Float
```

### 4.9 Prompt template (for `PROMPT` spans, also usable on LLM spans)
```
llm.prompt_template.template    String   raw template with {placeholders}
llm.prompt_template.variables   JSON     substitution dict
llm.prompt_template.version     String
prompt.vendor                   String   e.g. "langfuse", "promptlayer"
prompt.id                       String
prompt.url                      String
```

### 4.10 Retriever / documents
```
retrieval.documents.{i}.document.id         String|Int
retrieval.documents.{i}.document.content    String
retrieval.documents.{i}.document.score      Float
retrieval.documents.{i}.document.metadata   JSON
```

### 4.11 Reranker
```
reranker.model_name              String
reranker.query                   String
reranker.input_documents         List (same document.* shape)
reranker.output_documents        List
reranker.top_k                   Int
```

### 4.12 Embedding
```
embedding.model_name                  String
embedding.invocation_parameters       JSON
embedding.embeddings.{i}.embedding.text    String
embedding.embeddings.{i}.embedding.vector  [Float]
```

### 4.13 Tool span attributes (for `TOOL` spans)
Same keys as tool definitions — `tool.name`, `tool.description`, `tool.parameters`, `tool.json_schema`. Plus generic `input.value` (arguments) and `output.value` (result).

### 4.14 Agent / graph
```
agent.name           String
graph.node.id        String
graph.node.name      String
graph.node.parent_id String
```

### 4.15 Exceptions (as OTel events on the span)
```
exception.type        String
exception.message     String
exception.stacktrace  String
exception.escaped     Bool
```

## 5. TraceConfig (redaction / cost controls)

All flags default to `False`. Controlled via constructor or env var of the same uppercased name. Redaction produces the sentinel string `"__REDACTED__"`.

| Option | Effect |
|---|---|
| `hide_llm_invocation_parameters` | Drops `llm.invocation_parameters` |
| `hide_inputs` | Masks `input.value` and all `llm.input_messages.*` |
| `hide_outputs` | Masks `output.value` and all `llm.output_messages.*` |
| `hide_input_messages` / `hide_output_messages` | Mask just the message arrays |
| `hide_input_text` / `hide_output_text` | Mask text content inside messages |
| `hide_input_images` | Mask image parts inside input messages |
| `hide_prompts` / `hide_choices` | Completions API input/output |
| `hide_embeddings_text` / `hide_embeddings_vectors` | Embedding content and vectors |
| `base64_image_max_length` | Int, default **32 000**; truncate long inline base64 |

## 6. Instrumentor ecosystem

OpenInference ships Python (and TS) auto-instrumentors that emit the schema above. One package per framework/provider — the Python set as of spec main:

```
openai                openai-agents            anthropic           google-genai
google-adk            bedrock                  vertexai            mistralai
groq                  litellm                  portkey             instructor
langchain             llama-index              haystack            dspy
crewai                autogen                  autogen-agentchat   agno
beeai                 smolagents               strands-agents      pydantic-ai
agent-framework       agentspec                claude-agent-sdk    guardrails
pipecat               promptflow               mcp                 openlit
openllmetry
```

All of them are installed as normal Python packages and activated via `<Name>Instrumentor().instrument()` — a standard OTel instrumentor pattern.

## 7. What to copy into your own library

If you're implementing a compatible emitter:

1. **Hard-require `openinference.span.kind`** on every span — that's what downstream UIs (Phoenix, Langfuse via mapping, Arize SaaS) branch on.
2. Use **indexed flat keys** for lists. Don't nest JSON inside a single attribute unless it's a true leaf (e.g. `tool.parameters`). This lets consumers filter/aggregate on individual fields in OTLP backends.
3. Emit **both** `input.value` / `output.value` **and** the typed structured fields — the generic ones make spans renderable in any OTel UI, the typed ones light up GenAI UIs.
4. Implement `TraceConfig` up front. Masking added later tends to leak through events.
5. Record `llm.system` **and** `llm.provider` separately — UIs use `system` for the logo and `provider` for cost tables.
6. Emit token-count details (cache_read, cache_write, reasoning) when the provider returns them; Phoenix/Arize aggregate them for cost charts.
