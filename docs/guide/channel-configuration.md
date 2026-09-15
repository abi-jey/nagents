# Configure Channels From Chat

In the [local web client](ngn-web.md), ask the agent to discover installed
connectors, configure one using credentials you supply in chat, and enable it.
The agent uses approved tools and applies the change when its current turn has
finished successfully. Manual entry in the Channels form is also available.

## Discover And Install

`channel_configuration(operation="discover")` returns:

- Installed plugin IDs, versions, configuration schemas, and private field names.
- Saved connections with public configuration and configured-secret field names.
- The current configuration `revision` and server-controlled `plugin_path`.

Use this host discovery to determine whether a connector is installed. The running
host adds its plugin directory to its own `sys.path`; a separate Python process
using `importlib.metadata` may not see the same packages.

For a missing connector, use the returned `plugin_path` as the installation target
through the normal approved shell tool, then call discovery again. Installation
alone does not enable a connection. Newly installed entry points can be discovered
without restarting; upgrading already imported modules may require a restart.
See [dynamic connector installation](ngn-web.md#dynamically-installed-connector-packages)
and [connector packages and schemas](channels.md#write-an-installable-connector).

## Queue A Configuration

`channel_configure(connection_id, configuration)` accepts a stable connection ID
and a strict JSON object:

| Field | Meaning |
| --- | --- |
| `revision` | Required revision returned by discovery. |
| `plugin` | Required installed plugin ID from discovery. |
| `enabled` | Required boolean controlling whether the connector runs. |
| `auto_reply` | Optional boolean enabling automatic communication within the owning chat. |
| `chat_approvals` | Optional boolean allowing the owning chat to decide tool approvals without a live browser subscriber. |
| `config` | Public plugin fields, replacing the previous public configuration; defaults to `{}`. |
| `secrets` | Private plugin fields; omitted values preserve existing secrets for the same plugin. An empty string clears a secret. |
| `main_session_id` | Existing workspace root. Empty/omitted preserves the connection's main, or uses the active run's root for a new connection. |

Booleans must be JSON `true` or `false`, not strings or numbers. Unknown fields,
non-finite numbers, oversized JSON, and fields inconsistent with the installed
plugin schema are rejected. The connection ID supplies the server-controlled
instance name; do not put `name` in plugin configuration.

These management tools require a trusted, unassigned web/admin root and normal
tool approval. Channel-origin turns, chat-owned browser follow-ups, and background
wakeups cannot configure connections. Identity comes from the active run, even
if someone selects another conversation in the browser. Remote session commands
cannot adopt an unowned admin root; see [chat ownership and routing](ngn-web.md#channels-telegram-chats-and-session-binding).

## Idle Apply, Revisions, And Status

Only one configuration can be queued per run, with at most one pending across the
host. **`QUEUED` means process-local and not yet saved or running.** The current
turn must finish successfully before the worker applies it at a safe idle boundary.
The tool does not replace a running connector or its instructions during the turn.

The original revision is checked again during apply. If a competing edit wins,
the request fails with `STALE_REVISION` rather than overwriting it. Discover again
and submit a new approved request. There is no automatic retry.

Cancelling the originating run, run failure, or shutdown discards its not-yet-applied
request. A server restart does not restore the in-memory queue. An apply already
started after successful completion is joined during shutdown; saved configuration
is not rolled back.

On a later eligible turn, use `channel_configuration(operation="status")` to read
up to 32 process-local request summaries:

| Status | Meaning |
| --- | --- |
| `QUEUED` | Waiting for successful turn completion and idle apply. |
| `APPLIED` | Configuration was saved. Check `connector_status` for `running`, `disabled`, or `error`. |
| `FAILED` | Apply or originating-run validation failed. A fixed `reason` and `saved` flag describe the outcome. |
| `CANCELLED` | Pending configuration was discarded. |

A connector can fail to open after persistence: `APPLIED` with `saved: true` and
`connector_status: error` does **not** mean it is running. Discovery shows current
saved connections; request status is process-local history.

## Automatic Communication

Set `auto_reply: true` to let the agent communicate independently within the
connection's durable owning chat, including [scheduled work](ngn-web.md#scheduled-wakeups)
in owned sessions. Routing follows permanent chat ownership, not the browser selection or a later
attachment change. Other chats remain outside that scope, and privileged tools,
including configuration changes, retain their approval requirements.

Set `auto_reply: false` to disable that policy. Omitting it preserves the saved
policy when editing the same plugin; new or replacement connectors default to
false. `enabled` separately controls whether the connector is running.

## In-Channel Approvals

By default a `server_owned` (channel-origin) or background run needs a live
browser subscriber to answer a tool approval; with no browser connected the
approval is denied unattended. Set `chat_approvals: true` to let the session's
durable owning chat decide instead.

The host only honors a decision when every correlation field matches: an enabled
connector that advertises `approvals` and renders prompts, the permanent owning
conversation, and the run's single live pending approval
(`session`, `run`, `call`). A recognized-but-stale interaction carries empty
fields: it is consumed and never becomes model input, but it cannot approve
anything. A connector returns decisions through the optional
`Channel.approval` hook, paralleling `Channel.command`; connectors that do not
render prompts leave the default `None` and are unaffected.

Omitting `chat_approvals` preserves the saved policy when editing the same
plugin; new or replacement connectors default to false. Enabling it means any
trusted sender admitted by that connector can approve privileged tools in the
owning chat, so combine it with `allowed_user_ids`/`allowed_usernames` and
`private_chats_only` where appropriate.

New saves use channel catalog version 2. Existing version 1 catalogs load with
automatic messages disabled and are upgraded on Save, without a startup rewrite.

## Private Credentials

Place fields marked private/`writeOnly` in `secrets`, not `config`. Private values
are persisted in host-protected credential storage when apply succeeds; discovery
and status expose field names and configured indicators rather than secret values.
The complete discovery result, including `plugin_path`, and plugin results are
checked by `CredentialGuard`; unsafe data is withheld and plugin exception text is
not returned.

Credentials supplied in chat and tool arguments still pass through the normal
model, transcript, and approval surfaces. Credential protection here covers
connector configuration and returned data; it does not scrub user-authored chat.
