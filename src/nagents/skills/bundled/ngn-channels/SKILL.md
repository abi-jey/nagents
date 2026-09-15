---
name: ngn-channels
description: Implement or configure Nagents channels and Telegram, shared Agent listeners, permanent web chat ownership, approved self-configuration, same-chat messaging, typing/execution notices, and durable recovery. Use for ngn external messaging integrations.
---

# Nagents channels and connector plugins

First identify the host: a standalone `Agent.listen()` application or `ngn serve`.
Then inspect the installed connector descriptor, stable connection name, and
non-secret status. This skill contains its required guidance inline; repository
paths and public links are verification sources, not automatically loaded files.

## Pick the identity and routing model

| Host | Incoming conversation identity | Outbound behavior |
| --- | --- | --- |
| `Agent.add_channel(...); await agent.listen(session_id=...)` | Every attached channel/chat feeds the one explicit persistent Agent session. | Model explicitly chooses channel and destination through tools. |
| `ngn serve` channel host | Each `(connection ID, conversation_id)` gets fresh roots with permanent ownership. Commands switch only among that chat's roots. | Owned-root sends/actions are confined to its connection and destination, including web follow-ups. |

Source chat/thread IDs describe the transport; they do not universally create
Agent sessions. Threads remain reply-routing metadata, not separate web sessions.
Use independent Agent instances and session identities when standalone contexts
must be separated. Web roots cannot transfer between chats or connections.

Final assistant text is local unless the model calls an outbound tool.
`Agent.listen(on_event=...)` is a local raw-event observer; the separate connector
`Channel.on_event` hook can render compact status notices, not raw model content.

## Standalone listener with Telegram

Install compatible core and connector versions in the application's virtual
environment. The Channel API is in the 0.6 prerelease series; enable prereleases
when selecting an alpha, or pin the tested published versions:

```bash
python -m pip install --pre nagents nagents-channel-telegram-bot
```

In an async application with a configured `agent`, attach a connector before
listening. Supply the referenced environment variables to that process:

```python
import os
from nagents.channels import load_channel

channel = load_channel(
    "telegram-bot",
    {
        "name": "telegram",
        "token_env": "TELEGRAM_BOT_TOKEN",
        "allowed_chat_ids": [os.environ["TELEGRAM_ALLOWED_CHAT_ID"]],
    },
)
agent.add_channel(channel)
try:
    await agent.listen(session_id="project-assistant")
finally:
    await agent.close()
```

`add_channel()` is synchronous, chainable, and performs no connection I/O.
Attach uniquely named connectors before listening. `listen()` opens them and
owns producers plus a serial execution worker. Reuse the database, session ID,
bot identity, and stable connector names across restarts.

This API belongs to `Agent` (or `harness.agent` when deliberately composing a
custom host). Calling it does not install a connection in an existing `ngn serve`
instance. A listener owns its Agent: independent runs and session mutations are
rejected until shutdown. Cancel the listener from its owning task or close the
Agent from another task; do not call close from its execution observer.

## Inbound envelopes and model tools

`ChannelMessage` requires `message_id`, `conversation_id`, and `sender_id`;
optional fields are `text`, `thread_id`, `reply_to`, `event_type`, `attachments`,
and JSON-compatible `metadata`. Envelopes enter as labeled external user-role
data. Neither a connector nor its message text chooses system/developer authority.

For Telegram, `message_id` is the update ID used for deduplication; `reply_to`
is the actual originating Telegram message ID. Do not use an update ID to reply,
edit, or delete. `metadata.in_reply_to`, when present, describes what the source
message replied to, not the target for replying to that source message.

| Tool | Use |
| --- | --- |
| `channel_list` | Inspect attached instances, capabilities, and action schemas before choosing an operation. |
| `channel_send` | Send text to an explicit `channel` and `destination`, optionally preserving `thread_id` and `reply_to`. |
| `channel_action` | Call an advertised connector-specific action with explicit arguments. |

Tools use the normal registry/executor/plugin hooks. Existing names are not
overwritten; listener-owned tools/instructions are removed on cleanup. Loading
this skill alone installs no connector.

`ChannelSend` describes destination/text and optional thread/reply/attachments/
metadata. Successful `send()` returns `ChannelDelivery.message_ids`, a tuple of
confirmed transport IDs. `ChannelAction` advertises a name, description, and
JSON Schema; validate operation arguments inside the connector.

