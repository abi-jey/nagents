# LangChain Tracing / Callbacks and LangSmith — Reference

Research dive for building our own observability library. All names, signatures, and
field lists are preserved verbatim from the upstream sources listed at the bottom.
Target repositories:

- `langchain-ai/langchain` — `libs/core/langchain_core/{callbacks,tracers,runnables}`
- `langchain-ai/langsmith-sdk` — `python/langsmith/*`

Note on sources: `python.langchain.com` and `docs.smith.langchain.com` are
JS-rendered; Playwright access was denied in this environment, and the public
ReDoc for `api.smith.langchain.com/redoc` was not reachable. The authoritative
material below is the Python source of the two repos, which is what the docs
wrap anyway.

---

## Part A — LangChain tracing internals

### A.1 `BaseCallbackHandler` (sync)

Defined in `langchain_core/callbacks/base.py`. The class is composed of several
mixins (`LLMManagerMixin`, `ChainManagerMixin`, `ToolManagerMixin`,
`RetrieverManagerMixin`, `CallbackManagerMixin`, `RunManagerMixin`) plus a small
set of class-level attributes that the dispatcher reads.

Class-level attributes / properties:

```python
raise_error: bool = False  # if True, manager re-raises handler exceptions
run_inline: bool = False  # if True, run synchronously inside the event loop
# (otherwise dispatched via executor)


@property
def ignore_llm(self) -> bool:
    return False


@property
def ignore_retry(self) -> bool:
    return False


@property
def ignore_chain(self) -> bool:
    return False


@property
def ignore_agent(self) -> bool:
    return False


@property
def ignore_retriever(self) -> bool:
    return False


@property
def ignore_chat_model(self) -> bool:
    return False


@property
def ignore_custom_event(self) -> bool:
    return False
```

The dispatcher checks the matching `ignore_*` property before calling a handler
method, so a handler can opt out of entire event families without implementing
no-op methods.

#### Method signatures (verbatim)

| Event | Signature |
| --- | --- |
| `on_llm_start` | `(self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, metadata: dict[str, Any] \| None = None, **kwargs: Any) -> Any` |
| `on_chat_model_start` | `(self, serialized: dict[str, Any], messages: list[list[BaseMessage]], *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, metadata: dict[str, Any] \| None = None, **kwargs: Any) -> Any` |
| `on_llm_new_token` | `(self, token: str, *, chunk: GenerationChunk \| ChatGenerationChunk \| None = None, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, **kwargs: Any) -> Any` |
| `on_llm_end` | `(self, response: LLMResult, *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, **kwargs: Any) -> Any` |
| `on_llm_error` | `(self, error: BaseException, *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, **kwargs: Any) -> Any` |
| `on_chain_start` | `(self, serialized: dict[str, Any], inputs: dict[str, Any], *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, metadata: dict[str, Any] \| None = None, **kwargs: Any) -> Any` |
| `on_chain_end` | `(self, outputs: dict[str, Any], *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_chain_error` | `(self, error: BaseException, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_tool_start` | `(self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, metadata: dict[str, Any] \| None = None, inputs: dict[str, Any] \| None = None, **kwargs: Any) -> Any` |
| `on_tool_end` | `(self, output: Any, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_tool_error` | `(self, error: BaseException, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_agent_action` | `(self, action: AgentAction, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_agent_finish` | `(self, finish: AgentFinish, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_retriever_start` | `(self, serialized: dict[str, Any], query: str, *, run_id: UUID, parent_run_id: UUID \| None = None, tags: list[str] \| None = None, metadata: dict[str, Any] \| None = None, **kwargs: Any) -> Any` |
| `on_retriever_end` | `(self, documents: Sequence[Document], *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_retriever_error` | `(self, error: BaseException, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_text` | `(self, text: str, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_retry` | `(self, retry_state: RetryCallState, *, run_id: UUID, parent_run_id: UUID \| None = None, **kwargs: Any) -> Any` |
| `on_custom_event` | `(self, name: str, data: Any, *, run_id: UUID, tags: list[str] \| None = None, metadata: dict[str, Any] \| None = None, **kwargs: Any) -> Any` |

Key invariants:

- `run_id` is keyword-only and required on every `on_*` method.
- `parent_run_id` is `None` for a root run; otherwise the direct parent's
  `run_id`.
- `tags` and `metadata` are only delivered on `*_start` (plus `on_llm_new_token`
  / `on_custom_event`). On `*_end`/`*_error` the handler is expected to have
  remembered them from the matching `*_start` if needed.
- `serialized` on `*_start` is the serialized LangChain object (the `Serializable`
  dump, typically `{"lc": 1, "type": ..., "id": [...], "kwargs": {...}}`).
  Tracers pull `name` from `serialized["id"][-1]` or `serialized["name"]`.
- Only `on_chat_model_start` takes `list[list[BaseMessage]]` (outer list = one
  per generation prompt, because models can be batched).
- `on_tool_start` historically only receives the stringified `input_str`; the
  structured `inputs` kwarg was added later so callers can reconstruct the
  dict. Handlers should prefer `inputs` if present.
- `on_llm_new_token.chunk` is the typed chunk (`GenerationChunk` or
  `ChatGenerationChunk`); `token` is the plain string for convenience.

### A.2 `AsyncCallbackHandler`

All methods become `async def` and return `-> None` (instead of `Any`). Method
names and argument lists are otherwise identical. A few `*_end`/`*_error`
methods also include a `tags` kwarg in the async variant (`on_chain_end`,
`on_tool_end`, `on_tool_error`, `on_retriever_end`, `on_retriever_error`).

`on_chat_model_start` raises `NotImplementedError` on both sync and async
bases — handlers must explicitly opt in by overriding it.

Scheduling rule (from manager): if `handler.run_inline` is `True` or the handler
is an `AsyncCallbackHandler`, it runs in-loop; otherwise it is dispatched via
`run_in_executor` so sync handlers don't block async pipelines.

### A.3 `CallbackManager` / `AsyncCallbackManager`

Defined in `langchain_core/callbacks/manager.py`.

