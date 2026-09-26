# Channels and Listening

A listening session represents **one persistent agent identity**. Telegram chats,
GitHub issue events, webhooks, and other sources can all feed that identity's
shared conversation. Source conversation IDs do not create separate sessions.

The model decides whether to act and which channel/destination to contact.
Final assistant text is retained and observable locally; it is not automatically
sent back or broadcast. Connector protocol acknowledgements, such as answering
a Telegram callback query, are separate from model-authored responses.

## Attach connectors

The channel API is introduced in the 0.6 prerelease series. Install a testing
release explicitly while the feature is under review:

```bash
python -m pip install --pre nagents nagents-channel-telegram-bot
```

```python
import asyncio
import os
from pathlib import Path

from nagents import Agent, Provider, ProviderType, SessionManager
from nagents_channel_telegram_bot import TelegramBot


async def main() -> None:
    agent = Agent(
        Provider(
            ProviderType.OPENAI_COMPATIBLE,
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ["OPENAI_MODEL"],
        ),
        SessionManager(Path("agent.db")),
        system_prompt=(
            "You coordinate my projects across attached channels. Decide when "
            "a message needs a response and use channel tools to send it."
        ),
    )
    agent.add_channel(TelegramBot(token=os.environ["TELEGRAM_BOT_TOKEN"]))
    # Chain more installed connectors with distinct instance names here.
    try:
        await agent.listen(session_id="my-assistant")
    finally:
        await agent.close()


asyncio.run(main())
```

Reuse the database, session ID, and connector names on restart. This restores
the shared transcript and queued input. Each connector installation is explicit;
installing a package alone never imports, connects, or activates it.

All connected sources intentionally share the agent's context. Use separate
Agent instances and sessions when identities should be independent.

## Inbound context and outbound tools

Incoming messages enter as labeled external **user-role data**. Connectors cannot
select a developer/system role through the envelope, and the raw JSON envelope is
never placed in model context. Each event is rendered as a single trusted header
line followed by the message text:

```text
[2026-09-14T23:21:13Z] telegram · private · Abbas Jafari (@realabja) · user 202247508 · chat 202247508 · message 168181692 · reply 4222 · text+image

look at this
```

The header carries the ISO timestamp, channel, conversation type, sender
name/username, sender and chat IDs, ingress message ID (deduplication), the
transport reply ID, an optional thread ID, and a message kind computed from the
text and attachments (`text`, `image`, `text+image`, `text+document`, …). The
inbound reply ID is the transport message ID to pass to `channel_send`;
`message_id` identifies the ingress event for deduplication (for example, a
Telegram update ID) and must never be substituted for it.

### Attachments

Connectors may advertise `fetch_attachment`. When they do, the runtime downloads
each referenced file once per run, subject to fixed caps — at most 3 inline
attachments, at most 8 MiB each, and an allowlist of `image/jpeg`, `image/png`,
`image/gif`, `image/webp` and `application/pdf`. Allowed files become native
provider content parts (`ImageContent`/`DocumentContent`) next to the text part,
so vision- and document-capable models receive the bytes rather than a reference.
Everything else — an unsupported type, a failed fetch, an oversized file, or a
connector without the capability — degrades to a bounded text note such as
`[attachment 1: voice.ogg · audio/ogg · 12.3 KiB · unsupported for model input]`.
Downloaded bytes are untrusted data, never instructions.

While listening, the runtime provides:

| Tool | Purpose |
| --- | --- |
| `channel_list` | Discover attached instances, capabilities, and action schemas. |
| `channel_send` | Send text to an explicit channel and destination, optionally a thread or reply target. |
| `channel_action` | Invoke an advertised connector-specific operation, such as editing a message. |

### Outbound attachments

`channel_send` accepts `attachments`: up to three workspace-relative file paths.
The runtime resolves each path inside the workspace (no absolute paths, `..`,
symlinks, or directories), reads at most 20 MiB per file and 30 MiB total, infers
the media type from the extension, and hands the bytes to the connector as
`ChannelFile` values. The text may be empty when files are present.

The connector decides formatting and may advertise `send_files`: the Telegram
connector uploads JPEG/PNG as photos (`sendPhoto`) and everything else as
documents (`sendDocument`), using the text as the caption when it fits Telegram's
1024-unit limit and sending a separate message otherwise. A connector that does
not implement outbound files raises a sanitized `ChannelError`, so a model never
believes an attachment was delivered when it was not. Attachments are explicit,
approval-covered tool arguments; nothing is read or uploaded silently.

