# OpenTelemetry GenAI Semantic Conventions

**Source:** https://opentelemetry.io/docs/specs/semconv/gen-ai/ and https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/

The **official** OTel answer to GenAI telemetry. Status: **Development** (experimental) as of spec v1.36+ — gated behind `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`. OpenInference (ch. 01) predates this and uses a different attribute namespace; the two overlap in intent but differ in keys. Many new instrumentors dual-emit.

## 1. Signals

Four signal types are defined:
- **Spans** — one per model/agent/tool call.
- **Events** — originally per-message events; current direction is a single `gen_ai.client.inference.operation.details` event carrying all inputs/outputs as structured JSON attributes.
- **Metrics** — token usage, operation duration, TTFT, time-per-output-token.
- **Evaluation event** — `gen_ai.evaluation.result`.

## 2. Span model

### 2.1 Naming
| Span type | Name template |
|---|---|
| Inference (chat, text_completion, embeddings, generate_content) | `{gen_ai.operation.name} {gen_ai.request.model}` |
| Retrieval | `{gen_ai.operation.name} {gen_ai.data_source.id}` |
| Execute tool | `execute_tool {gen_ai.tool.name}` |
| Invoke agent | `invoke_agent {gen_ai.agent.name}` |
| Create agent | `create_agent {gen_ai.agent.name}` |

### 2.2 `gen_ai.operation.name` values
`chat`, `text_completion`, `embeddings`, `generate_content`, `create_agent`, `invoke_agent`, `execute_tool`, `retrieval`.

### 2.3 Span kind
- `CLIENT` when the call is remote (OpenAI, Bedrock, Assistants API, Vertex).
- `INTERNAL` when the agent loop runs in-process (LangChain, CrewAI).

## 3. Attribute registry (namespace `gen_ai.*`)

All attributes are **Development** stability unless noted.

### 3.1 Operation / provider / conversation
| Key | Type | Description |
|---|---|---|
| `gen_ai.operation.name` | string | Operation (see §2.2). **Required on every gen_ai span.** |
| `gen_ai.provider.name` | string | `openai`, `anthropic`, `aws.bedrock`, `azure.ai.openai`, `gcp.vertex_ai`, `gcp.gemini`, `cohere`, `mistral_ai`, `groq`, ... |
| `gen_ai.conversation.id` | string | Session / thread identifier |
| `gen_ai.data_source.id` | string | Knowledge base / index identifier |
| `gen_ai.prompt.name` | string | Named prompt in a prompt registry |

### 3.2 Request parameters
| Key | Type |
|---|---|
| `gen_ai.request.model` | string |
| `gen_ai.request.temperature` | double |
| `gen_ai.request.top_p` | double |
| `gen_ai.request.top_k` | double |
| `gen_ai.request.max_tokens` | int |
| `gen_ai.request.frequency_penalty` | double |
| `gen_ai.request.presence_penalty` | double |
| `gen_ai.request.stop_sequences` | string[] |
| `gen_ai.request.seed` | int |
| `gen_ai.request.choice.count` | int |
| `gen_ai.request.encoding_formats` | string[] |

### 3.3 Response
| Key | Type |
|---|---|
| `gen_ai.response.id` | string |
| `gen_ai.response.model` | string (actual model string returned) |
| `gen_ai.response.finish_reasons` | string[] |

### 3.4 Usage (tokens)
| Key | Type |
|---|---|
| `gen_ai.usage.input_tokens` | int |
| `gen_ai.usage.output_tokens` | int |
| `gen_ai.usage.cache_read.input_tokens` | int |
| `gen_ai.usage.cache_creation.input_tokens` | int |
| `gen_ai.token.type` | string (`input` / `output`) — used on the token metric |

### 3.5 Output
| Key | Type | Notes |
|---|---|---|
| `gen_ai.output.type` | string | `text`, `json`, `image`, `speech`, ... |
| `gen_ai.output.messages` | any (JSON) | Opt-in; model response messages |

### 3.6 Inputs (opt-in, sensitive)
| Key | Type |
|---|---|
| `gen_ai.input.messages` | any (JSON) — full chat history |
| `gen_ai.system_instructions` | any (JSON) — system prompt(s) |

These are **opt-in** because they carry user data. Instrumentation SDKs expose an env var (see §6) to enable them.

### 3.7 Tools
| Key | Type |
|---|---|
| `gen_ai.tool.name` | string |
| `gen_ai.tool.type` | string |
| `gen_ai.tool.description` | string |
| `gen_ai.tool.call.id` | string |
| `gen_ai.tool.call.arguments` | any (opt-in) |
| `gen_ai.tool.call.result` | any (opt-in) |
| `gen_ai.tool.definitions` | any (opt-in) |