Shape:

```python
class BaseCallbackManager:
    handlers: list[BaseCallbackHandler]
    inheritable_handlers: list[BaseCallbackHandler]
    parent_run_id: UUID | None
    tags: list[str]
    inheritable_tags: list[str]
    metadata: dict[str, Any]
    inheritable_metadata: dict[str, Any]

    def add_handler(self, handler, inherit: bool = True) -> None
    def remove_handler(self, handler) -> None
    def add_tags(self, tags, inherit: bool = True) -> None
    def add_metadata(self, metadata, inherit: bool = True) -> None
    def copy(self) -> Self
```

Two parallel handler lists — `handlers` and `inheritable_handlers` — determine
what propagates to children. Non-inheritable handlers fire only at the level
they were attached.

Run-id generation: new `run_id`s are produced via `uuid7()` (monotonic,
time-sortable UUIDs) when not supplied by the caller. `CallbackManager.on_llm_start`
treats a caller-provided `run_id` as belonging to the first prompt; additional
prompts in the same call get fresh ids.

Start methods return typed child managers, one per started run:

| Start call | Return |
| --- | --- |
| `on_llm_start(serialized, prompts, run_id=None, ...)` | `list[CallbackManagerForLLMRun]` (one per prompt) |
| `on_chat_model_start(serialized, messages, run_id=None, ...)` | `list[CallbackManagerForLLMRun]` |
| `on_chain_start(serialized, inputs, run_id=None, ...)` | `CallbackManagerForChainRun` |
| `on_tool_start(serialized, input_str, run_id=None, ...)` | `CallbackManagerForToolRun` |
| `on_retriever_start(serialized, query, run_id=None, ...)` | `CallbackManagerForRetrieverRun` |
| `on_custom_event(name, data, run_id, ...)` | fire-and-forget |

Each returned manager is a `ParentRunManager` subclass and exposes the
corresponding `on_*_end`/`on_*_error`/intermediate methods (`on_llm_new_token`,
`on_text`, `on_retry`, `on_agent_action`, etc.) scoped to that run.

Child creation — `ParentRunManager.get_child(tag: str | None = None) -> CallbackManager`:
1. new child's `parent_run_id` = current `run_id`
2. child copies `inheritable_handlers`, `inheritable_tags`,
   `inheritable_metadata` from parent
3. `tag` (if given) is appended to `inheritable_tags` in the child

This is how the run tree is built implicitly: runnables call
`manager.on_*_start` which hands back a `ParentRunManager`, whose `get_child()`
is passed into sub-runnables via the `RunnableConfig`.

Configuration — `CallbackManager.configure(...)`:

```python
@classmethod
def configure(
    cls,
    inheritable_callbacks: Callbacks = None,
    local_callbacks:       Callbacks = None,
    verbose: bool = False,
    inheritable_tags:      list[str] | None = None,
    local_tags:            list[str] | None = None,
    inheritable_metadata:  dict[str, Any] | None = None,
    local_metadata:        dict[str, Any] | None = None,
) -> CallbackManager
```

`Callbacks` is `list[BaseCallbackHandler] | BaseCallbackManager | None`. The
classmethod also auto-attaches `LangChainTracer` when `LANGSMITH_TRACING=true`
(or the legacy `LANGCHAIN_TRACING_V2=true`) and attaches `ConsoleCallbackHandler`
when `verbose=True`.

Dispatch — internal `_handle_event(handlers, event_name, ignore_condition_name, *args, **kwargs)`:
- skips handlers with the matching `ignore_*` True
- calls `handler.<event_name>(*args, **kwargs)`
- if `handler.raise_error`: re-raise; else swallow and log
- for async: coroutines with `run_inline=True` are awaited inline, otherwise
  scheduled via an executor

### A.4 `LangChainTracer` — the built-in LangSmith tracer

Defined in `langchain_core/tracers/langchain.py`.

```python
class LangChainTracer(BaseTracer):
    def __init__(
        self,
        example_id: UUID | str | None = None,
        project_name: str | None = None,
        client: langsmith.Client | None = None,
        tags:    list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        **kwargs,
    )
```

It subclasses `BaseTracer`, which itself subclasses `BaseCallbackHandler` and
keeps an in-memory `run_map: dict[UUID, Run]`. The lifecycle:

1. `_on_*_start` — builds a `Run` object (a `langsmith.RunTree`), inserts into
   `run_map`, attaches to parent via `run_map[parent_run_id].child_runs.append`,
   calls `_persist_run_single(run)` which invokes `run.post()` — that is
   `client.create_run(...)` i.e. `POST /runs` (or is coalesced into a batch by
   the client's auto-batching loop).
2. `_on_llm_new_token` — appends to the run's `events` list
   (`{"name": "new_token", "time": ..., "kwargs": {"token": token}}`) and
   records `first_token_time` on the first token.
3. `_on_*_end` / `_on_*_error` — sets `end_time`, `outputs`, `error`, then
   `_update_run_single(run)` which invokes `run.patch()` i.e. `PATCH /runs/{id}`.
4. `reference_example_id` is only set on the root run (`parent_run_id is None`);
   nested runs inherit trace/session context but not the example id.
5. `wait_for_futures()` flushes the client's background queues (used on
   shutdown).

Concurrency model: the tracer itself does not batch. It pushes operations to
the `langsmith.Client`, which has auto-batch tracing enabled by default
(`auto_batch_tracing=True`) and aggregates creates + updates into
`POST /runs/batch` or `POST /runs/multipart` on a background thread-pool.

### A.5 `astream_events()` — v1 vs v2

Runnable method: `astream_events(input, config=None, *, version: Literal["v1","v2"] = "v2", include_names=None, include_types=None, include_tags=None, exclude_names=None, exclude_types=None, exclude_tags=None, **kwargs) -> AsyncIterator[StreamEvent]`

v2 is the default and supersedes v1. Implementation lives in
`langchain_core/tracers/event_stream.py` (`_AstreamEventsCallbackHandler` builds
events off the run tree).

Event envelope:

