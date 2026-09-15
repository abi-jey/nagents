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

Three optional Channel methods support richer hosts:

- `command(message)` recognizes an explicit transport command and returns a
  `ChannelCommand`, without doing I/O. The host decides which commands to handle
  and owns session routing and persistence.
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
policy: each external chat starts with a fresh session and can reattach only to
sessions permanently owned by its `(connection ID, conversation ID)`. Ownership
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
