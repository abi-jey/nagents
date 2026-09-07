# Span Taxonomy — Cross-Platform Comparison

Full cross-reference of span kinds, attribute keys, and data model fields across all tools covered in this folder.

## 1. Span kind / operation type names

| Concept | OpenInference (`openinference.span.kind`) | OTel GenAI (`gen_ai.operation.name`) | LangSmith (`run_type`) | Langfuse (observation type) | Braintrust (`type`) | W&B Weave | Helicone |
|---|---|---|---|---|---|---|---|
| LLM chat call | `LLM` | `chat` | `llm` | `GENERATION` | `llm` | Auto-patched call | Proxied request |
| Text completion | `LLM` | `text_completion` | `llm` | `GENERATION` | `llm` | Auto-patched call | Proxied request |
| Embedding | `EMBEDDING` | `embeddings` | `embedding` | `GENERATION` | `llm` | Auto-patched call | N/A |
| Generic step / chain | `CHAIN` | `chat` (or custom) | `chain` | `SPAN` | `task` / `function` | `@weave.op` call | N/A |
| Tool call | `TOOL` | `execute_tool` | `tool` | `SPAN` | `tool` | `@weave.op` call | N/A |
| Retrieval | `RETRIEVER` | `retrieval` | `retriever` | `SPAN` | `function` | `@weave.op` call | N/A |
| Reranker | `RERANKER` | *(none standard)* | `chain` | `SPAN` | `function` | `@weave.op` call | N/A |
| Agent loop | `AGENT` | `invoke_agent` | `chain` (tagged) | `SPAN` | `task` | `@weave.op` call | Session path |
| Create agent | *(none)* | `create_agent` | *(none)* | *(none)* | *(none)* | *(none)* | N/A |
| Guardrail | `GUARDRAIL` | `execute_tool` (approx) | `chain` | `SPAN` | `function` | `@weave.op` call | N/A |
| Evaluator / scorer | `EVALUATOR` | `gen_ai.evaluation.result` event | `chain` | `SCORE` | `score` | Scorer op call | Online scorer |
| Prompt render | `PROMPT` | *(none, use `gen_ai.prompt.name`)* | `prompt` | `SPAN` | *(none)* | *(none)* | N/A |

## 2. Model / provider attributes

| Attribute concept | OpenInference | OTel GenAI | LangSmith `extra.invocation_params` | Langfuse |
|---|---|---|---|---|
| Model name (request) | `llm.model_name` | `gen_ai.request.model` | `model` | `model` |
| Model name (response) | *(same)* | `gen_ai.response.model` | *(in `response.model`)* | *(response field)* |
| Provider | `llm.provider` | `gen_ai.provider.name` | *(inferred)* | *(inferred)* |
| AI system/product | `llm.system` | *(merged into provider)* | *(none)* | *(none)* |
| Temperature | `llm.invocation_parameters` (JSON) | `gen_ai.request.temperature` | `temperature` | `modelParameters.temperature` |
| Max tokens | `llm.invocation_parameters` | `gen_ai.request.max_tokens` | `max_tokens` | `modelParameters.maxTokens` |
| Top-p | `llm.invocation_parameters` | `gen_ai.request.top_p` | `top_p` | `modelParameters.topP` |
| Finish reason | `llm.finish_reason` | `gen_ai.response.finish_reasons` | `finish_reason` | *(in output)* |
| Response ID | *(none)* | `gen_ai.response.id` | *(in response)* | *(none)* |

## 3. Token / cost attributes