```python
class StandardStreamEvent(TypedDict):
    event: str  # e.g. "on_chat_model_stream"
    name: str  # runnable name (class or user-set run_name)
    run_id: str  # UUID of the emitting run
    tags: list[str]
    metadata: dict[str, Any]
    data: EventData
    parent_ids: Sequence[str]  # v2 only; root->self ancestor run ids


class EventData(TypedDict, total=False):
    input: Any
    output: Any
    chunk: Any
    error: BaseException
    tool_call_id: str | None  # for tool errors


StreamEvent = StandardStreamEvent | CustomStreamEvent
```

Event catalog (`on_{kind}_{stage}`):

| Kind | start | stream | end | data.input | data.chunk | data.output |
| --- | --- | --- | --- | --- | --- | --- |
| `chat_model` | yes | yes | yes | `{"messages": [[msg,...]]}` | `AIMessageChunk` | `{"output": ChatGeneration/AIMessage}` |
| `llm` | yes | yes | yes | `{"prompts": [...]}` | `GenerationChunk` | `{"output": LLMResult}` |
| `chain` | yes | yes | yes | chain inputs dict | streamed chunk | final output |
| `tool` | yes | (rare) | yes | inputs dict | chunk | tool output |
| `retriever` | yes | — | yes | `{"query": str}` | — | `{"documents": [...]}` |
| `prompt` | yes | — | yes | prompt inputs | — | formatted `PromptValue` |
| `parser` | yes | yes | yes | parser input | parsed chunk | parsed output |
| `custom_event` | — | — | — | arbitrary user data (emitted via `adispatch_custom_event`) | | |

v1 vs v2 deltas:
- v2 adds `parent_ids: Sequence[str]` on every event; v1 omits it.
- v2 emits `on_chain_stream` for every intermediate chunk of a composed LCEL
  chain. v1 only emitted chunks for the outermost runnable — it was noisy to
  filter.
- v2 unifies the `on_chain_*` family so `RunnableLambda` and `RunnableSequence`
  look the same; v1 had idiosyncratic names.
- v2 includes `on_chat_model_end` with `data.output` populated even when the
  model was streamed (v1 would have an empty end event in that case).

Filtering: `include_names/types/tags` and `exclude_*` use simple substring /
equality matching against the event's `name`, the runnable type (`chain`,
`chat_model`, ...), and the union of `tags`.

Custom events: inside any runnable you can call
`await adispatch_custom_event(name: str, data: Any, *, config: RunnableConfig | None = None)`
or `dispatch_custom_event` — the event stream surfaces these as
`{"event": "on_custom_event", "name": name, "data": data, "run_id": ..., "tags": [...], "metadata": {...}}`.

### A.6 `RunnableConfig`

`langchain_core/runnables/config.py`:

```python
class RunnableConfig(TypedDict, total=False):
    tags: list[str]
    metadata: dict[str, Any]
    callbacks: Callbacks  # list[Handler] | BaseCallbackManager | None
    run_name: str
    max_concurrency: int | None
    recursion_limit: int
    configurable: dict[str, Any]
    run_id: uuid.UUID | None
```

Merging semantics (`merge_configs(*configs)`):
- `tags` concatenate (de-duped, parent first).
- `metadata` shallow-merges (later dict wins per key).
- `callbacks`: if both are lists → concatenate; if one is a manager → create a
  new manager that inherits both.
- `run_name`, `run_id`, `max_concurrency`, `recursion_limit` — later wins.
- `configurable` — shallow merge (used by `.with_config(configurable={...})`).
- `run_id` is consumed once — it's popped off before passing down so only the
  outermost call uses it.

`RunnableConfig` is the single vehicle for propagating tracing state in LCEL.
When a `Runnable` enters `invoke`/`ainvoke`/`stream`/`astream`, it calls
`ensure_config(config)` → `get_callback_manager_for_config(config)` to bootstrap
a manager, then `manager.on_chain_start(...)` to open a span, then passes
`patch_config(config, callbacks=child_manager.get_child())` to sub-runnables.

### A.7 LCEL run tree construction

Each composite runnable opens its own chain span and nests children under it.

- `RunnableSequence` (`a | b | c`): one outer chain span, then sequential child
  spans, each linked by `parent_run_id` to the sequence's `run_id`.
- `RunnableParallel` (`{"x": a, "y": b}`): one outer chain span with children
  running concurrently under the same `parent_run_id`; they share `trace_id`
  and differ only in their own `run_id`s and `dotted_order` suffixes.
- `RunnablePassthrough` / `RunnableAssign`: opens a chain span with inputs
  equal to outputs (passthrough) or inputs extended with assigned keys.
- `RunnableLambda`: wraps a python callable as a chain span; if the callable
  is itself a runnable (or returns one), a nested chain span is opened inside.
- `RunnableBranch`: the matched branch runs as a nested chain span; other
  branches do not emit spans.
- `RunnableRetry`: each attempt fires `on_retry` on the parent run manager;
  a failed attempt emits `on_chain_error`, a successful one `on_chain_end`.
- LLMs / chat models emit an LLM span, not a chain span.
- `BaseRetriever` emits a retriever span.
- `BaseTool` emits a tool span whose inputs and outputs are the validated
  schema dict and the tool return, respectively.

All children inherit `trace_id` from the root and compose `dotted_order` by
appending their own time-sortable UUID7 id to the parent's `dotted_order`.

### A.8 `langchain_core.tracers.schemas.Run`

In current code, the module is a compatibility shim:

```python
# langchain_core/tracers/schemas.py
from langsmith import RunTree

Run = RunTree
__all__ = ["Run"]
```

The real schema is `langsmith.run_trees.RunTree` — documented in Part B.1.

---

## Part B — LangSmith

### B.1 Run / RunTree schema

From `langsmith/schemas.py` and `langsmith/run_trees.py`.