These use the normal Agent tool registry/executor and plugin hooks. Existing
tool names are not overwritten. Listener-owned tools and its request-local
instructions are removed during cleanup.

`on_event` observes model execution without sending anything externally:

```python
from nagents import ChannelEvent
from nagents import TextChunkEvent


async def observe(item: ChannelEvent) -> None:
    if isinstance(item.event, TextChunkEvent):
        print(item.event.chunk, end="", flush=True)


# await agent.listen("my-assistant", on_event=observe)
```

## Delivery and lifecycle

### Directional content capabilities

Connectors can optionally declare the content they receive, send and render.
These are separate directions: a browser can play a video without the model
being able to understand it, and a host can admit input without using a connector
listener.

```python
from nagents.channels import (
    ChannelContentCapabilities,
    ChannelReceiveCapabilities,
    ChannelRenderCapabilities,
    ChannelSendCapabilities,
)

# Assign this to a connector's content_capabilities attribute.
content_capabilities = ChannelContentCapabilities(
    receive=ChannelReceiveCapabilities(via="listen", text=True),
    send=ChannelSendCapabilities(
        text=True,
        file_media_types=("image/png", "image/jpeg"),
        max_files=2,
        max_file_bytes=8 * 1024 * 1024,
    ),
    render=ChannelRenderCapabilities(text=True, file_media_types=("image/png", "image/jpeg")),
)
```

`channel_list` includes `content_capabilities` only when the connector declares it.
Legacy entries keep their existing shape and behavior. An omitted descriptor or
direction means **undeclared**; an explicit direction with `text=False` and an
empty file-type tuple supports neither text nor files.

- `text` describes the message text field, not `.txt` attachments.
- `file_media_types` is an explicit MIME allowlist, normalized to lowercase.
  Wildcards, parameters and duplicate types are rejected at binding.
- Receive declarations use `via="listen"` or `via="host"`; the latter does not
  advertise standalone listener support.
- Optional receive/send limits must be positive integers. Unspecified limits do
  not override the host's existing file caps.
- Render declarations can use `playback="browser_dependent"` to indicate that
  decoding depends on the recipient browser. The default is `"none"`.
- Each direction permits up to 32 MIME types of at most 127 characters each;
  the serialized descriptor is limited to 8 KiB.

Binding validates and snapshots the descriptor. Rebuild the dispatcher when
capabilities change; modifying a returned catalog does not change dispatch.
Declared send restrictions are checked before calling the connector, including
actual file sizes. A rejected mixed text/file send delivers neither part.
Receive/render declarations do not become send permissions. Tool approval,
profiles, destination ownership and provider limits remain independent.

### Host-managed dispatch

Applications that already own execution can use `ChannelDispatcher` without
starting a channel listener or creating another Agent/session owner:

```python
from nagents.channels import ChannelDispatcher

# connector and workspace are supplied by the owning application.
dispatcher = ChannelDispatcher((connector,), workspace=workspace)
catalog = dispatcher.catalog()

# Call only after the host's normal authorization/approval checks.
delivery = await dispatcher.channel_send(
    connector.name,
    destination="room-123",
    text="Here is the plot.",
    attachments=["plot.png"],
)
```

Construction and discovery do not open connectors, start listeners or access a
session database. The dispatcher shares validation and one-attempt send/action
semantics with `Agent.listen()`. It does not retry uncertain sends, broadcast final
answers, manage input admission, choose session destinations or grant permission.
The host owns connector open/close, approval, routing and cancellation/joining of
in-flight operations. An empty dispatcher can represent a host with no connected
transports; duplicate connector names are rejected before any dispatch.

For a host that does not need additional routing wrappers,
`with dispatcher.register_tools(harness.agent.tool_registry): ...` temporarily
registers `channel_list`, `channel_send` and `channel_action`. Initialize the
Harness and its trusted extensions first, then keep this scope open until owned
execution has finished. Registration rejects existing tool names; cleanup removes
only the definitions it installed and preserves subsequent replacements. Tool
execution still passes through normal Harness approval and profile checks.
Registration does not install a system prompt; the host supplies trusted,
request-local destination context through its normal instructions.