| Metric | OpenInference | OTel GenAI | LangSmith | Langfuse |
|---|---|---|---|---|
| Input tokens | `llm.token_count.prompt` | `gen_ai.usage.input_tokens` | `prompt_tokens` | `usage.input` |
| Output tokens | `llm.token_count.completion` | `gen_ai.usage.output_tokens` | `completion_tokens` | `usage.output` |
| Total tokens | `llm.token_count.total` | *(sum)* | `total_tokens` | `usage.total` |
| Cache-read tokens | `llm.token_count.prompt_details.cache_read` | `gen_ai.usage.cache_read.input_tokens` | *(none)* | `usage.inputCost` (cost only) |
| Cache-write tokens | `llm.token_count.prompt_details.cache_write` | `gen_ai.usage.cache_creation.input_tokens` | *(none)* | *(none)* |
| Reasoning tokens | `llm.token_count.completion_details.reasoning` | *(none)* | *(none)* | *(none)* |
| Input cost (USD) | `llm.cost.prompt` | *(not in spec)* | *(computed by LangSmith)* | `usage.inputCost` |
| Output cost (USD) | `llm.cost.completion` | *(not in spec)* | *(computed)* | `usage.outputCost` |
| Total cost (USD) | `llm.cost.total` | *(not in spec)* | *(computed)* | `usage.totalCost` |
| Usage unit | *(tokens assumed)* | *(tokens assumed)* | *(tokens assumed)* | `usage.unit` (TOKENS/CHARACTERS/...) |

## 4. Message attributes

| Concept | OpenInference (indexed) | OTel GenAI (JSON blob) | Langfuse | LangSmith |
|---|---|---|---|---|
| Input messages | `llm.input_messages.{i}.message.role/content/...` | `gen_ai.input.messages` (opt-in JSON) | `input` (object) | `inputs.messages` |
| Output messages | `llm.output_messages.{i}.message.role/content/...` | `gen_ai.output.messages` (opt-in JSON) | `output` (object) | `outputs.generations` |
| Message role | `message.role` | *(in JSON blob)* | *(in object)* | *(in object)* |
| Tool calls | `message.tool_calls.{k}.tool_call.function.*` | `gen_ai.tool.call.arguments` (opt-in) | *(in output object)* | *(in outputs)* |
| Function call | `message.function_call_name/arguments_json` | *(deprecated, use tool calls)* | *(in object)* | *(in object)* |

## 5. Tool attributes

| Attribute | OpenInference | OTel GenAI | LangSmith | Langfuse |
|---|---|---|---|---|
| Tool name | `tool.name` | `gen_ai.tool.name` | `name` | `name` |
| Tool description | `tool.description` | `gen_ai.tool.description` | *(in serialized)* | *(none)* |
| Tool parameters | `tool.parameters` / `tool.json_schema` | `gen_ai.tool.definitions` (opt-in) | *(in serialized)* | *(none)* |
| Tool call ID | `tool_call.id` | `gen_ai.tool.call.id` | *(in messages)* | *(none)* |
| Call arguments | `tool_call.function.arguments` | `gen_ai.tool.call.arguments` (opt-in) | `inputs` | `input` |
| Call result | `output.value` | `gen_ai.tool.call.result` (opt-in) | `outputs` | `output` |

## 6. Retrieval attributes

| Attribute | OpenInference | OTel GenAI | LangSmith | Langfuse |
|---|---|---|---|---|
| Query | `retrieval.documents` (inferred from output) | `gen_ai.retrieval.query.text` (opt-in) | `inputs.query` | `input` |
| Documents | `retrieval.documents.{i}.document.*` | `gen_ai.retrieval.documents` (opt-in) | `outputs.documents` | `output` |
| Doc ID | `document.id` | *(in JSON blob)* | *(in object)* | *(none)* |
| Doc content | `document.content` | *(in JSON blob)* | `page_content` | *(in output)* |
| Doc score | `document.score` | *(in JSON blob)* | *(in object)* | *(none)* |

## 7. Context / session / user attributes

| Concept | OpenInference | OTel GenAI | LangSmith | Langfuse | Braintrust | Helicone |
|---|---|---|---|---|---|---|
| Session | `session.id` | `gen_ai.conversation.id` | `session_id` (extra) | `sessionId` | `metadata.session_id` | `Helicone-Session-Id` |
| User | `user.id` | *(none)* | `metadata.user_id` | `userId` | `metadata.user_id` | `Helicone-User-Id` |
| Tags | `tag.tags` | *(none)* | `tags` | `tags` | `tags` | `Helicone-Property-*` |
| Metadata | `metadata` | *(none in span)* | `extra.metadata` | `metadata` | `metadata` | `Helicone-Property-*` |
| Version | *(none)* | *(none)* | *(none)* | `version` | *(none)* | *(none)* |
| Release | *(none)* | *(none)* | *(none)* | `release` | *(none)* | *(none)* |

