# Conversation cache telemetry

Responses and ChatGPT Codex providers retain a bounded, provider-local summary of
successful completed requests. This is **telemetry**, not a server cache controller:
it does not store prompts, create upstream cache entries, guarantee cache hits, or
change conversation history when entries are evicted.

Use the stable conversation identity supplied by Agent, or scope direct library
calls explicitly:

```python
with provider.session_context("conversation-123"):
    async for event in provider.generate(messages):
        pass

for snapshot in provider.conversation_cache_snapshot():
    print(snapshot.key.model, snapshot.request_count, snapshot.cached_tokens)
```

The read API returns a tuple of frozen `ConversationCacheSnapshot` values, keyed
by sanitized endpoint, requested model, and conversation ID. It exposes completed
request counts, cumulative reported input/cached tokens, last input/cached token
counts, and input-reporting presence. Missing input usage is not an estimated
cache miss. Unscoped requests, failures, malformed responses, and incomplete
responses are omitted. Repeated usage on text and tool events counts only once.

The tracker retains at most 128 conversation keys per provider, with bounded
metadata lengths and least-recently-recorded eviction. Reads do not refresh entry
ordering. Snapshots remain unchanged after future requests. Providers do not
share trackers, and concurrent task-scoped session identities remain isolated.
URL userinfo, query parameters, and fragments are excluded. The tracker retains
no message content, tool schemas, response bodies, or authentication headers.

Opt-in observers receive `conversation_cache_usage` with a `snapshot` after each
recorded completion. Like other observation events, its data can be serialized
using `nagents.observation.json_default`.

`ConversationCacheTracker`, `ConversationCacheKey`, and
`ConversationCacheSnapshot` are also available from `nagents.provider` for
standalone metadata accounting. A standalone tracker accepts a positive
`max_entries` bound. Its eviction affects only local telemetry, never upstream
cache state. Process restart discards these in-memory statistics.