The web channel host uses this same dispatcher behind its existing approval,
credential-protection and chat-ownership wrappers.

### Built-in TUI text delivery

The interactive TUI hosts `builtin.tui` for explicit text delivery. Normal
assistant replies already appear in the conversation; use `channel_send` when
an explicit delivery is requested. Discover the available route and capabilities
through `channel_list`; the host supplies the current destination in its model
instructions. The destination is the executing root session, not a terminal
process or a separate chat.

An approved send produces a distinct delivery entry while retaining its compact
tool card as execution evidence. Deliveries are saved separately from model
messages and restored with the conversation. Separate sends remain separate
even when their text is identical. After compaction, retained deliveries from
outside the current context appear in a labeled earlier-context section.

`builtin.tui` accepts only the message's text field. All attachments, including
`.txt` files, are unsupported: a send containing text and an attachment fails
as a whole, without delivering the text first. Historical attachments from
compatible shared storage appear as filename/type/channel notes, without loading
or rendering their media bytes.

The TUI keeps its existing input queue, Harness execution and approval modal.
Channel tools remain subject to profiles and approval; reviewer/demo restrictions
still apply, including to discovery. Subagents gain no channel tools or sending
permission. The adapter declares host-managed text input, does not advertise
legacy `receive`, and rejects direct `listen()` calls. It starts no additional
listener and is not an external connector eligible for automatic replies.

The built-in route is owned by the running TUI. Tool registration detects
collisions and shutdown removes only its own definitions. The separate browser
route and its media playback are documented below.

### Built-in web media delivery

`ngn serve` provides a host-managed `builtin.web` route for explicit text and
media deliveries in ordinary, externally unowned conversations. Discover its
directional capabilities with `channel_list`; host instructions supply the
executing root session ID as the destination. Normal replies already appear in
the browser and are not automatically sent again.

Supported files are PNG, JPEG, GIF, WebP, MP3 (`audio/mpeg`), WAV, Ogg audio,
MP4 video, and WebM video. The dispatcher accepts workspace-relative paths, with
limits of **3 files, 20 MiB per file, and 30 MiB per delivery**. The complete send
is rejected if any attachment has an unsupported type or mismatched container.
Generic files, HTML, SVG, nonempty thread/reply references, and child sends are
unsupported. Existing profile restrictions and browser approval decisions apply.

Each delivery is saved separately from model messages, with a durable ID and an
exact assistant-row/tool-call anchor. Reconnect, restart, multiple tabs, and cold
history restore the same delivery. Compaction moves older entries into
**Deliveries from earlier context**. Saved media remains available after the
original workspace file changes or is deleted.

Use **Load preview / download** to fetch an attachment. Images and native audio/
video controls use authenticated, bounded byte reads and temporary object URLs;
tokens never appear in media URLs. Media does not autoplay. Browser codec support
varies, so a loaded file always has an explicit **Download** link. A decoder error
leaves that fallback available. Use **Unload preview** to release its memory.
The browser allows two concurrent media fetches and at most 60 MiB of reserved or
retained media per page. Session changes and unmounting cancel stale requests and
revoke object URLs.

Assets are scoped to their active root conversation. Trash revokes server access,
restore reinstates it, and permanent deletion removes saved metadata and bytes.
Externally owned chats retain their owner-only catalog and outbound destination
restrictions; acquiring external ownership does not hide earlier local deliveries.
The built-in adapter is not a configurable connector or automatic-reply target,
and its `listen()` method fails because the web host already owns input admission.

### Browser image and PDF input

In `ngn serve`, use **Attach**, paste an image, or drop files into the composer.
Images show a local draft preview; PDFs show their filename/type. Remove an
attachment before sending to discard it. Uploading or pasting only stages a draft:
**Send** (or Enter) explicitly submits it, with or without accompanying text.
Switching conversations discards that conversation's attachment drafts. Dictation
continues to produce editable text rather than an audio attachment.

The host accepts up to **3 attachments, 8 MiB each**, from JPEG, PNG, GIF, WebP,
and PDF. The available picker types are the intersection of this allowlist and
the executing provider/adapter's declared input support, including pinned designed
roots. For example, the Responses adapter accepts images but not PDFs. Unsupported
types fail before native input is sent; browser audio/video playback does not
imply model support. Offline demo and custom persistence adapters do not expose
upload admission.