## 8. Trace-level fields

| Field | OpenInference | LangSmith | Langfuse | Braintrust | Weave |
|---|---|---|---|---|---|
| Trace ID | OTel `trace_id` (128-bit hex) | `id` (UUID) | `id` (UUID) | `root_span_id` / `trace_id` | `trace_id` |
| Span ID | OTel `span_id` (64-bit hex) | `id` (UUID) | `id` (UUID) | span `id` | call `id` |
| Parent ID | OTel `parent_span_id` | `parent_run_id` | `parentObservationId` | `parent_id` | `parent_id` |
| Name | OTel span `name` | `name` | `name` | `name` | `op_name` |
| Start time | OTel `start_time` | `start_time` | `startTime` | `start_time` | `started_at` |
| End time | OTel `end_time` | `end_time` | `endTime` | `end_time` | `ended_at` |
| Status | OTel `status` (OK/ERROR) | `status` (success/error) | *(none — use `level`)* | *(from exception)* | `exception` presence |
| Error | OTel exception event | `error` string | `statusMessage` | `error` | `exception` |
| Level | *(none)* | *(none)* | `level` (DEBUG/DEFAULT/WARNING/ERROR) | *(none)* | *(none)* |
| Input | `input.value` + typed attrs | `inputs` (dict) | `input` (any JSON) | `input` | `inputs` dict |
| Output | `output.value` + typed attrs | `outputs` (dict) | `output` (any JSON) | `output` | `output` |

## 9. Scoring / eval data model

| Aspect | OpenInference | OTel GenAI | Langfuse | LangSmith | Braintrust | Phoenix |
|---|---|---|---|---|---|---|
| Score object | `EVALUATOR` span | `gen_ai.evaluation.result` event | `Score` object | `Feedback` object | `score` span | `eval.*` attributes |
| Score name | `tool.name` (eval name as span name) | `gen_ai.evaluation.name` | `name` | `key` | span name | `eval.{name}.label` |
| Score value | `output.value` | `gen_ai.evaluation.score.value` | `value` (float or string) | `score` (float) | `scores.{name}` | `eval.{name}.score` |
| Category / label | *(none)* | `gen_ai.evaluation.score.label` | `stringValue` | `value` (string) | label string | `eval.{name}.label` |
| Explanation | *(none)* | `gen_ai.evaluation.explanation` | `comment` | `comment` | `reasoning` | `eval.{name}.explanation` |
| Linked to | `trace_id` via span | span event | `traceId` + `observationId` | `run_id` | `root_span_id` | `span_id` |

## 10. Metrics / server-side performance

OTel GenAI defines these standard metrics (see `02-otel-genai-semconv.md`):

```
gen_ai.client.token.usage           Histogram  {token}
gen_ai.client.operation.duration    Histogram  s
gen_ai.server.request.duration      Histogram  s
gen_ai.server.time_to_first_token   Histogram  s
gen_ai.server.time_per_output_token Histogram  s
```

None of the other tools emit OTel metrics by default — they capture latency and token counts as span attributes and compute aggregates in their own UIs.

## 11. OTel interoperability matrix

| Tool | Accepts OTLP input? | Emits OTLP? | OpenInference native? | OTel GenAI native? |
|---|---|---|---|---|
| Phoenix | Yes (port 4317/6006) | No | Yes | Partial |
| Langfuse | Yes (OTLP HTTP endpoint) | No | Via mapping | Via mapping |
| LangSmith | No | No | No | No |
| Arize AX | Yes | No | Yes | Partial |
| Traceloop | Yes (OTLP exporter) | Yes (OTLP) | No | Yes (own variant) |
| OpenLIT | Yes | Yes (OTLP) | No | Yes |
| Braintrust | Yes (`braintrust[otel]`) | No | No | Partial |
| Helicone | No | No | No | No |
| W&B Weave | No | No | No | No |