For the Telegram connector, current advertised actions are `edit_message`
(`destination`, `message_id`, `text`) and `delete_message` (`destination`,
`message_id`). Read the live schema for the installed version. It sends plain
text with no parse mode, rejects overlength text rather than auto-splitting, and
does not support outbound attachments or arbitrary send metadata. Destination
IDs are canonical nonzero decimal strings; reply/thread/message IDs are positive
decimal strings. Never silently retry without the requested reply/thread target.

Incoming `ChannelAttachment` values are connector-owned references, with media
type, filename, and size metadata. Telegram file references are not automatically
downloaded, transcribed, executed, or converted to model media bytes.

## Install and enable in the web host

Open **Channels**, or use `channel_configuration(operation="discover")` from an
unassigned web/admin root. Discovery returns installed plugin schemas, private
field names, saved connection summaries, `revision`, and host-controlled
`plugin_path`. Host discovery is authoritative; a separate Python process may
not have the plugin directory on its import path.

In a host environment already supplying compatible Nagents and aiohttp:

```bash
python -m pip install --pre --no-cache-dir --no-deps \
  --target "$NGN_CHANNEL_PLUGIN_PATH" nagents-channel-telegram-bot
```

The operator configures `NGN_CHANNEL_PLUGIN_PATH`. It is not an inbound chat value. An agent runs this
through its existing approved shell tool. `--no-deps` assumes the host supplies
compatible dependencies; another connector may have additional requirements.
Persist the target with the state volume so installed packages survive image
replacement. Package installation alone does not enable a connector.

Use the Channels panel's **Refresh** after installing a new package, then select
the plugin, configure the connection, choose its main session, and enable it.
New entry points can be discovered in-process; upgrades of already imported
modules or dependencies may require restart. This connector refresh is distinct
from skill discovery, which happens automatically at Agent boundaries.

### Approved self-configuration

`channel_configure(connection_id, configuration)` queues one approved change.
Only these seven fields are accepted: `revision`, `plugin`, `enabled`,
`auto_reply`, `config`, `secrets`, `main_session_id`. Use the discovered revision
and schema. Public `config` replaces public fields; omitted `secrets` preserve
saved values and empty strings clear them. Empty main preserves the old main or
uses the active run root for a new connection.

Both management tools require an unassigned web/admin execution root. Chat-owned
web follow-ups, channel ingress, and wakeups cannot configure connections.
`QUEUED` is process-local, not saved/running. Apply occurs only after the originating
turn succeeds and the host is idle; cancellation, failure, or restart discards it.
Competing revisions fail without retry. On a later eligible turn, call
`channel_configuration(operation="status")` for revision and request summaries.
`APPLIED` means saved: inspect connector status separately for an open failure.

`auto_reply: true` permits independent `channel_send`/`channel_list` from genuine
executions of permanently owned sessions, including web follow-ups and scheduled
work after `/new`. Destination stays that session's own chat. It does not auto-send
final text or waive shell, edits, configuration, or action approval. Omitting the
field preserves the same connector's policy; new/replacement connections default false.

### Telegram setup and configuration

1. Create a bot with Telegram's official `@BotFather` and `/newbot`.
2. Supply its credential to the backend environment as `TELEGRAM_BOT_TOKEN`.
   Use `token_env = "TELEGRAM_BOT_TOKEN"` in non-secret configuration. For a
   direct token, prefer the private UI field. If the owner explicitly supplies
   one in an unassigned admin chat for setup, pass it through `secrets.token` in
   the approved configuration tool. Normal model/transcript/approval surfaces
   still contain supplied credentials; they are not scrubbed. Keep them out of
   source, examples, and public `config`. Use either `token` or `token_env`, not both.
3. Start a conversation with the bot or add it to the intended group with the
   required permissions. Group privacy mode controls incoming visibility.
   Bots cannot initiate arbitrary private conversations.
4. Run exactly one polling consumer for a bot token. Polling conflicts with an
   existing webhook. Removing a previous webhook is an explicit operator action;
   the connector does not delete it or discard pending updates at startup.
5. Configure admitted chats and enable the connection. Obtain the intended IDs
   from a controlled inbound envelope; never use example IDs as real destinations.

The `telegram-bot` entry point is
`nagents_channel_telegram_bot:plugin`, a `ChannelPlugin` whose factory is
`TelegramBot.from_config`. Its non-secret configuration keys are:

| Key | Meaning |
| --- | --- |
| `token_env` | Environment-variable name; defaults to `TELEGRAM_BOT_TOKEN` if neither credential key is supplied. |
| `name` | Stable connector instance name; the web host injects the connection ID. |
| `allowed_chat_ids` | Chat ID allowlist; inspect its interaction with other admission filters in the installed schema. |
| `allowed_usernames`, `allowed_user_ids`, `private_chats_only` | Telegram plugin 0.1.0a2 supports user/private-chat admission filters. Use owner-supplied values, never invented identities. |
| `execution_notifications` | Notification-enabled builds may advertise this boolean. Set it only if present in the installed descriptor. |
| `poll_timeout` | Integer 1–50 seconds, default 30. |

`token` is the alternative private `secrets` field. Saved credential values are never
returned by management responses; a configured indicator preserves an existing
secret. Unknown keys and incorrect types are rejected by the factory. The
connector authenticates on `open()` with `getMe`, not during import/construction.

### Telegram commands in the web host

| Input | Host action |
| --- | --- |
| `/sessions` | List only this chat's owned active roots, never unowned admin or foreign roots. |
| `/session` | Report this chat's binding. |
| `/session <session-id>` | Attach to a session already owned by this chat. |
| `/session main` | Attach to configured main only if already owned by this chat. |
| `/session default` | Return to the chat's default session. |
| `/session default <session-id>` | Set an already-owned default without changing current attachment. |
| `/new <title>` | Create and attach a fresh owned root; keep old ownership. |

Recognized commands are handled by the host with an acknowledgement to the
originating chat. Pending inputs keep the session target assigned at admission
even if a later command changes the binding. Connector parsing alone does not
change standalone `Agent.listen()` sessions. A slash command is host-specific;
it is not a universal Agent Skills activation format. In ordinary incoming text,
`$ngn-channels` explicitly loads this skill when the Agent has skill discovery.

Ownership survives disable/delete/readd, restart, Trash/Restore and permanent
purge. Migration derives it from bindings and accepted inbox provenance; mixed
roots are quarantined. Ordinary input to an unsafe binding gets a fresh root and recovery
notice, not old history or execution of that input. Resend after the notice.
Foreign, unowned and missing command targets receive identical rejection.
Owned-root actions must advertise a required string `destination`; destination-less
actions are denied. Trusted connectors must honor it. Browser selection is never
outbound authority; the active execution root is.

Current and default bindings are separate references. To move both away from a
session before deletion, use `/session default <session-id>` and
`/session <session-id>` with a same-chat replacement (create one with `/new`). Changing only the
current binding leaves the old default referenced; these commands do not change
the connection's configured main session.

These routing guards apply to one-click Move to Trash as well as Delete forever.
Move the configured main session in Channels and let queued messages finish;
deletion never detaches bindings implicitly. Undo/Trash Restore returns the same
session ID/history before its frozen retention deadline (default 30 days).
Trashed roots cannot receive new input or be selected by channel commands.
Delete forever requires confirmation; neither deletion nor restoration replays work.

## Implement an installable connector

Subclass `nagents.channels.Channel`. The required operations are
`async listen(receive: ChannelReceiver) -> None` and
`async send(message: ChannelSend) -> ChannelDelivery`.

- Set a stable `name`, useful `description`, supported `capabilities`, and
  optional `actions: tuple[ChannelAction, ...]`.
- `async open() -> None` initializes transport resources. Constructors and
  configuration factories defer connection I/O.
- For each source event, await `receive(ChannelMessage(...))`. Acknowledge
  transport admission only after it returns successfully; a failure leaves the
  event unacknowledged. Avoid an unowned detached intake queue.
- `async action(name, arguments)` implements advertised operations and rejects
  unsupported ones explicitly. Return JSON-compatible results.
- `command(message)` optionally returns `ChannelCommand(name, arguments)`
  synchronously without I/O. The host owns command policy, storage, and replies.
- `approval(message)` optionally returns `ChannelApproval(conversation_id,
  session_id, run_id, call_id, allow)` synchronously without I/O when it
  recognizes a tap on a prompt it rendered; advertise `approvals`. A returned
  value claims the interaction, so it is never model input. Return empty
  correlation fields for a recognized-but-stale tap; the host validates every
  field against its live pending approval and the owning-chat `chat_approvals`
  policy before applying it.
- `async activity(event: ChannelActivity) -> None` optionally manages typing or
  similar indicators. `conversation_id`, `thread_id`, `session_id`, and `active`
  identify the target and owner; an old stop must not stop a newer owner's work.