Uploads use the existing same-origin token authentication, streaming byte limits,
container checks, and opaque session-scoped IDs. The server binds the submitted
text and **ordered attachment IDs** to the message UUID in the inbox transaction.
An identical retry observes the same admission; changing text or attachments with
that UUID is a conflict. Accepted work survives browser disconnection and process
restart. Provider support is checked again when the queued work begins. Missing
attachments reject the entire input rather than silently submitting partial text.

Unsubmitted uploads expire after **one hour**. Removing a draft, changing sessions,
or unmounting the composer aborts uploads, releases preview URLs, and requests
deletion of staged bytes. Expiry cleans up abandoned browsers and cancellation
races whose acknowledgement cannot be received. The server admits four concurrent
upload streams, at most three staged files per root, and 128 MiB of staged bytes
per workspace. Clearing/deleting a session also invalidates in-flight upload
generations, preventing a late completion from recreating its discarded draft.

After admission, bytes are retained for queued execution and normal model history;
draft removal cannot retract an accepted message. Trash discards staged drafts
and retains admitted history for restore. Permanent deletion or session clearing
removes the stored uploads. Browser history and event streams expose filename/type/
size metadata rather than attachment bytes, and there is no direct upload-media
serving endpoint. Attached content and filenames are untrusted user input, and
uploading never authorizes forwarding files to an external connector.

### Durable local delivery foundation

Local deliveries have a separate journal in the session database. They do not
create extra assistant messages. `DeliveryJournal` in
`nagents.session.deliveries` stores text and attachment BLOBs in one transaction,
returning a stable `DeliveryReceipt`. This is host infrastructure; it does not
automatically install terminal or browser channels, media HTTP routes or players.

The host integration combines:

1. An explicitly bound, approved `channel_send` definition in the real Harness
   executor. The private execution bridge captures the committed assistant
   tool-call row and the call's position within it.
2. `LocalDeliveryService`, supplied with a live authority check backed by that
   bridge. It validates the exact channel and destination before starting storage.
3. `DeliveryJournal.prepare(origin, channel, message, capabilities)`, which
   validates and snapshots the complete payload, followed by the prepared
   operation's single-use `commit()`.

The authority is host-owned, synchronous and side-effect-free. Constructing a
`DeliveryOrigin` or inheriting a context variable does not authorize a delivery.
The bridge checks the actual task, executor, approved definition and live call.
It rejects spawned/stale invocations, other tools, unsupported persistence
adapters and child sends. Ordinary execution and external connectors do not
require this local-delivery bridge.

Origins record root and actor session IDs, host run and turn IDs, invocation and
tool identities, and the exact assistant-row/call-position anchor. A host run
can include several Agent turns. Web execution uses its existing run ID; the TUI
binds a generated ID at its owned run/continuation boundary. These scopes reset
when execution ends. Input events and tool-result rows are not anchor sources.

#### Commit and cancellation

Preparation rejects unsupported multipart sends as a whole, including mismatched
destinations and nonempty thread/reply references or metadata. It checks actual
attachment bytes and host limits even when callers bypass workspace-file loading.
The transaction rechecks active root membership and the committed assistant
anchor before writing text and every attachment together.

Each prepared operation has a preallocated `delivery_id`, an `outcome` and a
zero-or-one `receipt` tuple. Outcomes are `prepared`, `pending`, `committed`,
`failed` or `unknown`. A second `commit()` is rejected; a separate preparation
creates a new identity even for identical content.

Cancellation joins owned storage before propagating. A known successful commit
retains its receipt, including when cancellation prevents a normal tool result.
An ambiguous acknowledgement is checked using the preallocated ID, without
retrying the write; `outcome_unknown=True` is reported only when the status cannot
be established. An established rollback is a known failure.

Notification is a bounded, best-effort step after commit. Observer failure does
not erase the delivery. `receipt.notification()` contains only its ID, root,
channel and sequence; `receipt.channel_delivery()` returns a compact tool receipt
with asset references. Neither duplicates the delivery text or includes media
bytes. Presentation code can reconcile repeated notices by delivery ID and load
content separately.

#### Reading and lifecycle

- `await journal.history(root_session_id)` returns metadata and text grouped into
  `earlier` and `current` deliveries using the inclusive compaction boundary.
  Both groups preserve assistant-anchor, call-position and delivery-sequence
  ordering. Compaction copies never inherit an older call's delivery.