### 3.8 Agents
| Key | Type |
|---|---|
| `gen_ai.agent.id` | string |
| `gen_ai.agent.name` | string |
| `gen_ai.agent.description` | string |
| `gen_ai.agent.version` | string |

### 3.9 Retrieval
| Key | Type |
|---|---|
| `gen_ai.retrieval.query.text` | string (opt-in) |
| `gen_ai.retrieval.documents` | any (opt-in) |

### 3.10 Embeddings
| Key | Type |
|---|---|
| `gen_ai.embeddings.dimension.count` | int |

### 3.11 Evaluations
| Key | Type |
|---|---|
| `gen_ai.evaluation.name` | string |
| `gen_ai.evaluation.score.value` | double |
| `gen_ai.evaluation.score.label` | string |
| `gen_ai.evaluation.explanation` | string |

## 4. Events

Current recommendation collapses per-message events into a single structured event + attributes:

### 4.1 `gen_ai.client.inference.operation.details`
Attached to the inference span. Carries input/output messages, system instructions, tool definitions as opt-in attributes (the same keys as §3).

### 4.2 `gen_ai.evaluation.result`
Emitted by evaluators. Payload = §3.11 attributes.

**Legacy (still seen in older instrumentation):** discrete per-role events `gen_ai.system.message`, `gen_ai.user.message`, `gen_ai.assistant.message`, `gen_ai.tool.message`, `gen_ai.choice`. These are deprecated in favor of the single event + `gen_ai.input.messages` JSON. New code should emit the structured-attribute form.

## 5. Metrics

| Metric | Instrument | Unit | Required attributes |
|---|---|---|---|
| `gen_ai.client.token.usage` | Histogram | `{token}` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.token.type` |
| `gen_ai.client.operation.duration` | Histogram | `s` | `gen_ai.operation.name`, `gen_ai.provider.name` |
| `gen_ai.server.request.duration` | Histogram | `s` | `gen_ai.operation.name`, `gen_ai.provider.name` |
| `gen_ai.server.time_to_first_token` | Histogram | `s` | `gen_ai.operation.name`, `gen_ai.provider.name` |
| `gen_ai.server.time_per_output_token` | Histogram | `s` | `gen_ai.operation.name`, `gen_ai.provider.name` |

Conditionally required on all: `gen_ai.request.model`, `server.address`, `server.port`, `error.type`.

## 6. Content capture controls

Input/output content is **opt-in** (compliance). The de-facto env var shared across instrumentors:

```
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
```

## 7. Required vs optional summary

For a valid chat-span you need at minimum:
- `gen_ai.operation.name` = `chat`
- `gen_ai.provider.name`
- span name `chat {model}`

And *conditionally required* (set when available):
- `gen_ai.request.model`
- `error.type` on failure
- `server.address` / `server.port`
- `gen_ai.conversation.id`
- `gen_ai.output.type`

## 8. Relationship to OpenInference

| Concept | OpenInference key | OTel GenAI key |
|---|---|---|
| Span kind | `openinference.span.kind` (LLM/TOOL/...) | `gen_ai.operation.name` (chat/execute_tool/...) |
| Model | `llm.model_name` | `gen_ai.request.model` / `gen_ai.response.model` |
| Provider | `llm.provider` + `llm.system` | `gen_ai.provider.name` |
| Messages in | `llm.input_messages.{i}.*` (indexed flat) | `gen_ai.input.messages` (JSON blob) |
| Messages out | `llm.output_messages.{i}.*` | `gen_ai.output.messages` |
| Token counts | `llm.token_count.prompt/completion/total` | `gen_ai.usage.input_tokens/output_tokens` |
| Cache tokens | `llm.token_count.prompt_details.cache_read/write` | `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens` |
| Reasoning tokens | `llm.token_count.completion_details.reasoning` | *(not yet standardized)* |
| Tool name | `tool.name` | `gen_ai.tool.name` |
| Tool args | `tool_call.function.arguments` | `gen_ai.tool.call.arguments` |
| Conversation | `session.id` | `gen_ai.conversation.id` |
| Prompt template | `llm.prompt_template.*` | `gen_ai.prompt.name` only |

**Takeaway for implementers:** emit both namespaces in parallel where feasible. The indexed flat keys of OpenInference aggregate/filter well in OTLP backends; the OTel GenAI blobs are canonical for the spec but require a consumer that understands the JSON schema.
