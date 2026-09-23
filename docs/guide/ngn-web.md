# Local Web Client

`ngn serve` serves a React client for the existing **ngn Harness**. It shares the
terminal client's workspace-scoped sessions, tools, provider configuration, and
per-call approvals. It does not use the older `nagents.server` application.

## Source Availability

This guide describes the **current source checkout**, not the published `v0.5.0`
release. That release includes the existing ngn CLI, Harness, TUI, and headless
commands, but does **not** include `ngn serve` or the `web` extra. Install this
feature from the checkout using the commands below.

## Start From Source

From the repository root, with your Python virtual environment active and
Node 20.19 or newer installed:

```bash
python -m pip install -e '.[web]'
ngn serve --dev --demo --workspace /path/to/project
```

For Poetry, use `poetry install -E web` instead of the editable pip command and
launch with `poetry run ngn serve --dev --demo --workspace
/path/to/project`. Omit `--demo` when you want configured live provider requests.

Use a separate virtualenv and editable installation for each Git worktree.
Changing your working directory does not change which checkout an installed `ngn`
command imports. Startup prints the resolved UI asset directory and build ID so
you can identify the build in use.

Open **http://127.0.0.1:8765**. Use the exact host and port you started; `localhost`
and `127.0.0.1` are intentionally different origins. `--port 9876` selects another
port. The only accepted hosts are `127.0.0.1`, `::1`, and `localhost`.

Common Harness options work before or after `serve`, including `--provider`,
`--model`, `--agent`, `--config`, `--continue`, and `--resume`. API keys and saved
OAuth credentials stay on the backend. Configure model-provider credentials through
the existing CLI/environment. Connector credentials have a separate Channels form
described below. Textual is not required. The existing `server` extra also supplies
the HTTP/WebSocket dependencies, but the applications are independent.

### Your Own Connection

Model discovery uses the backend's **active provider**, not browser-supplied
credentials or a browser-selected endpoint. It never changes authentication or
billing mode. Keep an existing private deployment's ChatGPT/Codex login; enabling
catalog discovery does not require switching it to an OpenAI Platform API key.

For your own installation, choose your own connection deliberately:

- **OpenAI Platform API key:** set `OPENAI_API_KEY` securely in the backend
  environment using your own key, then run `ngn serve --provider openai --auth
  api-key --model gpt-4.1`. Platform API usage and billing are separate from a
  ChatGPT subscription. Never put key values into browser settings, URLs, or Git.