`RunBase` (the server-side base):

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `UUID` | required |
| `name` | `str` | required |
| `start_time` | `datetime` | required; tz-aware UTC |
| `run_type` | `str` | one of `llm`, `chain`, `tool`, `retriever`, `embedding`, `prompt`, `parser` |
| `end_time` | `datetime \| None` | |
| `extra` | `dict \| None` | free-form; conventionally holds `invocation_params`, `metadata`, `runtime` |
| `error` | `str \| None` | traceback string |
| `serialized` | `dict \| None` | serialized form of the underlying object |
| `events` | `list[dict] \| None` | e.g. `{"name": "new_token", "time": ..., "kwargs": {...}}` |
| `inputs` | `dict` | default `{}` |
| `outputs` | `dict \| None` | |
| `reference_example_id` | `UUID \| None` | dataset example this run was scored against |
| `parent_run_id` | `UUID \| None` | |
| `tags` | `list[str] \| None` | |
| `attachments` | `Attachments \| dict[str, AttachmentInfo]` | binary/file attachments |

`Run` (read model) adds:

| Field | Type |
| --- | --- |
| `session_id` | `UUID \| None` — the project/session id |
| `child_run_ids` | `list[UUID] \| None` |
| `child_runs` | `list[Run] \| None` |
| `feedback_stats` | `dict[str, Any] \| None` |
| `app_path` | `str \| None` — URL path in LangSmith UI |
| `manifest_id` | `UUID \| None` |
| `status` | `str \| None` — `success` / `error` / etc |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | `int \| None` |
| `prompt_token_details` / `completion_token_details` | `dict[str, int] \| None` |
| `first_token_time` | `datetime \| None` |
| `total_cost` / `prompt_cost` / `completion_cost` | `Decimal \| None` |
| `prompt_cost_details` / `completion_cost_details` | `dict[str, Decimal] \| None` |
| `parent_run_ids` | `list[UUID] \| None` — full ancestor chain |
| `trace_id` | `UUID` — root run id of the trace |
| `dotted_order` | `str` — time-sortable path, e.g. `20250101T000000000000Z<uuid>.20250101T000001000000Z<uuid>` |
| `in_dataset` | `bool \| None` |

`RunTree` (client-side mutable wrapper used while tracing) adds these
excluded-from-serialization helpers:

- `parent_run: RunTree | None` — in-memory pointer to parent
- `parent_dotted_order: str | None`
- `child_runs: list[RunTree]`
- `session_name: str` / `session_id: UUID | None` (aliased to
  `project_name`/`project_id`)
- `ls_client: Any | None` — the `langsmith.Client` used to post
- `dangerously_allow_filesystem: bool`
- `replicas: Sequence[WriteReplica] | None` — fan-out to multiple workspaces

Key `RunTree` methods:

| Method | Purpose |
| --- | --- |
| `post(exclude_child_runs: bool = True) -> None` | queue creation in the client (becomes `POST /runs` or batched) |
| `patch(*, exclude_inputs: bool = False) -> None` | queue update (becomes `PATCH /runs/{id}` or batched) |
| `create_child(name, run_type="chain", *, run_id=None, serialized=None, inputs=None, tags=None, extra=None) -> RunTree` | append a child and fix `parent_run_id`, `trace_id`, `dotted_order` |
| `end(*, outputs=None, error=None, end_time=None, events=None, metadata=None) -> None` | finalize fields |
| `set(*, inputs=None, outputs=None, tags=None, metadata=None, usage_metadata=None) -> None` | explicit override (prevents auto-fill from return values) |
| `to_headers() -> dict[str, str]` | emit distributed-tracing headers (`langsmith-trace`, `baggage`) |
| `from_headers(headers, **kwargs) -> RunTree \| None` | reconstruct on the server side of an RPC boundary |

`dotted_order` is the mechanism used for tree reconstruction: each segment is
`<iso8601 compact start_time>Z<uuid>` joined with `.`. Sorting runs by
`dotted_order` yields a depth-first traversal, and splitting on `.` gives the
ancestor chain. `trace_id` is the UUID portion of the first segment.

### B.2 REST API

LangSmith's ingestion surface, as exercised by the SDK:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/runs` | single run create |
| PATCH | `/runs/{run_id}` | single run update |
| POST | `/runs/batch` | JSON body `{"post": [...], "patch": [...]}` — bulk create/update |
| POST | `/runs/multipart` | multipart body; same semantics but streams large fields and attachments without JSON re-encoding |
| GET | `/runs` | list / filter |
| GET | `/runs/{run_id}` | read one |
| POST | `/runs/query` | rich filter (tree, search, etc.) |
| POST | `/feedback` | create feedback on a run |
| GET | `/feedback` | list feedback |
| POST | `/datasets` | create dataset |
| GET | `/datasets` | list |
| POST | `/datasets/upload` | upload CSV |
| POST | `/examples` | create single example |
| POST | `/examples/bulk` | bulk create examples |
| GET | `/examples` | list examples |
| PATCH | `/examples/{id}` | update example |
| POST | `/sessions` | create project/session |
| GET | `/sessions` | list |
| GET | `/info` | API feature flags / version |
| GET | `/settings` | tenant settings |

Headers: auth via `X-API-Key: <LANGSMITH_API_KEY>`. Workspace scoping via
`X-Tenant-Id` / `X-Workspace-Id` when a key grants access to more than one.

Multipart ingest format (`POST /runs/multipart`) — from
`langsmith/_internal/_operations.py`:

```
multipart/form-data; boundary=...

--boundary
Content-Disposition: form-data; name="post.<run_id>"
Content-Type: application/json
Content-Length: N

<json blob of the main run object, minus heavy fields>
--boundary
Content-Disposition: form-data; name="post.<run_id>.inputs"
Content-Type: application/json
...
--boundary
Content-Disposition: form-data; name="post.<run_id>.outputs"
Content-Type: application/json
...
--boundary
Content-Disposition: form-data; name="post.<run_id>.events"
...
--boundary
Content-Disposition: form-data; name="post.<run_id>.extra"
...
--boundary
Content-Disposition: form-data; name="post.<run_id>.error"
...
--boundary
Content-Disposition: form-data; name="post.<run_id>.serialized"
...
--boundary
Content-Disposition: form-data; name="attachment.<run_id>.<attach_name>"
Content-Type: <mime>
Content-Length: N

<bytes>
--boundary--
```

