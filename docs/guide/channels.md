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

Incoming messages carry the channel name, source conversation, sender, event ID,
thread/reply information, event type, attachment references, and structured
metadata. They enter as labeled external **user-role data**. Connectors cannot
select a developer/system role through the envelope.

The inbound `reply_to` identifies the originating transport message suitable for
an outgoing reply. It can differ from `message_id`, which identifies an ingress
event for deduplication (for example, a Telegram update ID).

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