- **ChatGPT/Codex login:** run `ngn login --device-auth`, approve only the code you
  requested, and use `ngn login --status` to check the saved login. Then run
  `ngn serve --provider openai --auth chatgpt`, with the default endpoint and
  `api = "auto"`. Interactive `/login` remains available in the terminal client.
  Use your own eligible ChatGPT account; protected credential storage currently
  requires POSIX. See [device login](ngn.md#openai-device-login) for limitations.

The API-key and Codex catalogs are not interchangeable, and discovery never falls
back between them. In particular, a Codex OAuth access token is **not** an OpenAI
API key. Login/refresh remains on the backend; the browser receives only model
IDs and a non-secret source label. Administrator-managed containers use their
existing backend credential environment/store, not credentials supplied by a web
visitor. This does not add multi-user account isolation to a shared deployment.

### Frontend Build

Local source checkouts, CI packages, and Docker use the same React/TypeScript
source in `src/nagents/web-ui/` and the same `npm run build` command. The build
typechecks the frontend, runs Vite, and writes `src/nagents/web/static/build.json`
with hashes of its inputs and output assets. Generated assets and `node_modules/`
are ignored by Git.

Plain `ngn serve` verifies and serves the existing bundle. It never installs npm
dependencies, builds assets, or watches files, including in a source checkout.
Build the assets explicitly before the first normal source launch:

```bash
npm --prefix src/nagents/web-ui ci
npm --prefix src/nagents/web-ui run build
ngn serve --demo
```

For development, use `ngn serve --dev` from an editable installation. Missing or
stale assets trigger `npm ci` followed by `npm run build` in the frontend directory
adjacent to the imported Python package. This includes source edits, additions,
deletions, and lockfile changes after a Git pull. A failed build stops startup
instead of serving an old UI. Builds need Node.js and access to the locked npm
dependencies; a verified build starts without npm.

Development mode watches frontend and Python source files. Changes restart the
Python backend and rebuild stale frontend assets. Refresh the browser after the
restart to load the new UI; this is not Vite hot module replacement. Reloading
cancels active tasks, so use this mode for development. Leave off `--dev` for
normal use and deployments.

Wheels and deployed containers serve the same verified bundle, built during
packaging. They do not install npm dependencies or rebuild at startup. A missing
or damaged packaged bundle produces a reinstall error. Matching UI builds still
require matching source revisions; pulling a checkout does not update an already
deployed image. Docker defaults to the same `ngn serve` command without `--dev`,
listening on loopback port 8765. Use the [private deployment guide](ngn-web-deployment.md)
for its same-pod proxy setup. There is no separate Vite origin/proxy required.

## Try It Offline

```bash
ngn serve --demo
```

Choose **Inspect workspace** for real streamed local tool events, then **Review a
demo approval** (or send `demo approval`). Review the sample diff and choose **Deny**,
or press Escape. **Allow once** records only a demo decision; it does not write the
sample file. **Stop run** cancels an active run, including a pending approval.
Create a new session and select an older session to read/resume its conversation.

Demo responses are scripted. Demo retains the Harness restrictions: no model
requests, shell execution, workspace writes, or Python plugins. Session history is
still stored locally in the Harness data directory.

## Operation And Safety

### Workspace Navigation

The sidebar keeps **New session** above an independently scrolling session list.
**Global settings**, **Channels**, **Trash**, and the workspace information overlay stay
in the bottom section, so they remain reachable with a long conversation history. On narrow
screens, **Toggle sessions** opens a drawer; Escape or its close button dismisses
it and returns focus to the navigation toggle. Dialogs opened from the drawer
return to their controls without losing the open navigation.

### Delete A Session

Choose the sidebar row's **Move to Trash** action to remove a session in **one
click, without confirmation**. You do not need to select it first. Row actions
appear on hover or keyboard focus and remain available on touch screens. A pending
state prevents duplicate submissions. Failed admission leaves the session,
conversation, and draft visible, with an error explaining what needs attention.

Soft deletion hides the root from the active session list and prevents ordinary
resume, message admission, channel selection, history access, or subscription to
it. Its saved conversation remains available for restoration until its retention
deadline. Deleting a different row preserves the conversation, draft, and live
subscription you are viewing. If your current root is trashed, the client opens a
surviving root; the server chooses or creates a replacement atomically when needed,
including when the last active root is removed. A different server-side selection
from another browser does not replace an unaffected conversation you are viewing.

#### Undo And Restore

Use **Undo** in the deletion notice or **Restore** in **Trash**. Restoration
returns the **same session ID and saved history**, rather than copying messages
into a new conversation. It restores the row to the session list without changing
the current server/browser selection or starting a model run. Choose **Open restored session**
when you want to navigate to the restored conversation; a late restore response
must not overwrite a newer selection or draft.

Soft deletion preserves the current unsent draft, including when its selected
root is removed. Drafts remain **browser-local** and are not uploaded or stored
by the Trash API. Server-side history restoration does not recover browser-local
text on another device or after that local state is lost.

#### Retention And Automatic Cleanup

Trash retains sessions for **30 days by default**. The Trash panel's retention
control accepts an integer from **1 through 365 days** and saves a workspace-wide
policy that survives server restarts. This preference has its own API/revision,
separate from the model, provider, tool, and dictation Settings values.

Each deletion freezes a `purge_at` deadline using the policy in effect when it is
accepted. Changing retention affects **future deletions only**; it does not shorten
or extend existing deadlines. Repeating soft deletion while that root is already
in Trash returns the same item and deadline, without creating another replacement
root or extending retention.

Expired Trash is purged after startup and periodically, approximately every
30 seconds, at a safe idle boundary. Cleanup is bounded and can lag the deadline
while the application is busy. **An expired item cannot be restored even if its
rows have not yet been purged.** Cleanup does not call a provider or run tools;
active sessions are not automatically pruned by this policy.

#### Delete Forever

Use a session row's **More actions → Delete forever**, or **Delete forever** on a
Trash item, for immediate permanent deletion. This retains an irreversible-action
confirmation naming the session. Confirming submits the request; pending/error
feedback prevents duplicate submissions. Cancelling leaves the conversation
intact. Permanent deletion removes that root's saved history and offers no Undo.
It removes logical database records, not backups or SQLite free pages.

#### Activity And Routing Guards

Moving to Trash and deleting forever retain the idle, active-work, and
in-memory-process guards. Admission returns `409` while the Harness is busy, a
descendant task handle is retained, descendant tasks are still running, or the
root has pending wakeups. Finish or cancel running work first. Retained handles
can still resume child conversations; after those tasks finish, restart ngn to
expire the handles before deleting their root. Child histories are kept:
deletion never guesses ownership from a session prefix, a task name, or text in
old messages.

Channel attachments and queued inbox work never block deletion. Deletion wins:
deleting a bound root detaches that channel conversation, so its next inbound
creates a fresh chat root and is processed as ordinary input with no recovery
notice. A conversation that only used the deleted root as its default stays on
its surviving root. A connection whose **main session** was deleted is
repointed to a surviving root; the connector stays enabled. Pending inbound work
is marked terminal and its `(channel, message_id)` identity is recorded, so a
late connector redelivery cannot re-execute deleted work. Historical
session-owner identities are retained; deleting an ID never transfers its chat
ownership, and a restored root may be re-adopted with `/session ID`.

Soft deletion changes active-list membership while retaining the root's history
and related stored input. Restore atomically reinstates that membership and title.
Permanent purge removes the root's content while retaining content-free
channel/message deduplication keys and historical session-owner identities, so
late channel redelivery cannot execute deleted work or transfer a reused session
ID to another chat. Workspace files, unrelated roots and child histories, saved
settings, channel configuration, and private credentials are preserved.

Cancellation joins the SQLite transaction and selection/subscription cleanup
before releasing the idle boundary, avoiding partially applied membership changes.
Deleted-root subscribers are revoked, and clients refresh session membership;
ordinary resume/reconnect cannot restore a trashed root. Restore publishes the
updated session list but never replays a prompt, tool, or old subscription event.

A lost HTTP acknowledgement can mean the mutation committed. Refresh the active
session list and Trash before deciding what to do; do not blindly retry. Each
new soft deletion has an opaque `deletion_id`. Undo, Restore, and permanent purge
of a Trash item must supply that exact generation, so an old view cannot act on
a later deletion of the same session. See the [Trash API contract](#trash-api-contract).

### Execution And Access

- One local app instance owns one Harness on one async lifespan/event loop. Web
  messages and channel notifications queue for serialized execution. Each message
  carries an explicit target session; changing the sidebar selection does not
  reroute already accepted input. Settings and other session mutations still
  require an idle boundary. Scheduled wakeups also wait for idle execution.
- The browser subscribes to session updates over WebSocket. Navigating away or
  losing that subscription does not cancel server-owned queued work. Use **Stop
  run** to cancel an active run; server shutdown joins its owned work. Pending
  approvals fail closed, and completed actions are **not rolled back**.
- Approvals apply to an exact run, call ID, and fresh approval nonce. Only the
  active pending approval may be decided; stale, duplicate, or unknown decisions
  fail. An unanswered approval expires after five minutes and is denied.
- Partial streamed output remains visible after failure/cancellation. Subscription
  reconnects use a cursor and server-instance epoch. A gap or restart requests a
  fresh snapshot instead of replaying model/tool work. Web input has a stable
  client message ID for durable admission deduplication. Approval decisions are
  never automatically retried.
- The client displays model and tool output as escaped text, with fenced code
  shown in keyboard-scrollable code regions. Tool records retain separate inputs,
  streamed output, final result/error, call identity, and measured duration when
  provided. Nested tasks are grouped by their actual parent task IDs, not their
  names or event arrival order. Later activations retain separate execution
  evidence beneath the same task identity. Child approval decisions stay with
  that child's call, even when another agent uses the same call ID.
  Task branches, the retained registry, and notification payloads start collapsed
  to keep the conversation visible. Expand a record for its full identifiers,
  inputs, results, and errors; explicit expansion choices survive live updates.
- Missing results are never inferred to be successful. Resumed history labels
  results as recorded, since live duration/approval/status events are not persisted
  in the Harness message history. Same-process reloads also show the retained task
  tree's latest known state, not a reconstructed activation timeline. Saved
  background context is collapsed and inspectable rather than shown as a fresh
  user command. A server restart does not restore task handles from that text.
- Enter sends and Shift+Enter inserts a newline. Approval dialogs initially focus
  the review heading, show exact inputs alongside the diff, and keep Deny conspicuous.
  Tab stays within the dialog, Escape denies, and closing restores focus. Finishing
  a stream does not steal focus or scroll a reader away from earlier messages.
- No remote binding, CORS wildcard, model-provider authentication/endpoint editor,
  attachment upload into chat, arbitrary file HTTP access, or direct shell
  endpoint is provided. The separate [dictation upload](#microphone-dictation)
  produces editable text. Only built frontend assets are served. Unknown routes
  remain 404.

This is a **trusted local-user tool, not a sandbox or multi-user service**. Other
processes/users with access to your loopback interface may access it. Do not expose
the standalone server through a reverse proxy, public tunnel, port forward, or
shared remote desktop. For administrator-managed **password-free Tailscale access**,
see the [private deployment guide](ngn-web-deployment.md). That setup runs the
same `ngn serve` process on one owner-approved workload, explicitly bound off
loopback (`--host 0.0.0.0`), using HTTPS without Funnel or NodePort exposure,
and is not a general remote-access mode. Tailscale ACLs/grants and trusted cluster
networking are the access boundary: everyone who can reach the Service shares
the trusted-user workspace, sessions, and approvals, with no multi-user isolation.
ClusterIP is not public Internet exposure, but cluster clients can also reach it
and forge headers. Same-origin fetch-metadata checks and the per-process CSRF
token protect browser requests, not against malicious authorized network
clients that can obtain the bootstrap token; there is no web login prompt.

Live approved shell/custom tools can access the host with your account's rights.
Closing the web server does not remove saved conversations or undo approved edits.

HTTP requests enforce the exact Host and Origin, reject cross-site fetches, and
require a random per-process token on API reads and mutations (except the
same-origin bootstrap). Mutations require an Origin header. Ordinary mutations
require JSON and remain limited to 64 KiB; prompts are limited to 32,000 characters.
Only the exact dictation route accepts bounded WAV audio instead of JSON.
The token is held in browser
memory, never a URL or local storage. Responses are non-cacheable, frame embedding
is blocked, and no third-party scripts/fonts are loaded.

WebSocket upgrades enforce the same Host/Origin boundary and authenticate before
accepting a connection. The browser offers `ngn.events.v1` and
`ngn.token.<process-token>` as subprotocols; the server selects only the former.
Tokens never belong in a WebSocket URL. Subscriptions can read only validated root
sessions in this workspace. Subscriber queues and replay history are bounded so a
slow tab does not block model execution.

## API Outline

All paths are under `/api`. API clients first GET `bootstrap`, then send its token
in `X-Ngn-Token`. POST, PUT, and DELETE requests also need
`Origin: http://127.0.0.1:8765` (matching the configured authority) and
`Content-Type: application/json`, except for the explicit WAV transcription
contract below.

| Method / Path | Purpose |
| --- | --- |
| `GET bootstrap` | Non-secret workspace/model/profile/demo info, token, active run ID |
| `GET settings` | Committed runtime values, startup defaults, profiles, revision, persistence and safe connection status; readable during a run |
| `GET models` | Explicit fresh discovery from the active provider; `{models: string[], source: string}`; read-only, including during a run |
| `POST settings` | `{revision, values}` validates and persists the complete allowlisted settings; idle only |
| `POST settings/reset` | `{revision}` inherits current global defaults and deletes the workspace override; idle only |
| `GET settings/global` | Global defaults and their independent revision |
| `POST settings/global` | `{revision, values}` saves defaults shared by workspaces in the same data directory; idle only |
| `POST settings/global/reset` | `{revision}` removes global overrides; idle only |
| `GET sessions` | Selected session ID/history and this workspace's session list; idle only |
| `GET sessions/{session_id}/context` | Read-only estimated token breakdown of the request that session would send; safe while idle and during a run |
| `GET activity/{session_id}/{after}` | Read bounded, session-scoped wakeup/background activity after a cursor; does not start a run |
| `POST sessions/new` | `{}` creates/selects a session and returns the updated snapshot |
| `POST sessions/resume` | `{session_id}` checks workspace membership and returns its snapshot |
| `DELETE sessions/{session_id}` | `{}` or `{permanent: false}` moves an idle, unbound root to Trash; returns `Snapshot & {deleted_session_id: string, trash: TrashItem}` |
| `DELETE sessions/{session_id}` | `{permanent: true}` permanently deletes an active-list root; returns `Snapshot & {deleted_session_id: string}` |
| `GET trash` | Returns `{revision: string, retention_days: number, items: TrashItem[]}` |
| `PUT trash/settings` | `{revision: string, retention_days: number}` saves retention for future deletions; returns the same Trash snapshot shape |
| `POST trash/{session_id}/restore` | `{deletion_id: string}` restores that generation; returns `{restored_session_id: string, sessions: Session[]}` without selecting it |
| `DELETE trash/{session_id}` | `{deletion_id: string}` permanently purges that generation; returns `{purged_session_id: string}` |
| `POST messages` | `{session_id, prompt, message_id}` durably queues input; updates arrive through subscriptions |
| `WS events` | Authenticated session subscriptions, snapshots, replay cursors, and live execution records |
| `GET channels` | Installed plugin descriptors, redacted saved connections, bindings, and configuration revision |
| `POST channels/refresh` | Refresh installed connector discovery after package installation |
| `PUT channels/{id}` | Save a connection and its enabled/main-session configuration at an idle boundary |
| `DELETE channels/{id}` | Remove a configured connection using its current revision |
| `POST run` | Compatibility API: `{session_id, prompt}` starts a request-owned NDJSON response |
| `POST cancel` | `{run_id}` cancels/joins exactly that active run |
| `POST approval` | `{run_id, approval_id, call_id, decision: "allow" or "deny"}` |
| `POST dictation/transcribe` | Bounded raw `audio/wav` recording; returns `{text}` without starting a chat run |

The run stream uses the CLI's normalized `schema_version: 1` event names and
adds `run_id` to every record. Web lifecycle records are `run_started`, `heartbeat`,
`approval`, `approval_closed`, and `run_finished` (`completed`, `failed`, or
`cancelled`). `approval.id` is the Harness call ID; `approval_id` is the fresh web
nonce. A core `done` is not the transport terminator; wait for `run_finished`.
`approval_closed` includes the backend's `decision` and `expired` flag so the live
transcript can retain the actual decision without guessing from tool output.
The compatibility `POST run` stream remains request-owned and is not resumable;
closing it cancels that run. New browser input uses `POST messages` and the
WebSocket subscription instead. Subscription replay replays observations, never
executes a prompt or tool again.

### Context Statistics

The header shows a compact **Context** indicator: total estimated input tokens
against the model context window when that window is known. Expanding it lists
the per-component estimate — system prompt, tool definitions, skills, the
user/assistant/tool history split, and media/attachments — plus the total,
remaining space, and the last provider-reported input tokens when available. The
**Tool definitions** row is called out separately because tool schemas can
dominate a request.

`GET /api/sessions/{session_id}/context` backs the indicator. It is read-only,
does not select the session, run the model, or change prompt content, and it
never returns credentials. `GET /api/sessions` (which the client calls on
connect, select, and reconnect) does not include the breakdown; the indicator
refreshes it during model turns, tool calls/results, compaction, and run completion.
Streaming event bursts are coalesced, only one read is in flight at a time, and
active runs also get a periodic refresh. Navigation cancels old reads so a previous
session's totals cannot overwrite the selected conversation. A **Live** indicator
and manual refresh control are available in the breakdown.

An in-progress root reply appears as **Streaming reply (not yet saved)** until
the completed message enters history. Child-agent replies are kept separate.
Pinned channel agents use their own provider and tool configuration for these
estimates, including while idle. The numbers use the character/byte estimates described in the
[Context Statistics API](../api/context-stats.md), not tokenizer counts.

The response shape is:

```typescript
type ContextStats = {
  components: { key: string; label: string; tokens: number }[]; // stable order, sums to total_tokens
  total_tokens: number;
  context_window: number | null; // null when the model window is unknown
  remaining_tokens: number | null;
  observed_prompt_tokens: number | null;
  observed_completion_tokens: number | null;
  provider: string;
  model: string;
  estimate_method: string;
};
```

### Trash API Contract

`DELETE /api/sessions/{session_id}` now defaults to **soft deletion**. Clients
that intend immediate permanent deletion must explicitly send
`{"permanent": true}` for an active-list root. For a root already in Trash, use
`DELETE /api/trash/{session_id}` with its `deletion_id` instead. The identifier in
the URL is the session ID; the request body identifies its deletion generation.

The shared Trash wire types are:

```typescript
type TrashItem = {
  id: string;
  title: string;
  deleted_at: number;
  purge_at: number;
  deletion_id: string;
};

type TrashSnapshot = {
  revision: string;
  retention_days: number;
  items: TrashItem[];
};
```

`deleted_at` and `purge_at` are **UTC epoch seconds**, not milliseconds or formatted
date strings. `deletion_id` is opaque and renewed on each new soft deletion after
restoration. A repeated soft deletion of an already-trashed root preserves its
generation and deadline.

`Snapshot` in the table is the existing selected-session response, including
`session_id`, `sessions`, and `history` plus its existing activity/task fields.
`Session` is the existing session-list descriptor, including `id`, `title`, and
`updated_at`. The restore response intentionally returns only the restored ID
and session list; it does not contain a replacement selection or run acknowledgement.

Two independent concurrency tokens apply:

- **`revision`** protects `PUT /api/trash/settings`. Start from `GET /api/trash`,
  send the current revision and integer `retention_days` in `1..365`, and retain
  the returned snapshot. Unknown fields and invalid types/bounds are rejected;
  a stale revision returns `409`. Refetch before retrying a changed policy.
- **`deletion_id`** protects Restore and Delete forever in Trash. Supply the
  generation received with the item; stale generations return `409` instead of
  restoring or purging a later deletion of the same ID. Restoring an expired item
  still present in Trash returns `410`; an already-purged or missing item returns
  `404`. Refresh Trash rather than assuming delayed cleanup means recovery is
  still possible.

The retention policy is stored separately from `/api/settings` and its other
values. Do not add `retention_days` to that request or reuse its settings revision.
All Trash routes retain the existing Host, Origin, fetch-metadata, and token
guards. No deletion, restoration, policy update, or expiry cleanup starts model
or tool execution.

### Channels, Telegram Chats, And Session Binding

Open **Channels** to configure installed connectors. The plugin selector displays
the package's configuration schema and version. Saved secret values are never
returned to the browser; a configured indicator lets you keep an existing value
without re-entering it. Choose an existing **main session** when configuring a
connection, then enable it to start receiving events on the server.

From an **unassigned web/admin root**, the agent can also configure a connector
through approved tools. `channel_configuration(operation="discover")` returns
installed schemas, private field names, saved connection summaries, the current
`revision`, and host-controlled `plugin_path`. Use that discovery result rather
than assuming a separate Python process has the host's plugin import path.

`channel_configure(connection_id, configuration)` accepts exactly seven fields:
`revision`, `plugin`, `enabled`, `auto_reply`, `config`, `secrets`, and
`main_session_id`. The approved request is queued, then applied at idle only after
its originating turn succeeds. Failed/cancelled turns and restart discard pending
requests; stale revisions never overwrite a competing change. Use
`channel_configuration(operation="status")` on a later eligible turn to inspect
the request result. `APPLIED` means saved; connector open status may still be `error`.
Chat-owned web follow-ups, channel-origin turns, and scheduled work cannot use
these management tools. See [Configure Channels From Chat](channel-configuration.md).

Prefer environment credentials or the private Channels form. Owner-supplied tokens
in a trusted admin conversation are also supported through the configuration
tool's private `secrets` parameter. They still pass through normal model,
transcript, and approval surfaces; those surfaces are not a credential scrubber.
Do not put tokens in public plugin `config`, source, or examples.

Inspect the installed Telegram descriptor before configuring its options.
Plugin 0.1.0a2 supports `allowed_usernames`, `allowed_user_ids`, and
`private_chats_only`, alongside chat filters. Notification-enabled versions may
also advertise `execution_notifications`; older schemas reject unknown fields.

The web host creates a fresh persistent session for each new connector/chat pair;
it never starts a new chat in the configured main session. A session's first
assigned `(connection ID, conversation ID)` is permanent. Changing current/default
attachments, disconnecting, disabling, deleting/readding a connector, restarting,
or using Trash/Restore does not clear ownership. One chat can retain many sessions;
one session cannot move to another chat or connection.
Telegram messages appear as ordinary user messages with source information, and
new chat sessions appear in the sidebar. Selecting another session in the browser
does not move a Telegram chat's binding.

Telegram's host commands allow an explicit change:

| Command | Purpose |
| --- | --- |
| `/sessions` | List this chat's owned, active sessions, plus unowned workspace roots it can adopt; never another chat's roots. |
| `/session` | Report the chat's current session. |
| `/session <session-id>` | Attach to a session owned by this chat, or adopt an existing unowned root. Another chat's root is refused. |
| `/session main` | Attach to the configured main session, adopting it if it is unowned. |
| `/session default` | Return to this chat's owned default session. |
| `/session default <session-id>` | Set the default to an owned or unowned root without changing the current attachment. |
| `/new <title>` | Create and attach a new chat-owned session; old ownership remains. |
| `/compact` | Compact the chat's bound session: its history is replaced by a summary. |

Attaching an **unowned** root (typically created in the web UI) adopts it: the
chat becomes its permanent owner, exactly as `/new` does, and no other chat can
take it over afterwards. A root already owned by a different chat is never
adopted, listed, or attached. Ownership is recorded in the same transaction that
binds the conversation, before any accepted message is dispatched.

Commands are handled by the host rather than sent to a model as ordinary work.
Their acknowledgements return to the originating chat. Pending messages retain
their admitted session target when a later command changes the binding.
`/compact` is the exception: it compacts the bound session rather than answering
from a cached reply, and reports the resulting message counts back to the chat.

Unowned, foreign, hidden, and unknown session targets receive the same rejection;
commands do not disclose whether those IDs exist. Only trusted local management
can make a first assignment of an unowned root, and it cannot transfer an owner.

For a chat-owned execution root, `channel_send` is restricted to its owner's
connection and destination. `channel_action` additionally requires an advertised
string `destination` parameter marked required, with that same destination.
Destination-less actions are denied. These checks still apply to web-origin
follow-ups and survive browser selection changes; existing executor/profile and
approval checks remain. Connectors are trusted Python and must honor the declared
destination rather than reinterpret it as a different chat.

Opt in to `auto_reply: true` to permit independent messages and channel discovery
from genuine executions of owned sessions. This includes incoming channel turns,
web follow-ups, and scheduled work even after the chat selects `/new`. It still
requires the permanently owned connection/destination and a live enabled connector;
it does not waive approval for shell, edits, configuration changes, or channel
actions. The model must call a send tool: final response text is not automatically
forwarded. Omitting the option preserves an existing same-plugin policy; new or
replacement connections default to false.

Host acknowledgements and typing indicators are scoped to the owning chat, never
broadcast across roots or chats. Typing follows the executing session's permanent
owner while that root remains live, **not** the chat's current/default attachment.
An older owned root therefore still types during web follow-ups or scheduled work
after `/new`; conflicted/trashed roots and removed/disabled transports do not.
Pending work retains its admitted root; a later same-chat attachment change does
not redirect it. Unowned admin roots retain
explicit outbound tools, but cannot be adopted by remote session commands.

Connectors can implement `Channel.on_event(ChannelExecutionEvent)` for compact
start/tool/approval/terminal notices to the same permanent owner. Its phases are
`run_started`, `tool_requested`, `tool_completed`, `waiting_for_approval`,
`completed`, `failed`, and `cancelled`. Tool notices contain bounded sanitized
argument summaries and status, not raw reasoning, prompts, or tool-result bodies.
This is separate from model-authored messages and from typing; inspect the
connector's notification option. See the [execution-event API](../api/channel-execution-events.md).

On upgrade, ownership is reconstructed from both current/default bindings and
accepted channel inbox provenance, including historical detached sessions. Mixed
or incomplete historical ownership is quarantined: queued unsafe work is retained
as failed, and no model or old acknowledgement is replayed from it. An affected
chat's next ordinary input gets a fresh owned root and a fixed recovery notice instead of
loading old history or executing that input; the user can then resend. Other chats
continue normally. Legacy queued control replies that might contain global
session catalogs are replaced with a fixed notice; catalogs are also rebuilt for
the owning chat before delivery. Administrative history remains
inspectable; clear current/default references before deleting an old root.

Configure the connector's allowed chat IDs for the contacts intended to use this
agent. Permanent chat ownership does not change the trusted local web/API token's
administrative access or the deliberately shared semantics of standalone
`Agent.listen()` applications.

### Dynamically Installed Connector Packages

Connectors are ordinary Python packages declaring a `nagents.channels` entry point.
The Channels panel shows the server-controlled plugin directory and an installation
command. An agent can run that command through its existing approved shell tool,
then the panel's **Refresh** makes a newly installed connector available.

In the Kubernetes template, packages persist at `/state/channel-plugins`:

```bash
python -m pip install --pre --no-cache-dir --no-deps \
  --target "$NGN_CHANNEL_PLUGIN_PATH" nagents-channel-telegram-bot
```

The image already supplies the Telegram connector's Nagents/aiohttp requirements.
Other connectors may need compatible additional dependencies. The directory is
on the state volume, so packages survive image replacement. Local installations
default to a plugin directory beneath the configured data directory; use the path
shown by your own server rather than assuming the Kubernetes path.

Installation and activation are distinct: package discovery does not enable a bot
or supply credentials. Configure and enable it in Channels after installation.
New modules can be discovered in the running process. Upgrades to already imported
modules/dependencies may require a server restart; the app does not reload live
Python modules underneath executing tools.

### Scheduled Wakeups

The web Harness provides `schedule_wakeup`, with `wake_up_in` retained as the
historical tool name. This is a native Harness tool, not the legacy server's
global scheduler. For example, an agent can call:

```python
schedule_wakeup(minutes=5, reason="Check the earlier result and report back.")
```

`seconds`, `minutes`, `hours`, and `days` are additive, finite, nonnegative
numbers. Their sum must be greater than zero and no more than seven days. The
reason must contain 1 to 2,000 characters. The tool immediately acknowledges a
one-shot timer with an ID and due time; it does not sleep inside a tool call or
keep the original HTTP response open. The delay starts at acceptance, not when
the current model turn finishes. A due timer waits for the next safe idle
boundary rather than interrupting a model request, tool block, or approval.

The timer belongs to the scheduling agent and its original root conversation.
A root timer works without children. A child timer resumes the retained child
conversation; its result notifies its **immediate parent**. If that parent has
finished, an eligible retained parent is restarted in its own conversation and
reports its synthesis upward. Main observes the task lifecycle, but a grandchild
result is not independently injected into every ancestor's model context.
Unavailable or cancelled parents fail explicitly rather than silently rerouting
the result to Main. Notifications remain untrusted user-role data, not system
instructions or extra results for an already acknowledged tool call.

Execution is serialized with user runs, session changes, and settings writes.
Changing the selected session never redirects a wakeup into that conversation.
Automatic runs preserve permission ceilings and do not grant unattended file
writes, shell execution, or custom-tool approval. Scheduling is not permission
to perform a later privileged operation. Root-owned execution budgets still
apply to automatic child/parent activations; human follow-up counters are
separate. The scheduler also bounds pending timers and automatic activation
chains to prevent unbounded self-scheduling.

**Timers and child continuation handles are process-local.** They are cancelled
on server shutdown, pod restart, or deployment replacement; they are not durable
cron jobs. Persisted conversations and settings do not restore pending timers.
Do not schedule a critical reminder here that must survive a restart. Frontends
without a lifecycle-owned scheduler, including the ordinary CLI/TUI, reject the
tool rather than falsely acknowledging an unavailable service.

The browser reads session-scoped background activity while idle. Scheduling,
firing, notification delivery, and actual parent activation are distinct records;
a successful scheduling call does not mean the wakeup has fired. The activity
buffer is bounded and process-local. A gap is reported explicitly, not filled
with invented success or an automatic replay of a prompt. Ordinary request
streams remain non-replayable. Saved history is still the recovery path for
persisted conversation text, not a durable event or timer log.

### Microphone Dictation

Use the microphone control in the composer to record on **your browser's device**.
The mic shares the Send toolbar; recording and review controls appear only when
needed. The composer's **?** disclosure contains keyboard and microphone help.
Stop to transcribe, review or edit the returned text, and choose **Insert into
draft**. The existing draft is preserved; insertion appends to the latest draft,
including text typed while transcription was running. Only the normal **Send**
action sends that text to the coding model. Recording, transcription, and draft
insertion never start a chat run automatically.

Microphone access requires HTTPS or loopback, browser permission, and Web Audio /
AudioWorklet support. Permission is requested only after an explicit mic action.
The browser creates 16 kHz, mono, PCM16 WAV audio; no container microphone, host
audio-device mount, `voice` extra, PortAudio, or FFmpeg installation is needed.
If the browser cannot record, ordinary text input remains usable.

Recording has a configured duration cap. Reaching it stops capture and waits for
an explicit transcription action. Cancel/discard stops capture and releases its
tracks; navigating away or changing the connection also invalidates pending work.
Microphone cancellation and **Stop run** are separate controls. Cancelled or late
responses never replace a new session's draft. Transcription uploads are not
automatically retried: cancellation cannot recall audio already received by the
transcription provider.

#### Connection And Configuration

Transcription uses a **separate OpenAI Platform API key**, not the Codex login or
chat-provider credentials as a fallback. The chat connection can remain Codex.
The default transcription model is `gpt-4o-mini-transcribe`. Model selection here
is independent of chat-model discovery and requires a compatible file-transcription
model supporting JSON output and, when supplied, the singular `language` parameter.

Inject the transcription key into the backend's environment using your secret
management tooling, then select its variable name without putting the key in
YAML, browser settings, URLs, or the image:

```bash
ngn serve --dictation \
  --dictation-api-key-env NGN_TRANSCRIPTION_API_KEY \
  --dictation-model gpt-4o-mini-transcribe \
  --dictation-max-seconds 120
```

This assumes `NGN_TRANSCRIPTION_API_KEY` has already been supplied to the process.
The existing `NGN_DICTATION_*` defaults and trusted YAML options also work with
`serve`. The backend endpoint defaults to `https://api.openai.com/v1`; endpoint
and API-key-variable selection remain administrator-managed. See the
[deployment guide](ngn-web-deployment.md#transcription-credentials) for a runtime
Kubernetes Secret reference.

The **Dictation** section in Settings controls the enabled preference, model,
language, and recording duration. These preferences persist with workspace
settings. They cannot override an administrator's disabled service or maximum
duration. Missing credentials and demo mode are reported clearly without opening
the microphone or making a provider request. No provider credential is returned
to the browser.

The **Context compaction** section in Settings selects the automatic trigger:
the provider default, a token window, a message count, or off. The choice is a
persisted workspace override applied to the shared Harness on the next run. A
`messages` threshold counts stored conversation messages; a `tokens` window
compacts near 70% of the given context size. Manual `Harness.compact()` (and the
channel `/compact` command) always remain available.

#### Upload Contract

`POST /api/dictation/transcribe` accepts only a raw `audio/wav` body and also
requires `X-Ngn-Session` for the selected root conversation (or a root with a
verified live editor subscription) and
`X-Ngn-Settings-Revision` for the recording's captured settings revision. Stale
session/settings requests conflict rather than silently using a different
conversation or transcription configuration. A recovered WebSocket editor can
retain its root after server restart without changing the legacy shared selection.

One admitted upload/transcription owns the operation slot. Competing uploads,
settings saves, and session mutations conflict; queued runs and due wakeups wait
for the slot to become idle. Admission occurs before buffering the recording.
The backend counts actual received bytes, validates PCM format and duration,
and rebuilds WAV data without ancillary metadata before the provider upload.
The limit is `effective_seconds * 32000 + 4096` bytes, with at most 300 seconds.
Ordinary JSON endpoints retain their separate 64 KiB limit.

Audio is handled in bounded memory, not written as a workspace file or added to
conversation history. Provider requests have bounded time and response sizes,
use only the configured transcription credential, and do not follow redirects
or retry automatically. Returned text remains an unsent draft. Oversized draft
insertion is rejected with both texts retained rather than silently truncated.

### Runtime Settings

#### Workspace Tools

Open **Tools** in the sidebar to inspect registered built-in, channel, and extension
tools. Select a default agent/profile, search or filter tools, and use the switches
to enable or disable them. Tool descriptions and parameter schemas are read-only.
**Save tools** writes `.ngn/tools.yaml` in the workspace, for example:

```yaml
version: 1
agents:
  assistant:
    shell: false
    compact_history: true
```

`assistant` is the only built-in agent; configured custom profiles also appear in
the Tools selector. Unspecified tools are enabled by default. Selections are per agent and apply in
both `ngn serve` and the terminal harness. Disabled tools are omitted from model
requests and rejected by the executor, including when a configuration change
occurs during approval. Profile restrictions and per-call approvals still apply.
Disabling `schedule_wakeup` also disables its `wake_up_in` alias. **Reset agent**
removes that agent's overrides when saved. Changes use revision checks and atomic
file replacement; no credentials are written to this configuration.

The built-in **`compact_history`** requests compaction of the current conversation.
The agent finishes pending tool calls, persists their results, then compacts before
the next model round. It uses the configured compactor/strategy and emits normal
compaction events; it does not start a nested run. A cancelled or finished run
discards any unprocessed session-scoped request. The tool is also selectable in
Agent Designer.

#### Global and Workspace Preferences

Use **Global settings** in the sidebar for defaults shared by workspaces in this
installation. Click the workspace folder, then **Workspace settings**, for
overrides belonging only to that folder. Workspace overrides take priority;
**Use global defaults** removes the workspace override and inherits the current
global values. Profiles and entered API-key values remain workspace-specific.
Global settings carry credential references, not copied key values.

**Message submission** selects what happens when another web message arrives in
the same active conversation. **Queue** is the default: finish the current run,
then process accepted messages in order. **Interrupt** cancels the active run,
waits for cleanup, and then processes queued messages. Other conversations are
not cancelled, and retrying an already accepted message never interrupts a run.

Global defaults live in `data_dir/web-defaults.db`, shared by workspace databases
under that data directory. `GET/POST /api/settings/global` and
`POST /api/settings/global/reset` expose their own revision-checked scope. Saving
global defaults applies to the current workspace when it has no saved override;
other running workspace servers load those defaults on restart. Both scopes are
web-only, and dictation still respects administrator-provided limits.

Trash retention uses its [separate preferences API](#trash-api-contract); it is
not an additional field in the model/tool/dictation settings below.

Settings responses contain `scope`, `values`, `defaults`, `profiles` (`name`, `mode`, `model`),
an opaque `revision`, `persisted`, `effective_mode` (`build` or `reviewer`),
allowlisted `providers`/`apis`/`auths` lists, and read-only `connection`
(`provider`, `api`, `auth`, `base_url`, `api_key_env`, `key_configured`,
`auth_status`). Both `values` and `defaults` contain exactly these fields:

| Field | Accepted Values |
| --- | --- |
| `model` | Trimmed, nonblank provider model ID, at most 200 characters, without control characters |
| `agent` | An existing built-in or trusted configured profile name |
| `read_only` | Boolean workspace restriction; startup-enforced read-only operation cannot be relaxed by web preferences |
| `provider` | An allowlisted provider name from `providers`, for example `openrouter` or `openai_compatible` |
| `base_url` | Empty for the provider default, or an HTTP(S) endpoint without credentials, query parameters, or fragments, at most 300 characters; required for `litellm` |
| `api` | One of `auto`, `chat_completions`, `responses`, or `messages` (`completions` is rejected for the harness) |
| `auth` | One of `auto`, `api-key`, or `chatgpt`; `chatgpt` remains limited to the default OpenAI provider endpoint |
| `api_key_env` | Environment variable name for the key, at most 64 characters; never a literal secret |
| `shell_timeout` | Finite number greater than 0 and at most 600 seconds |
| `max_output` | Integer, 1,024 through 1,048,576 **bytes of tool output**, not model tokens |
| `max_file_bytes` | Integer, 1,024 through 4,194,304 bytes |
| `max_tool_rounds` | Integer, 1 through 1,000 |
| `max_subagent_depth` | Integer, 0 through 8; root depth is 0 |
| `dictation_enabled` | Boolean preference; effective only when the administrator permits dictation |
| `dictation_model` | Trimmed, nonblank compatible transcription model ID, at most 200 printable characters |
| `dictation_language` | Empty for automatic detection, or a two-letter lowercase language code |
| `dictation_max_seconds` | Integer, 1 through 300; effective recording duration is capped by the administrator's startup limit |
| `compact_trigger` | One of `auto` (provider default), `tokens`, `messages`, or `off` |
| `compact_tokens` | Integer, 1,024 through 10,000,000; the total context window used when `compact_trigger` is `tokens` |
| `compact_messages` | Integer, 1 through 10,000; the conversation length used when `compact_trigger` is `messages` |
| `submit_mode` | `queue` (default) or `interrupt` for new messages in the active web conversation |

POST carries the settings values plus the write-only `api_key` field. Unknown
fields, numeric strings, booleans used as numbers, and nonfinite numbers are
rejected with HTTP 422, as are invalid provider combinations (for example,
`litellm` without an endpoint, `chatgpt` with a custom endpoint, credentials in
an endpoint URL, or an unsupported API mode). Changing profile selects its
trusted instructions/mode, but the explicitly submitted model takes precedence
over that profile's model. Model IDs are free text: settings GET/save never query
a model catalog or test entitlement. A later provider request may reject an
unavailable ID. Permission ceilings and per-call approvals remain enforced.
Existing sessions, history, provider credentials, and tools are retained. New
children inherit the current model; retained children keep their prior state
under the existing child continuation contract.

`compact_trigger = auto` leaves the provider's context-limit default in place,
`tokens` compacts near 70% of `compact_tokens`, `messages` compacts once the
conversation reaches `compact_messages`, and `off` disables automatic compaction.
The choice applies to the shared Harness on the next run and persists with the
other overrides; manual `Harness.compact()` and the channel `/compact` command
remain available regardless.

An optional `api_key` stores a write-only key for the submitted provider; an
empty value leaves any stored key unchanged. `clear_api_key` removes it. A
stored key is installed only into this process's environment under
`api_key_env`, is never included in `values`, `defaults`, `connection`, logs, or
any response, and `key_configured` only signals its presence. Removing it
restores the process environment captured at startup, so deployment-provided
keys remain the fallback. Provider routing changes are applied by rebuilding the
provider after the save commits; the previous client is closed exactly once.

Mutations reject HTTP 409 during a run, approval, or another mutation, or when the
submitted revision is stale. Reload before retrying; do not automatically overwrite
another tab's settings. Reads expose only the last committed snapshot, including
during a save. Error `detail` is a safe string, never raw request/provider data.

The versioned `ngn_web_settings` singleton row lives in the **existing workspace
session SQLite database**, resolved by the Harness under `data_dir`. It stores only
the approved preferences and revision, never a key value or the full
configuration. Write-only keys live in a separate `ngn_web_provider_keys` table
in the same private database; both are deleted on reset. The override applies
across sessions and web-server restarts for that resolved workspace. One process
must own the workspace; this is not multi-process settings synchronization.
ConfigMap/YAML, CLI, profile and initial authentication/model resolution
establish startup defaults, followed by global defaults, **before** the saved
workspace override is applied. Reset deletes the workspace rows and inherits
current global defaults. The
CLI/TUI do not load this web-only override.

Existing version-1, version-2, and version-3 rows are validated against their original
schemas, retain their saved preferences, and receive the provider fields from
trusted defaults for newer fields. New saves write version 4, including message
submission policy. Existing chat preferences and revision checks are retained;
arbitrary unknown fields or versions are not accepted as a migration shortcut.

Legacy built-in selections migrate to `assistant`. An old read-only selection
remains read-only through the workspace restriction, rather than gaining write
or shell access. Explicit custom profiles retain their configured identity.

After a version-3 save, an older image cannot load that row. Roll back with a
compatible image or use administrator-reviewed settings-row recovery; preserve
the session database and credential store.

Bootstrap and settings responses also include a non-secret `dictation` capability
projection: effective `enabled`/`available`, `admin_enabled`, safe `status`, the
API-key variable name, effective duration/byte limits, PCM format, and the current
settings `revision`. The credential value and writable routing are never included.

A save joins its local SQLite transaction even if its HTTP request is cancelled;
on failure, live config, model, round limit and instructions are restored. If the
connection is lost, reload to determine whether the save committed before retrying.
Invalid JSON, unsupported versions, invalid values, or a removed saved profile block
web startup rather than silently restoring potentially more permissive defaults.
An administrator must stop ngn and repair or remove **only** the `ngn_web_settings`
row in the affected database; preserve session history and credential storage.
The API may edit only the allowlisted provider fields above. Plugins, trust,
paths, demo mode, workspace storage, arbitrary files, Python configuration, and
anything outside a validated candidate configuration remain server-managed. A
provider override never changes the administrator's demo-mode ceiling.

### Model Discovery

`GET /api/models` is an explicit request, never a startup, bootstrap, or settings
read side effect. It accepts no query parameters and uses the same `X-Ngn-Token`,
Host, Origin, and fetch-metadata guards as other API reads. A successful response
contains only `models` (an array of IDs) and `source`: `codex` for an active
`OpenAIProvider` using ChatGPT authentication, otherwise the actual `ProviderType.value` such as
`openai_compatible`, `openrouter`, or `litellm`. The configured provider can still
be named `openai` while the active connection is Codex.

Offline demo and unsupported connections return HTTP **501** with a safe string
`detail`. OpenAI-compatible API-key connections and Codex OAuth have separate
catalog implementations. Missing provider credentials, upstream errors, timeouts, and invalid
catalog data return HTTP **502**, not browser-authentication HTTP 401. A valid
empty upstream catalog returns HTTP 200 with `models: []`. Failures never supply
a fake or cached catalog, expose upstream exceptions, or change the provider.

Codex returns only picker-visible model IDs, including those marked
`supported_in_api: false`; that field does not indicate OAuth unavailability.
Its fixed catalog request uses compatibility version `0.153.4` while retaining
ngn's own client identity. This follows the [official Codex client contract](../api/provider.md#local-configuration-and-chatgpt-authentication),
not a stable public OpenAI REST guarantee. The existing backend login/refresh
flow and private deployment's selected authentication mode are unchanged.

Discovery does not change the selected model, settings values, persisted row,
revision, or browser draft, and it does not take over a running task or approval.
Selecting and saving a model remains a separate settings action. Manual model-ID
entry continues to work when discovery fails or is unsupported. IDs do not prove
capabilities or entitlement; a later generation request remains authoritative.
This endpoint and the library method are source-checkout features, not part of
the published `v0.5.0` web/API surface.

## Development Checks

The frontend is organized by responsibility within `src/nagents/web-ui/src/`:

- `app/App.tsx` composes the layout and features. `app/useClient.ts` coordinates
  connection, shared busy state, and explicit recovery.
- `features/chat/` contains conversation/composer rendering, the owning run-stream
  hook, and a tested transcript reducer/history adapter.
- `features/sessions/` owns session transport/state and session navigation.
- `features/approvals/` owns the exact pending decision and its accessible modal.
- `api/` contains HTTP transport with the per-process CSRF token and tested NDJSON parsing.
- `types.ts` defines shared backend contracts; `main.tsx` only mounts the app.

The Vite output contract is `../web/static` relative to `src/nagents/web-ui/`.
Python distributions include those generated assets, not frontend dependencies.

```bash
.venv/bin/pytest tests/test_web.py tests/test_cli.py
.venv/bin/ruff check src/nagents/web src/nagents/cli.py tests/test_web.py
.venv/bin/mypy src/nagents/web src/nagents/cli.py tests/test_web.py
npm --prefix src/nagents/web-ui test
npm --prefix src/nagents/web-ui run build
```