- `async on_event(event: ChannelExecutionEvent) -> None` optionally renders compact
  execution notices. Phases: `run_started`, `tool_requested`, `tool_completed`,
  `waiting_for_approval`, `completed`, `failed`, `cancelled`. Only bounded sanitized
  argument summaries/status are supplied, never raw reasoning, prompts or results.
- `async close() -> None` releases resources even after partial open failure and
  joins connector-owned keepalive work. The host cancels and awaits its producer
  task before closing transport resources.

A factory takes `dict[str, ChannelValue]` and returns a `Channel`. Publish it as:

```toml
[project.entry-points."nagents.channels"]
example = "my_connector:from_config"
```

For generated forms, wrap the factory in `ChannelPlugin(name, description,
config_schema, factory)` and expose that callable as the entry point. Mark private
fields `writeOnly: true`. Factories are trusted Python; credentials do not belong
in descriptors/public config, and transport dependencies stay in the connector.

### Activity is not delivery

The web host routes typing and notices by permanent owner plus live root membership,
not current/default attachment. Historical owned sessions still report web/scheduled
execution after `/new`; conflicted/trashed roots and disconnected transports do not.
Standalone notices follow the originating ingress; its Agent identity stays shared.
Telegram manages
best-effort `sendChatAction(action="typing")` keepalives, stopping local work on
completion/cancellation and letting Telegram's icon expire. This sends no text
and does not prove a reply succeeded. Connector close must join keepalives, and
rate limits must delay subsequent indicator attempts rather than retry early.

## Durable admission, failure, and recovery

- Core inbox admission is persisted before the receive callback returns.
  Deduplication uses `(session_id, channel_name, message_id)`; defaults allow
  1,000 pending entries and apply backpressure at capacity.
- Queued input survives restart. Failed/interrupted execution is retained but
  is not automatically replayed, because a tool may already have acted. An
  accepted source event is not proof of processing or delivery.
- Restore the same connector name before resuming queued input for it. The core
  listener rejects startup when a queued event's connector is absent, keeping
  that input queued. Deduplication records require a retention policy.
- The web host additionally persists chat bindings and admitted session targets.
  Reopening a browser or replaying subscription observations never reruns a tool.
- Raise sanitized `ChannelError`; use `retry_after` for rate limits and
  `outcome_unknown=True` when a remote send may have succeeded. Never expose
  Telegram's credential-bearing request URLs or raw HTTP exceptions.
- Receive operations may implement bounded reconnect/retry. Channel sends,
  actions, and whole Agent turns are not implicitly retried by the runtime.
  The provider retains its own configured HTTP retry policy. Inspect remote
  state before deciding whether to repeat an uncertain outbound operation.
- A connector failure stops the standalone listener and triggers cleanup. Use
  one process per session database; this is not distributed exactly-once work.
- Persisted inboxes/sessions differ from process-local subagent handles and
  scheduled wakeups. Timers disappear on restart and are not durable delivery jobs.

When diagnosing: check installed versions and descriptors, enabled status,
non-secret credential availability, polling/webhook conflicts, admitted chat
filters, binding/session identity, actual tool results, and retained inbox state.
Report accepted, executed, and remotely confirmed outcomes separately.

## Verification sources

- Core contracts: `src/nagents/channels/types.py`, `plugins.py`, `runtime.py`,
  `store.py`; attachment/listener ownership in `src/nagents/agent.py`.
- Web host: `src/nagents/web/channel_host.py`, `routing.py`,
  `channel_activity.py`, `channel_privacy.py`, and `catalog.py`.
- [Channels guide](https://abi-jey.github.io/nagents/guide/channels/) and
  [web guide](https://abi-jey.github.io/nagents/guide/ngn-web/).
- [Configuration tools](https://abi-jey.github.io/nagents/guide/channel-configuration/)
  and [execution hook API](https://abi-jey.github.io/nagents/api/channel-execution-events/).
- [Telegram connector implementation and README](https://github.com/abi-jey/nagents-channel-telegram-bot/tree/feature/telegram-channel).
- Official Telegram [Bot API](https://core.telegram.org/bots/api),
  [polling acknowledgement](https://core.telegram.org/bots/api#getupdates),
  [sendMessage](https://core.telegram.org/bots/api#sendmessage), and
  [typing activity](https://core.telegram.org/bots/api#sendchataction).