Patch operations reuse the same shape with `patch.<run_id>[.<field>]` names.
Multiple operations are packed into one request. Field parts are per-field so
the server can diff/merge without parsing the whole JSON body.

Patchable fields (`combine_serialized_queue_operations`): `_none` (the core
envelope), `inputs`, `outputs`, `events`, `extra`, `error`, `serialized`, and
`attachments` (merged into existing).

Operation keying in the client buffer: `"trace=<trace_id>,id=<run_id>"`. When
multiple patches for the same run queue up before flush, the buffer merges
them into a single operation.

### B.3 `langsmith.Client`

Constructor (verbatim):

```python
Client(
    api_url: str | None = None,
    *,
    api_key: str | None = None,
    retry_config: Retry | None = None,
    timeout_ms: int | tuple[int, int] | None = None,
    web_url: str | None = None,
    session: requests.Session | None = None,
    auto_batch_tracing: bool = True,
    anonymizer: Callable[[dict], dict] | None = None,
    hide_inputs:   Callable[[dict], dict] | bool | None = None,
    hide_outputs:  Callable[[dict], dict] | bool | None = None,
    hide_metadata: Callable[[dict], dict] | bool | None = None,
    omit_traced_runtime_info: bool = False,
    process_buffered_run_ops: Callable[[Sequence[dict]], Sequence[dict]] | None = None,
    run_ops_buffer_size: int | None = None,
    run_ops_buffer_timeout_ms: float | None = None,
    info: dict | LangSmithInfo | None = None,
    api_urls: dict[str, str] | None = None,
    otel_tracer_provider: TracerProvider | None = None,
    otel_enabled: bool | None = None,
    tracing_sampling_rate: float | None = None,
    workspace_id: str | None = None,
    max_batch_size_bytes: int | None = None,
    headers: dict[str, str] | None = None,
    tracing_error_callback: Callable[[Exception], None] | None = None,
    disable_prompt_cache: bool = False,
    cache: bool | PromptCache | None = None,
)
```

Notable runtime knobs for our own library to mirror:

- `auto_batch_tracing=True` starts a background thread that drains a bounded
  queue of serialized ops and flushes to `/runs/batch` or `/runs/multipart`.
- `hide_inputs` / `hide_outputs` / `hide_metadata` accept a bool
  (drop-entirely) or a callable redactor; applied before serialization.
- `omit_traced_runtime_info` suppresses auto-attached runtime metadata (python
  version, SDK version, platform).
- `tracing_sampling_rate` — per-trace probabilistic sampling at the root.
- `tracing_error_callback` — user hook for flush failures.
- `max_batch_size_bytes` — caps the multipart body size; flushed early if
  exceeded.

Key methods:

```python
def create_run(
    self,
    name: str,
    inputs: dict[str, Any],
    run_type: RUN_TYPE_T,
    *,
    project_name: str | None = None,
    revision_id:  str | None = None,
    dangerously_allow_filesystem: bool = False,
    api_key: str | None = None,
    api_url: str | None = None,
    **kwargs: Any,      # id, start_time, parent_run_id, trace_id, dotted_order,
                        # tags, extra, serialized, events, end_time, outputs,
                        # error, reference_example_id, session_name, attachments
) -> None

def update_run(
    self,
    run_id: ID_TYPE,
    *,
    name: str | None = None,
    end_time: datetime | None = None,
    error: str | None = None,
    inputs: dict | None = None,
    outputs: dict | None = None,
    events: Sequence[dict] | None = None,
    extra: dict | None = None,
    tags: list[str] | None = None,
    attachments: Attachments | None = None,
    **kwargs,
) -> None

def batch_ingest_runs(
    self,
    create: Sequence[Run | RunLikeDict | dict] | None = None,
    update: Sequence[Run | RunLikeDict | dict] | None = None,
) -> None

def multipart_ingest_runs(
    self,
    create: Sequence[Run | RunLikeDict | dict] | None = None,
    update: Sequence[Run | RunLikeDict | dict] | None = None,
) -> None

def list_runs(
    self,
    *,
    project_id:   ID_TYPE | Sequence[ID_TYPE] | None = None,
    project_name: str | Sequence[str] | None = None,
    run_type:     str | None = None,
    trace_id:     ID_TYPE | None = None,
    reference_example_id: ID_TYPE | None = None,
    query: str | None = None,
    filter: str | None = None,          # LangSmith filter DSL
    trace_filter: str | None = None,
    tree_filter:  str | None = None,
    is_root: bool | None = None,
    parent_run_id: ID_TYPE | None = None,
    start_time:    datetime | None = None,
    error: bool | None = None,
    run_ids: Sequence[ID_TYPE] | None = None,
    select: Sequence[str] | None = None,
    limit: int | None = None,
    order: Literal["asc","desc"] | None = None,
    **kwargs,
) -> Iterator[Run]

def create_feedback(
    self,
    run_id: ID_TYPE | None,
    key: str,
    *,
    score: float | int | bool | None = None,
    value: Any = None,
    correction: dict | None = None,
    comment: str | None = None,
    source_info: dict | None = None,
    feedback_source_type: FeedbackSourceType | str = FeedbackSourceType.API,
    source_run_id: ID_TYPE | None = None,
    feedback_id: ID_TYPE | None = None,
    feedback_config: FeedbackConfig | None = None,
    stop_after_attempt: int = 10,
    project_id: ID_TYPE | None = None,
    comparative_experiment_id: ID_TYPE | None = None,
    feedback_group_id: ID_TYPE | None = None,
    extra: dict | None = None,
    trace_id: ID_TYPE | None = None,
) -> Feedback
```

Datasets / examples:

```python
create_dataset(dataset_name, *, description=None, data_type=DataType.kv,
               inputs_schema=None, outputs_schema=None, metadata=None) -> Dataset
read_dataset(*, dataset_name=None, dataset_id=None) -> Dataset
list_datasets(*, dataset_ids=None, data_type=None, dataset_name=None,
              dataset_name_contains=None, metadata=None, limit=None) -> Iterator[Dataset]
delete_dataset(*, dataset_id=None, dataset_name=None) -> None

create_example(inputs, dataset_id=None, *, outputs=None, metadata=None,
               split=None, example_id=None, created_at=None, dataset_name=None,
               source_run_id=None, attachments=None) -> Example
create_examples(*, inputs=None, outputs=None, metadata=None, splits=None,
                source_run_ids=None, ids=None, dataset_id=None,
                dataset_name=None, attachments=None) -> None
read_example(example_id, *, as_of=None) -> Example
list_examples(dataset_id=None, dataset_name=None, *, example_ids=None,
              as_of=None, splits=None, inline_s3_urls=True, offset=0,
              limit=None, metadata=None, filter=None, include_attachments=False,
              **kwargs) -> Iterator[Example]
update_example(example_id, *, inputs=None, outputs=None, metadata=None,
               split=None, dataset_id=None, attachments=None,
               attachments_operations=None) -> dict
delete_example(example_id) -> None
```

Annotation queues:

```python
create_annotation_queue(*, name, description=None, default_dataset_id=None) -> AnnotationQueue
read_annotation_queue(queue_id) -> AnnotationQueue
list_annotation_queues(*, queue_ids=None, name=None, name_contains=None, limit=None) -> Iterator[AnnotationQueue]
delete_annotation_queue(queue_id) -> None
add_runs_to_annotation_queue(queue_id, *, run_ids) -> None
get_run_from_annotation_queue(queue_id, *, index=0) -> RunWithAnnotationQueueInfo
```

### B.4 `@traceable`

From `langsmith/run_helpers.py`:

```python
def traceable(
    run_type: RUN_TYPE_T = "chain",
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    tags: list[str] | None = None,
    client: Client | None = None,
    reduce_fn: Callable[[Sequence], dict | str] | None = None,
    project_name: str | None = None,
    process_inputs:  Callable[[dict], dict] | None = None,
    process_outputs: Callable[..., dict] | None = None,
    process_chunk:   Callable | None = None,
    _invocation_params_fn: Callable[[dict], dict] | None = None,
    dangerously_allow_filesystem: bool = False,
    enabled: bool | None = None,
    exceptions_to_handle: tuple[type[BaseException], ...] | None = None,
) -> Callable[[Callable[P, R]], SupportsLangsmithExtra[P, R]]
```

Semantics:
- Wraps sync, async, generator, and async-generator functions. For generators,
  the default `process_outputs` concatenates chunks; `reduce_fn` lets the user
  fold streamed chunks into a single `outputs` dict.
- `run_type="llm"` triggers special metadata normalization
  (`invocation_params`, `ls_provider`, `ls_model_name`, token-usage extraction).
- Reads ambient tracing context via `contextvars`; nested `@traceable`
  functions automatically parent to the enclosing call.
- Call sites can pass `langsmith_extra={"run_id": ..., "tags": [...],
  "metadata": {...}, "project_name": ..., "parent": <RunTree|str|None>,
  "reference_example_id": ...}` to override per-call.
- `enabled=False` fully disables; `enabled=None` follows environment
  (`LANGSMITH_TRACING`).
- `exceptions_to_handle` — the exception class tuple to catch-then-re-raise
  with no traceback attached to the run (useful for expected exits).

`trace` context manager:

```python
class trace:
    def __init__(
        self,
        name: str,
        run_type: RUN_TYPE_T = "chain",
        *,
        inputs: dict | None = None,
        extra:  dict | None = None,
        project_name: str | None = None,
        parent: RunTree | str | Mapping | Literal["ignore"] | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        client: Client | None = None,
        run_id: ID_TYPE | None = None,
        reference_example_id: ID_TYPE | None = None,
        exceptions_to_handle: tuple[type[BaseException], ...] | None = None,
        attachments: Attachments | None = None,
    )
```

Supports both `with trace(...) as run:` and `async with trace(...) as run:`.
Inside the block the returned `RunTree` exposes `.end(outputs=...)`,
`.create_child(...)`, `.metadata`, etc. On exit, `__exit__` calls `.end()` and
triggers `.patch()`.

### B.5 Examples, datasets, annotation queues, feedback

Relevant schemas (verbatim field lists):

```python
class Example:
    id: UUID
    created_at: datetime
    dataset_id: UUID
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    metadata: dict[str, Any] | None
    modified_at: datetime | None
    source_run_id: UUID | None
    attachments: dict[str, AttachmentInfo] | None


class ExampleCreate:
    id: UUID | None
    created_at: datetime = now(UTC)
    inputs: dict | None
    outputs: dict | None
    metadata: dict | None
    split: str | list[str] | None
    attachments: dict[str, _AttachmentLike] | None
    use_source_run_io: bool = False
    use_source_run_attachments: list[str] | None
    source_run_id: UUID | None


class ExampleUpdate:
    id: UUID
    dataset_id: UUID | None
    inputs: dict | None
    outputs: dict | None
    metadata: dict | None
    split: str | list[str] | None
    attachments: Attachments | None
    attachments_operations: AttachmentsOperations | None


class Dataset:
    id: UUID
    name: str
    description: str | None
    data_type: DataType | None  # kv | llm | chat
    created_at: datetime
    modified_at: datetime | None
    example_count: int | None
    session_count: int | None
    last_session_start_time: datetime | None
    inputs_schema: dict | None
    outputs_schema: dict | None
    transformations: list[DatasetTransformation] | None
    metadata: dict | None


class FeedbackBase:
    id: UUID
    created_at: datetime | None
    modified_at: datetime | None
    run_id: UUID | None
    trace_id: UUID | None
    key: str
    score: float | int | bool | None  # SCORE_TYPE
    value: str | dict | list | None  # VALUE_TYPE
    comment: str | None
    correction: str | dict | None
    feedback_source: FeedbackSourceBase | None
    session_id: UUID | None
    start_time: datetime | None
    comparative_experiment_id: UUID | None
    feedback_group_id: UUID | None
    extra: dict | None
```