- `await journal.read_asset(root_session_id, delivery_id, asset_id)` reads bounded
  bytes after checking access in the same transaction. Filenames are not paths.
- Web trash retains the journal but revokes access. Restore exposes the same
  delivery and asset IDs; purge removes the rows and BLOBs.
- Library session clear/delete and web permanent deletion remove related
  deliveries and assets in the same content transaction. Switching sessions or
  clearing TUI widgets does not delete saved deliveries.
- SQLite backups include the attachment bytes. Deliveries survive restart and
  changes to the original workspace file. Purge does not promise physical database
  shrinkage or secure erasure.

Actual interface rendering, replay reconciliation, and authenticated browser asset
routes are the next adapter-layer increments.

### Standalone listener lifecycle

- `add_channel()` is synchronous and chainable; it performs no connection I/O.
- `listen()` opens connectors and owns a single serial execution worker. Messages
  arriving while the agent is busy wait in its inbox; they do not interrupt a
  model request or get inserted into an unfinished tool block.
- Admission is durable before the receiver callback returns. Deduplication uses
  `(session_id, channel_name, message_id)`. A source acknowledgement therefore
  means **accepted**, not processed or replied to.
- Pending capacity is bounded (`inbox_limit=1000` by default). Full inboxes apply
  backpressure rather than silently dropping accepted events.
- Queued entries survive restart. Failed/interrupted executions are retained but
  are not automatically replayed, because a tool may already have performed an
  external action. This is not an exactly-once remote-delivery guarantee.
- If a queued event belongs to a connector that is no longer attached, startup
  fails with that event still queued. Restore the connector's stable instance
  name before resuming; accepted input is not discarded as a failed model run.
- Deduplication records are retained in the session database. They are not a
  self-pruning log; applications must plan database retention accordingly.
- A connector failure stops the listener and closes its resources. Connectors
  may reconnect safe receive operations themselves; model/send/action failures
  are never implicitly retried by the channel runtime.
- The provider retains its configured HTTP request retry policy. The listener
  does not override it or retry an entire Agent turn after a terminal failure.
- Cancel the listener coroutine, or call `agent.close()` from another task, to
  stop. Producers and the active model iterator are awaited before connections
  close. Unclaimed queued entries remain available for the next listener.
- Do not call `close()` from the listener's execution observer. Cancel the
  listener from its owning application task instead.
- Use one process per session database. The runtime guards duplicate listeners
  within the process; it is not a distributed worker coordinator.

The listener owns its Agent until shutdown. Independent `run()`, `run_simple()`,
`compact()`, and `clear_session()` calls are rejected while listening. Direct
SessionManager writes remain a low-level API and must not race the listener.

This initial API is for the core text Agent. It does not install a connector in
an existing `ngn serve` deployment or turn the Harness's process-local wakeups
into durable channel jobs.

## Write an installable connector

Subclass `nagents.channels.Channel`:

```python
from nagents.channels import Channel, ChannelDelivery, ChannelReceiver, ChannelSend


class ExampleChannel(Channel):
    name = "example"
    description = "Example transport"

    async def open(self) -> None:
        # Open transport resources; constructors should not do network I/O.
        ...

    async def listen(self, receive: ChannelReceiver) -> None:
        # Await receive(ChannelMessage(...)) for each source event.
        # Acknowledge it only after receive returns successfully.
        ...

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        # Send once, returning identifiers confirmed by the remote service.
        raise NotImplementedError

    async def close(self) -> None:
        # Release resources, including after partially failed open().
        ...
```

Connectors can advertise `ChannelAction` schemas and implement `action()` for
integration-specific features. Validate operation arguments in the connector.
Unsupported attachments or operations must fail explicitly. Attachment
references are not automatically fetched or passed to a model as media bytes.

Raise `ChannelError` with a credential-free description. Set `retry_after` for a
remote rate limit and `outcome_unknown=True` when a send may have succeeded but
its response was lost. Never expose raw exceptions containing bot-token URLs.

For configuration-driven discovery, expose a factory taking a
`dict[str, ChannelValue]` and returning a Channel:

```toml
[project.entry-points."nagents.channels"]
example = "my_connector:from_config"
```

```python
from nagents import load_channel

connector = load_channel("example", {"name": "project-updates"})
# agent.add_channel(connector)
```

