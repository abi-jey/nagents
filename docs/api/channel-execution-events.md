# Channel execution events

Channels provide both connector operations and optional execution notices.
`Channel.actions` advertises operations exposed by the existing
`ChannelRuntime` tools: `channel_list`, `channel_send`, and `channel_action`.
`async Channel.on_event(event: ChannelExecutionEvent) -> None` adds a separate
status hook. Its default implementation is a no-op, so existing connectors work
without modification. `Channel.activity(ChannelActivity)` remains the typing API.

Import the new API from `nagents.channels`:

```python
from nagents.channels import ChannelExecutionEvent
from nagents.channels import ChannelExecutionPhase
from nagents.channels import dispatch_channel_execution_event
from nagents.channels import sanitize_channel_tool_arguments
```

## Contract

`ChannelExecutionEvent` is a frozen dataclass with required `conversation_id`,
`session_id`, and `phase`; optional `thread_id`, `run_id`, `activation_id`,
`call_id`, `tool_name`, and `message_id` default to empty strings.
`destination` is a read-only alias of `conversation_id`.
`tool_arguments: dict[str, ChannelValue]` defaults to `{}` and
`tool_failed: bool` defaults to `False`.

`ChannelExecutionPhase` is a literal union:

| Phase | Meaning / standalone `Agent.listen` mapping |
| --- | --- |
| `run_started` | An inbox activation was claimed, before model work starts. |
| `tool_requested` | `ToolCallEvent` observed; execution and approval are **not** established. Includes the call ID, tool name, and sanitized arguments. |
| `tool_completed` | `ToolResultEvent` observed; the executor returned a result. Includes the call ID and name, and `tool_failed=True` when an error result was returned. Arguments are empty; no result/error payload is included. This does not prove a remote side effect succeeded. |
| `waiting_for_approval` | Available to hosts with approval gates; standalone listening does not manufacture this state. |
| `completed` | The run, generator/plugin cleanup, and inbox finalization succeeded. A raw `DoneEvent` alone does not establish completion. |
| `failed` | Provider error, execution/cleanup exception, or failure of the existing local observer. Tool error results alone need not fail a run. |
| `cancelled` | The activation was interrupted by cancellation. Requested tools may have unknown outcomes; no replay is implied. |

Standalone listening supplies a unique `channel-run-<uuid>` run ID and
`channel-inbox:<row-id>` activation ID (scoped to the inbox database/session),
plus the ingress message ID. Each activation's notices use only its incoming
connector, conversation, and thread. Explicit cross-channel tool sends do not
change that notice route. All attached conversations still share one serialized
agent session. Older inbox envelopes without routing information execute without
notices rather than guessing a destination.

The existing `ChannelEvent` / `Agent.listen(on_event=...)` API is still the local
raw-event observer; it is distinct from the connector hook, including its
existing exception semantics. Final assistant text is never automatically sent
through either the new hook or channel tools.

## Privacy and delivery

Events have no raw-event, assistant-text, reasoning, prompt, tool-result, or
exception-text field. Construction sanitizes arguments; dispatch sanitizes a
detached copy again to protect against mutable dictionaries.

**String display contract:** ordinary string values of at most **80 Unicode
characters** remain visible in full when they are printable, single-line, and
free of recognized credential patterns. For example, a `read_file` request with
`{"path": "README.md", "start_line": 20}` retains both arguments, allowing the
connector to display `path=README.md, start_line=20`. Short names, queries and
commands such as `python -m pytest -q` are also preserved.

Sensitive-key subtrees are redacted, even when their contents appear ordinary.
The filter also redacts common service/API token prefixes, JWTs, Bearer/Basic/
Digest authentication, URL userinfo, credential assignments and command flags,
including embedded and common percent-encoded forms. Keys are screened too.
Every candidate that can be displayed is checked in full, including up to two
percent-decoding passes; controls and encoded line breaks are rejected. Long
strings are omitted outright as `[redacted]`, without scanning or retaining a
prefix. **No string value is ever prefix-truncated**, so a secret cannot become
a seemingly ordinary displayed prefix through length truncation.

This is **conservative display filtering, not guaranteed detection of arbitrary
secrets under misleading keys**. The core has no application credential state.
Before constructing an event, a host must check full original arguments
(including keys and values) against its saved-credential guard, and redact or
omit matches. In the web host this is its `CredentialGuard`. A connector's own
formatter must additionally check its own credentials before rendering. These
host checks must use full originals, not shortened or already-filtered summaries;
connector checks apply to the received values before any formatter shortening
or interpolation.

Short identifier keys, finite numbers, booleans and null may remain. Summaries
retain their depth-4, 64-node, 16-input-entries-per-container, and 4096-JSON-byte
limits. Invalid objects are redacted without calling their string representation.
Treat summaries as display data, never executable tool inputs. Argument names,
values and correlation IDs are untrusted display data; a connector must escape
them for its transport.

`await dispatch_channel_execution_event(channel, event, *, timeout=2.0)` attempts
the optional hook once. Exceptions, timeout, and connector self-cancellation are
isolated and their text is not logged or forwarded. It does not retry uncertain
notice sends. Owner cancellation waits for the bounded attempt to finish and
then propagates, even under repeated cancellation. Dispatches are joined, with
no background queue or orphaned task. Connector hooks must cooperate with asyncio
cancellation and keep their transport I/O bounded. The helper skips invalid
routes, but does not establish authorization or durable session ownership.

## Web host and connector integration

1. The host resolves and verifies the **durable session owner**, including its
   connector, conversation, and thread, before dispatch. Never infer ownership
   from a tool's outbound destination, a last-active chat, or an attached-channel
   broadcast. Supply the host's durable activation/run/call IDs when available.
2. Check full original arguments against the host's saved credentials, then
   construct `ChannelExecutionEvent` using the mapping above. Emit approval state
   only at a real host approval gate. Emit terminal state after owned execution
   cleanup; do not equate a requested tool with successful execution.
3. Await `dispatch_channel_execution_event(channel, event)` in the owned run or
   cleanup task, and join dispatches before closing the connector. If a host owns
   a queue, it also owns its ordering, bounds, ownership rechecks, and shutdown.
4. A connector overrides `on_event` to render concise status text or edits using
   `event.destination` and `event.thread_id`. Keep its existing actions and typing
   implementation. Check its own credentials, make at most one transport attempt, escape display data, and
   avoid forwarding raw model content. Existing connectors can keep the no-op
   until their next release.