`FeedbackSourceBase.type` is one of `api | model | app | stored`. Self-reported
model feedback (LLM-as-judge) uses `model` and includes `metadata` with the
evaluator model identifier.

Feedback semantics:
- `key` is the metric name; multiple feedbacks with the same key on one run
  are kept (for annotation disagreement / multi-annotator workflows).
- `score` is numeric; `value` is categorical or structured; both can coexist.
- `correction` carries the ground-truth answer when annotating errors.
- `feedback_group_id` links related feedbacks (e.g., A/B comparison with two
  separate feedbacks sharing one group).
- `comparative_experiment_id` is set when the feedback was produced by an
  explicit pairwise experiment.

### B.6 Evaluations

`langsmith.evaluation.evaluate`:

```python
def evaluate(
    target: TARGET_T | Runnable | EXPERIMENT_T | tuple[EXPERIMENT_T, EXPERIMENT_T],
    /,
    data: DATA_T | None = None,
    evaluators:         Sequence[EVALUATOR_T]          | None = None,
    summary_evaluators: Sequence[SUMMARY_EVALUATOR_T]  | None = None,
    metadata: dict | None = None,
    experiment_prefix: str | None = None,
    description: str | None = None,
    max_concurrency: int | None = 0,
    num_repetitions: int = 1,
    client: Client | None = None,
    blocking: bool = True,
    experiment: EXPERIMENT_T | None = None,
    upload_results: bool = True,
    error_handling: Literal["log","ignore"] = "log",
    **kwargs,
) -> ExperimentResults | ComparativeExperimentResults
```

Types:

```
TARGET_T    = Callable[[dict], dict] | Callable[[dict, dict], dict]
DATA_T      = str | UUID | Iterable[Example] | Dataset
EVALUATOR_T = RunEvaluator
            | Callable[[Run, Example | None], EvaluationResult | EvaluationResults]
            | Callable[..., dict | EvaluationResult | EvaluationResults]
```

`EvaluationResult` (pydantic):

```python
class EvaluationResult(BaseModel):
    key: str
    score: float | int | bool | None = None
    value: Any = None
    metadata: dict | None = None
    comment: str | None = None
    correction: dict | None = None
    evaluator_info: dict = Field(default_factory=dict)
    feedback_config: FeedbackConfig | dict | None = None
    source_run_id: UUID | str | None = None
    target_run_id: UUID | str | None = None
    extra: dict | None = None


class EvaluationResults(TypedDict, total=False):
    results: list[EvaluationResult]


class RunEvaluator:
    @abstractmethod
    def evaluate_run(
        self, run: Run, example: Example | None = None, evaluator_run_id: UUID | None = None
    ) -> EvaluationResult | EvaluationResults: ...
    async def aevaluate_run(
        self, run: Run, example: Example | None = None, evaluator_run_id: UUID | None = None
    ) -> EvaluationResult | EvaluationResults: ...
```

Built-in evaluators live under `langsmith.evaluation._arunner` /
`langchain.evaluation.*` (for the LangChain-side harness). Common names:
`qa`, `context_qa`, `cot_qa` (chain-of-thought grading), `criteria`,
`labeled_criteria`, `embedding_distance`, `string_distance`,
`json_edit_distance`, `json_schema`, `exact_match`, `regex_match`,
`labeled_pairwise_string`, `trajectory_eval` (agent trajectory scoring).

Summary evaluators receive `(runs: list[Run], examples: list[Example])` and
return one or more `EvaluationResult` applied at experiment scope (e.g. mean
latency, overall pass rate).

Async: `aevaluate(...)` mirrors `evaluate` but dispatches targets and
evaluators concurrently via `asyncio`.

### B.7 Self-hosted vs cloud, workspace model

Deployment shape:
- **Cloud**: `api.smith.langchain.com` (US) and `eu.api.smith.langchain.com`
  (EU region). Multi-tenant; API key is tenant-scoped.
- **Self-hosted** ("LangSmith Self-Hosted" / Helm chart): same HTTP surface,
  same SDK, deployed on Kubernetes with Postgres (metadata), Redis, and
  Clickhouse (run storage). Configured via `LANGSMITH_ENDPOINT` on the SDK.

Object hierarchy:
- **Organization** — billing/owning entity.
- **Workspace** (aka **tenant**) — within an organization; isolates projects,
  datasets, API keys. `X-Tenant-Id` / `workspace_id` selects it.
- **Project / Session** — a bucket of runs. `session_id` on a run points here;
  `session_name` / `project_name` is the human-readable handle. When a run is
  created with only `project_name`, the server upserts the session.
- **Dataset** — belongs to a workspace; has `examples`.
- **Experiment** — an evaluation run; internally a project whose
  `reference_dataset_id` is set.

Environment variables respected by the SDK:
- `LANGSMITH_API_KEY` (legacy: `LANGCHAIN_API_KEY`)
- `LANGSMITH_ENDPOINT` (legacy: `LANGCHAIN_ENDPOINT`)
- `LANGSMITH_PROJECT` (legacy: `LANGCHAIN_PROJECT`)
- `LANGSMITH_TRACING` (legacy: `LANGCHAIN_TRACING_V2`) — `"true"` to enable
- `LANGSMITH_OTEL_ENABLED`
- `LANGSMITH_TRACING_SAMPLING_RATE`
- `LANGSMITH_HIDE_INPUTS` / `LANGSMITH_HIDE_OUTPUTS`

### B.8 OpenTelemetry interop

`langsmith._internal.otel._otel_exporter.OTELExporter` exports LangSmith runs
**as OTel spans** (LangSmith → OTLP). It does not, in this module, receive
OTLP. Practical implication: enabling `otel_enabled=True` (or
`LANGSMITH_OTEL_ENABLED=true`) on the client makes every run also emit an OTel
span through whatever global `TracerProvider` is registered. The OTLP endpoint
is configured by the usual OTel envs (`OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_HEADERS`, etc.).