Factories and connectors are explicitly trusted Python extensions. Dependencies
belong to their separate distributions; the core channel interface adds no
transport-specific dependency to Nagents.

### Configuration Descriptors And Host Features

A factory can be wrapped in a callable `ChannelPlugin` to expose configuration
fields to management clients:

```python
from nagents import ChannelPlugin

plugin = ChannelPlugin(
    name="Example connector",
    description="Receive project updates",
    config_schema={
        "type": "object",
        "properties": {"token": {"type": "string", "writeOnly": True}},
        "required": ["token"],
    },
    factory=from_config,
)
```

Point the entry point at `my_connector:plugin` instead of the factory function.
`load_channel` accepts either form. `writeOnly` marks credential fields for a
host to store privately and redact from configuration responses.

Four optional Channel methods support richer hosts:

- `command(message)` recognizes an explicit transport command and returns a
  `ChannelCommand`, without doing I/O. The host decides which commands to handle
  and owns session routing and persistence.
- `approval(message)` recognizes a decision for a prompt the host previously
  rendered and returns a `ChannelApproval`, without doing I/O. Only connectors
  that render prompts and advertise `approvals` implement it. Returning a value
  claims the interaction, so it is never model input: the host applies a fully
  validated decision or ignores it. A recognized-but-stale interaction carries
  empty correlation fields. Hosts accept a decision only from the session's
  permanent owning chat under the connection's `chat_approvals` policy.
- `activity(event)` receives `ChannelActivity` start/stop notifications for typing
  or similar indicators. A connector must stop its keepalive tasks during close.
- `on_event(event)` receives `ChannelExecutionEvent` through an optional, default
  no-op hook. Phases are `run_started`, `tool_requested`, `tool_completed`,
  `waiting_for_approval`, `completed`, `failed`, and `cancelled`. Connectors can
  render compact status and sanitized argument summaries; raw reasoning, prompts
  and tool-result bodies are excluded. This differs from the existing local
  `Agent.listen(on_event=...)` raw-event observer. See the
  [execution-event API](../api/channel-execution-events.md) for bounds and cleanup.

The standalone `Agent.listen(session_id=...)` API keeps one shared identity. The
[web host](ngn-web.md#channels-telegram-chats-and-session-binding) adds a routing
policy: each external chat starts with a fresh session, reattaches to sessions
permanently owned by its `(connection ID, conversation ID)`, and may adopt an
otherwise unowned workspace root (such as one created in the web UI) on attach.
A root owned by another chat is never adopted. Ownership
survives detachment, connector replacement, restart, and Trash/Restore; outbound
tools, typing, and execution notices follow that owner even when the chat selects
another session with `/new`. Web follow-ups and scheduled work in historical
owned roots remain scoped to the same chat; conflicted/trashed/missing roots do
not produce indicators. No connected transport means no transport activity.
Source IDs are routing data,
not a universal instruction to create or merge core Agent sessions.

The web host's `auto_reply` opt-in permits independent same-chat sends and channel
discovery during genuine owned-session execution, including scheduled work.
Shell, edits, channel actions, and configuration retain their approval gates.
Unassigned web/admin roots alone can use approved `channel_configuration`
discovery/status and `channel_configure` tools; the seven-field configuration is
applied only after a successful originating turn, at idle. Discovery includes the
installed schema, revision and `plugin_path`. Private `secrets` can carry a token
explicitly supplied by the owner; normal model/transcript/approval surfaces remain.
See [Configure Channels From Chat](channel-configuration.md) for the full contract.
Telegram plugin 0.1.0a2 supports user-ID, username and private-chat admission
filters; inspect the installed descriptor for those fields and any
`execution_notifications` option before setting them.

## Feature-branch alpha releases

Maintainers can publish a tested core prerelease without merging a draft PR:

```bash
gh workflow run ci.yml --ref feature/channels -f publish_alpha=true
```

The workflow runs all platform tests and asset checks, stamps the build as
`0.6.0a<run-number-and-attempt>`, and publishes those artifacts using PyPI Trusted
Publishing. It neither bumps the branch nor publishes a stable release. Reruns
receive a fresh alpha number. Consumers should pin the exact tested version when
reproducibility matters. The Telegram connector repository provides its own
feature-ref alpha publication workflow.