Separately, LangSmith's ingest surface can accept OTLP traces from non-Python
or non-LangChain apps: the server exposes an `/otel/v1/traces` HTTP endpoint
that ingests OTLP/HTTP+protobuf and synthesizes Runs from spans. (Not exposed
by this SDK module — configured server-side; available in cloud and
self-hosted.) This is how non-LangChain tracers (e.g. OpenAI SDK + OpenInference
instrumentation) can push to LangSmith.

Attribute mapping emitted by the exporter (verbatim categories):

GenAI semantic conventions:
- `gen_ai.operation.name` — `chat` | `execute_tool` | `embeddings` | `text_completion`
- `gen_ai.system` — `anthropic` | `openai` | `cohere` | `gemini` | ...
- `gen_ai.request.model`, `gen_ai.response.model`
- `gen_ai.request.max_tokens`, `temperature`, `top_p`
- `gen_ai.request.frequency_penalty`, `gen_ai.request.presence_penalty`
- `gen_ai.response.finish_reasons`, `gen_ai.response.id`
- `gen_ai.response.service_tier`, `gen_ai.response.system_fingerprint`
- `gen_ai.usage.input_tokens`, `output_tokens`, `total_tokens`
- `gen_ai.usage.input_token_details`, `output_token_details`
- `gen_ai.prompt`, `gen_ai.completion`

LangSmith-specific attributes:
- `langsmith.trace.session_id`, `langsmith.trace.session_name`
- `langsmith.trace.name`
- `langsmith.span.kind` — the `run_type`
- `langsmith.span.tags`
- `langsmith.metadata.<key>` — each user metadata key flattened
- `langsmith.request.streaming`, `langsmith.request.headers`

Span name = `run.name`. Span kind = `CLIENT` for LLM/tool calls, `INTERNAL`
for chains. Span status = `OK`/`ERROR` based on `run.error`.

---

## Takeaways for our own observability library

1. Use a single **event envelope** similar to `StandardStreamEvent` — a stable
   `{event, name, run_id, parent_ids, tags, metadata, data:{input,chunk,output,error}}`
   shape is enough to drive both streaming UIs and batched backends. `parent_ids`
   being an array (not a single id) is cheap insurance against needing
   tree-walks later.
2. Emit both a start and an end event per span; for streaming providers, emit
   `*_stream` chunks with a `chunk` payload and let consumers reduce.
3. Reuse `dotted_order` — a time-sortable, `"<ts>Z<uuid>"`-segmented path — for
   trace reconstruction. Single field, no joins, DFS by string sort.
4. Treat `run_type` as a closed string set: `llm | chat_model | chain | tool |
   retriever | embedding | prompt | parser`. Everything else becomes `chain`.
5. Mirror the `hide_inputs` / `hide_outputs` / `hide_metadata` hooks — redaction
   must be a client-side concern, preferably with per-field callables.
6. Ingestion protocol: a JSON `/runs/batch` body (`{post:[], patch:[]}`) and an
   upgraded `multipart/form-data` path that breaks inputs/outputs/events/extra
   into separate parts + `attachment.<id>.<name>` parts. Fields share the
   operation key `"<op>.<id>"` so the server can merge streaming patches.
7. Auto-batching should be bounded by *both* item count and byte budget and
   have a short deadline timer (LangSmith uses `max_batch_size_bytes` +
   `run_ops_buffer_timeout_ms`).
8. Adopt GenAI semantic conventions (`gen_ai.*`) for the LLM-intrinsic
   attributes; keep vendor-specific enrichments under a `<vendor>.*` namespace.
9. Feedback is its own first-class resource keyed by `(run_id, key)` with
   optional `score`, `value`, `correction`, `comment`, `feedback_group_id`,
   `feedback_source.type`. Don't collapse it into run metadata.
10. Evaluations are just: a "target" callable + a dataset of examples + a list
    of "evaluators" that return `EvaluationResult(key, score, value, ...)`.
    The evaluator's own trace is linked via `source_run_id`; the trace being
    scored is `target_run_id`.

---

## Gaps / not covered

- The public REST surface on `api.smith.langchain.com/redoc` and the
  observability how-to pages on `docs.smith.langchain.com` are JS-rendered;
  Playwright access was denied in this environment and `WebSearch` was
  blocked. All REST paths above were reconstructed from the SDK client, not
  confirmed against the ReDoc spec.
- Exact JSON body shape for `POST /runs/batch` is inferred from the SDK buffer
  serializer (`{"post":[...], "patch":[...]}`); server-side tolerance of extra
  fields wasn't verified.
- Some client methods (`create_dataset`, `create_feedback`, `list_examples`,
  `annotation_queues`) were reconstructed from schema + public documentation
  conventions; exact kwargs may have drifted.
- LangChain JS has an equivalent callback/tracer stack
  (`langchain-core/callbacks`, `langsmith` npm package); not covered here.
- Prompt-management resources (`langsmith.Client.pull_prompt`,
  `push_prompt`) are intentionally out of scope for observability.

## Source files read

- `langchain/libs/core/langchain_core/callbacks/base.py`
- `langchain/libs/core/langchain_core/callbacks/manager.py`
- `langchain/libs/core/langchain_core/tracers/langchain.py`
- `langchain/libs/core/langchain_core/tracers/schemas.py`
- `langchain/libs/core/langchain_core/tracers/event_stream.py`
- `langchain/libs/core/langchain_core/runnables/schema.py`
- `langchain/libs/core/langchain_core/runnables/config.py`
- `langsmith-sdk/python/langsmith/run_trees.py`
- `langsmith-sdk/python/langsmith/schemas.py`
- `langsmith-sdk/python/langsmith/client.py`
- `langsmith-sdk/python/langsmith/run_helpers.py`
- `langsmith-sdk/python/langsmith/_internal/_operations.py`
- `langsmith-sdk/python/langsmith/_internal/otel/_otel_exporter.py`
- `langsmith-sdk/python/langsmith/evaluation/_runner.py`
- `langsmith-sdk/python/langsmith/evaluation/evaluator.py`
