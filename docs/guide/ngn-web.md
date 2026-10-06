# Local Web Client

`ngn serve` serves a React client for the existing **ngn Harness**. It shares the
terminal client's workspace-scoped sessions, tools, provider configuration, and
per-call approvals. It does not use the older `nagents.server` application.

## Source Availability

Published distributions include `ngn serve`, the `web` extra, and built
frontend assets, verified in [v0.15.0](https://github.com/abi-jey/nagents/releases/tag/v0.15.0).
To use a published wheel in your active virtual environment:

```bash
python -m pip install 'nagents[web]'
ngn serve --demo --workspace /path/to/project
```

A published wheel does not need Node to serve its bundled frontend. This guide
follows the **current source checkout** and may describe newer features; compare
your installed version with the [release notes](https://github.com/abi-jey/nagents/releases).
For development, use the editable source workflow below.

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
port. Loopback is the CLI default; an explicit `--host 0.0.0.0` binds off loopback
for a trusted, access-controlled deployment.

Common Harness options work before or after `serve`, including `--provider`,
`--model`, `--agent`, `--config`, `--continue`, and `--resume`. API keys and saved
OAuth credentials stay on the backend. Configure model-provider credentials through
the existing CLI/environment. Connector credentials have a separate Channels form
described below. Textual is not required. The existing `server` extra also supplies
the HTTP/WebSocket dependencies, but the applications are independent.

### Your Own Connection

Open **Settings → Global → Provider connections** to add connections shared by all
workspaces, or **Settings → Workspace → Provider connections** to add a connection
just for this workspace or select a global connection. Add named OpenAI,
Azure AI Foundry, Anthropic, Gemini, OpenRouter, Azure v1, LiteLLM or custom
OpenAI-compatible connections. Select a provider type to see its supported API
and authentication modes; the list displays each connection's credential source
and effective endpoint without showing secrets. Choose a chat model in **Agent
profile and permissions**, where you can enter an ID or browse the active
connection's model catalog. The same connections appear in the TUI's `/provider` menu. The
global registry lives in `$XDG_CONFIG_HOME/ngn/providers.yaml` (normally
`~/.config/ngn/providers.yaml`); the workspace registry lives under
`$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/providers.yaml`. Use an API key **environment-variable name**
(`OPENAI_API_KEY` or `${OPENAI_API_KEY}`), never a literal key. The variable
must be set in the `ngn serve` process environment. OpenAI can use ngn's
ChatGPT device login or local Codex discovery; Foundry can use API keys or
Microsoft Entra ID with the optional `azure-identity` package.

The first named connection becomes active if no global default exists. A workspace
can use a global connection or its own connection; **Use global default** removes
the workspace selection. Choosing another switches chat's
provider; trusted agent profiles may also bind to a named provider. Voice
models, backend mode and voice are separate Global/Workspace preferences in
the settings icon beside **Voice**. Select a voice connection there or follow the active
chat connection. Every browser voice connection uses the same-origin ngn audio
relay. The server owns ChatGPT/Codex media and control connections, or the Live
WebSocket for supported API-key and Foundry connections. The **Main assistant**
voice backend delegates reasoning to the selected workspace agent; **Separate hosted
backend** uses a standalone model. No named-connection
API key is stored in the web settings database. Provider connections never store
a chat model; global/workspace model preferences are shared by web and terminal.
Existing saved settings are not deleted when you add a
connection; set its environment variable before making requests.

`ngn serve` logs resolved trusted config paths, provider registry path and
connection names/types, settings storage paths, session/run and tool lifecycle,
Live connection changes, and model catalog requests/results. Prompt contents,
tool arguments, keys, authorization headers and upstream response bodies are
not logged by these operational messages.

Model discovery uses the backend's **active provider**, not browser-supplied
credentials or a browser-selected endpoint. It never changes authentication or
billing mode. Keep an existing private deployment's ChatGPT/Codex login; enabling
catalog discovery does not require switching it to an OpenAI Platform API key.

For your own installation, choose your own connection deliberately:

- **OpenAI Platform API key:** set `OPENAI_API_KEY` securely in the backend
  environment using your own key, then run `ngn serve --provider openai --auth
  api-key --model gpt-6-luna`. Platform API usage and billing are separate from a
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
deployed image. The runtime image starts `ngn serve --host 0.0.0.0` without `--dev`;
see [container configuration](ngn-container.md) for no-file startup, credentials,
and a public Kubernetes example. See the [private deployment guide](ngn-web-deployment.md)
for its access-controlled deployment. There is no separate Vite origin/proxy required.

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
Repeated **New session** clicks reuse the selected unused draft. `ngn serve`
discards abandoned, unnamed drafts that have no messages, captions, uploads,
queued input, task activity, or other session references. Drafts with an active browser subscription are protected while the server
is running. `--continue` prefers meaningful history over an abandoned
blank draft; an explicit session ID is still honored. Used conversations and
named sessions remain available.
**Settings**, **Channels**, **Tools**, **Agent Designer**, **Trash**, and the workspace information overlay stay
in the bottom section, so they remain reachable with a long conversation history. On narrow
screens, **Toggle sessions** opens a drawer; Escape or its close button dismisses
it and returns focus to the navigation toggle. Dialogs opened from the drawer
return to their controls without losing the open navigation.

Unsent text belongs to its session. Switching conversations shows that
conversation's own draft. If an unused session is discarded while this tab still
has unsent text for it, **Recover draft** opens a blank conversation and restores
the text without sending it. The original remains readable if recovery fails or
another draft occupies the destination; existing text is never overwritten.
Drafts are held in this browser tab's memory; reloading or closing the tab discards them.
The composer stays editable while a message is being admitted. A late server
acknowledgement clears only the unchanged submitted draft, preserving newer typing.
Message badges distinguish **Sending…**, confirmed **Queued**, and **Delivery
unconfirmed** states. Working sessions have an indicator in the sidebar; when
another session is running, **View active session** takes you to it.

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

Soft deletion preserves the removed session's unsent **text** draft in this tab.
Restore and open that session to recover its draft; the replacement conversation
shows its own draft. Text drafts remain browser-local; server-side history
restoration does not recover that text on another device or after a page reload. Unsubmitted attachment
drafts are discarded when leaving or deleting their root; admitted attachments
remain part of the saved conversation available for restoration.

#### Retention And Automatic Cleanup

Trash retains sessions for **30 days by default**. The Trash panel's retention
control accepts an integer from **1 through 365 days** and saves a workspace-wide
policy that survives server restarts. This preference has its own API/revision,
separate from the model, provider, and tool Settings values.

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
  fail. **Allow once** approves that invocation. **Always allow this tool** also
  saves approval for this exact tool definition across all chats and agents in
  this workspace. **Deny**, Escape, cancellation, and expiry never save a grant.
  An unanswered approval expires after five minutes and is denied. Saved grants
  do not override read-only profiles, disabled tools, input validation, or file
  checks. Revoke them under **Tools → Saved tool approvals → Ask again**.
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
  Tool calls, task branches, the retained registry, and notification payloads start collapsed
  to keep the conversation visible. Expand a record for its full identifiers,
  inputs, results, and errors; explicit expansion choices survive live updates.
  Tool summaries show a readable tool name, a bounded plain-text target when
  recognized, the observed state, and measured duration when available. Channel
  sends show the channel and destination. On narrow screens the target moves
  beneath the name. Full tool names and exact inputs remain in the inspector.
  Structured shell results show **Exit 7** (with the actual nonzero code) or
  **Timed out** when appropriate. These describe the command's outcome; the
  enclosing task can continue and recover. Saved result strings are not parsed
  to infer an exit status.
  Opening a tool uses a single tabbed inspector: **Result**, **Output** (when
  distinct streamed output exists), **Inputs**, and **Details**. Results are
  selected first when available; inspecting an in-progress call keeps the reader's
  selected tab as new evidence arrives. Errors appear above the tabs. **Details**
  contains full tool/run/call/task identities and any observed approval decision.
  Left/Right, Home, and End navigate the tabs. **Copy** copies the exact received
  text; results are not reparsed or rounded. Long output stays in a bounded,
  keyboard-scrollable region. Tab changes retain each output's reading position.
  Promoting an identical stream to a result preserves the mounted reading surface
  and focus; differing partial output remains separately available. Updates never
  open a closed card or switch an inspected tab. Focusing a tool pauses conversation
  following so incoming output cannot pull the card away; returning to the feed's
  bottom resumes following. Backend output limits still apply.
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
- Image/PDF [attachment drafts](#image-and-pdf-attachments) use authenticated,
  session-scoped uploads and explicit submission. Arbitrary file
  HTTP access, a CORS wildcard, and a direct shell endpoint are not provided.
  Unknown routes remain 404.

This is a **trusted local-user tool, not a sandbox or multi-user service**. Other
processes/users with access to your loopback interface may access it. Do not expose
the standalone server through a reverse proxy, public tunnel, port forward, or
shared remote desktop. For administrator-managed **password-free Tailscale access**,
see the [private deployment guide](ngn-web-deployment.md). That setup runs the
same `ngn serve` process on one owner-approved workload, bound directly to the
in-cluster Service, using HTTPS at the ingress without Funnel or NodePort exposure,
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
The token is held in browser
memory, never a URL or local storage. Responses are non-cacheable, frame embedding
is blocked, and no third-party scripts/fonts are loaded. The browser's application
connections, including event and audio WebSockets, stay on the ngn origin. Content
Security Policy restricts connections, scripts, styles, fonts, and audio worklets
to that origin; images and media also permit local `blob:` and `data:` content.
Link DNS prefetching is disabled. External links open only when the user follows
them; they are not embedded resources.

WebSocket upgrades enforce the same Host/Origin boundary and authenticate before
accepting a connection. The browser offers `ngn.events.v1` and
`ngn.token.<process-token>` as subprotocols; the server selects only the former.
Tokens never belong in a WebSocket URL. Subscriptions can read only validated root
sessions in this workspace. Subscriber queues and replay history are bounded so a
slow tab does not block model execution.

### Tool Execution Browser QA

Use `ngn serve --demo` for **Inspect workspace** and `demo approval`; deny and
allow separate approvals to check visible decisions and the existing diff review.
For shell scenarios use a configured live provider (omit `--demo`) and ask it to
execute exactly these commands with the shell tool:

- Slow: `python -u -c "import time; print('starting', flush=True); time.sleep(10); print('finished')"`.
- Failure: `python -u -c "import sys; print('partial evidence', flush=True); sys.exit(7)"`.
- Large output: `python -c "print('\n'.join('line %04d ' % i + 'x' * 80 for i in range(500)))"`.

Approve the intended shell call. During slow execution, open/close the call and
switch inspector tabs, then check those choices after completion, failure, and subscription
reconnect. A full page reload starts fresh disclosure state. Read earlier messages
while output arrives and verify the scroll anchor; explicitly use **New task
activity** to reveal nested child activity. Check a delegated slow/failing call
inside a closed task branch as well. At 320px width and with keyboard-only
navigation, verify readable status labels, approval decisions in **Details**,
single-line target ellipsis, focus rings, Enter/Space disclosure activation, and
Left/Right/Home/End tab navigation. Large results must preserve the final received
line in the scrollable inspector. Check exact-text copying, clipboard failure feedback, focus and
output scroll position across incremental growth and completion, including while
reading midway through a record. The output region is height-bounded. A backend
truncation notice, when applicable, is evidence and must remain visible in the text.

## API Outline

All paths are under `/api`. API clients first GET `bootstrap`, then send its token
in `X-Ngn-Token`. POST, PUT, and DELETE requests also need
`Origin: http://127.0.0.1:8765` (matching the configured authority) and
`Content-Type: application/json`, except for dedicated attachment uploads.

| Method / Path | Purpose |
| --- | --- |
| `GET bootstrap` | Non-secret workspace/model/profile/demo info, token, active run ID |
| `GET settings` | Committed runtime values, startup defaults, profiles, revision, persistence and safe connection status; readable during a run |
| `GET models` | Explicit fresh discovery from the active provider; `{models: string[], source: string}`; read-only, including during a run |
| `GET provider-scopes/{scope}/providers` | Global or workspace list, type-specific choices, active ID, revision, YAML path and env availability (no secret values) |
| `PUT provider-scopes/{scope}/providers/{name}` | `{revision, profile}` adds or updates a connection in the selected scope; idle only, rejects literal keys |
| `POST provider-scopes/{scope}/providers/{name}/activate` | `{revision}` selects a global default or workspace connection; idle only |
| `DELETE provider-scopes/{scope}/providers/{name}` | `{revision}` removes an unbound, inactive connection; idle only |
| `POST providers/inherit` | `{revision}` clears the workspace selection, inheriting the global default |
| `GET providers/{name}/models` | Fetches a catalog with that connection's current authentication, without activating it |
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
| `GET live` | Dedicated GPT-Live readiness/reason, effective provider/model/backend/voice choices, and active voice session ID |
| `GET live/settings?scope=global\|workspace` | Voice defaults or effective workspace preferences, field origins/overrides, revision, and active provider connection reference |
| `POST live/settings` | Revisioned `{scope, revision, preferences}` for global defaults or `{scope, revision, overrides}` for workspace fields; unavailable during a call |
| `POST live/sessions` | `{voice?: string, revision, session_id}` starts a GPT-Live call through the server. `session_id` binds main-assistant work to the selected chat. Returns `201 {session_id, model, voice, context?}` with an opaque application session ID. No provider SDP or credentials reach the browser. |
| `WS live/sessions/{session_id}/audio` | Same-origin authenticated PCM16 mono 24 kHz binary audio frames in both directions; the server relays them to/from GPT-Live. |
| `GET live/sessions/{session_id}?after=0` | Bounded normalized transcript/status snapshot after a sequence cursor; renews the active call's browser lease |
| `GET live/sessions/{session_id}/delegations/{delegation_id}` | On-demand request, dispatched input, result, and application event timeline for one delegation; authenticated and bounded. |
| `POST live/sessions/{session_id}/close` | Ends the named voice call and returns its lifecycle status; send `{}` |

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
Automatic runs preserve permission ceilings. Scheduling itself does not grant
approval for writes, shell execution, or custom tools. An explicit workspace
**Always allow this tool** permission can approve that tool in an automatic run;
otherwise the existing browser or opted-in channel approval rules apply. Root-owned execution budgets still
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

### Image And PDF Attachments

Use **Attach**, paste an image, or drop files into the composer. Images have draft
previews and all attachments have a **Remove** control. Attaching only stages the
draft; **Send** or Enter explicitly admits a message. Image-only messages are
supported. Choose up to three files of at most 8 MiB each: JPEG, PNG, GIF, WebP,
or PDF, subject to the active provider/adapter's declared input support. The
picker omits unsupported formats, and the backend checks again at admission and
execution. This applies to the root's pinned designed provider as well.

The authenticated API is:

- `GET /api/sessions/{root}/uploads` returns the supported `file_media_types`
  and limits for that root.
- `POST /api/sessions/{root}/uploads/{upload_uuid}` stages raw bytes with the
  appropriate `Content-Type` and a percent-encoded `X-Ngn-Filename` header. Use
  a fresh canonical UUID for each draft file. Token and same-origin requirements
  are the same as other API writes; bytes are counted while streaming.
- `DELETE /api/sessions/{root}/uploads/{upload_uuid}` removes an unsubmitted
  draft. An admitted attachment cannot be retracted with this endpoint.
- `GET /api/sessions/{root}/uploads/{upload_uuid}/preview` returns an admitted
  image to its authenticated session, with caching disabled. Staged files and
  PDFs are not served from this endpoint.
- `POST /api/messages` accepts `attachments`, an ordered array of those IDs,
  alongside `session_id`, `message_id`, and `prompt`. The prompt may be empty
  when attachments are present. Text and IDs are bound atomically to the message
  UUID; retrying that UUID with changed attachments or text fails.

Unsubmitted bytes expire after one hour. Removal, navigation, and unmounting
request early cleanup and revoke local previews; expiry handles abandoned tabs
and interrupted acknowledgements. The server bounds staging to four simultaneous
uploads, three staged files per root, and 128 MiB per workspace. Session clear or
deletion invalidates in-flight upload generations as well as deleting drafts.

Accepted queued input survives restart and browser disconnection. Its bytes are
retained in storage/model history; the web transcript shows inline image previews
and file metadata without putting image/PDF bytes into event frames. Preview bytes
are returned only from the authenticated same-session endpoint above.
Unsupported inputs are rejected rather than forwarded to external connectors.
Audio/video understanding, upload-to-external-channel forwarding, and uploads in
offline demo/custom persistence adapters are outside this input path. See
[browser input lifecycle](channels.md#browser-image-and-pdf-input) for details.

### GPT-Live Voice Conversations

The **Voice** button beside **Attach** starts voice in the current chat. Grant
microphone access when prompted. A floating, interactive sphere sits beside the
composer controls, responding to microphone and playback levels. Click it to mute
or unmute your microphone; its glow and shape reflect the connection state.
The chat stays visible and typing remains available.

Status, elapsed time, microphone mute, speaker mute, **Audio**, and **Stop** live
in the composer. Mute controls are independent and do not end voice. **Stop**
finishes the connection. The settings icon beside **Voice** opens a complete
**Voice settings** overlay without starting the microphone. Devices, voice defaults,
instructions, context, and connection options share one scrollable form, with its
header and Save controls always visible. The overlay fills the screen on phones.
During voice, **Audio** opens the same overlay; opening and closing it keeps voice
connected, and device changes remain available during the call.
Reduced-motion preferences disable the sphere's motion.
Speech captions appear in the same chat, marked **Live** with their speaker and
call-relative time. Ordinary messages and assistant work retain their usual
presentation. Captions remain visible after ending the call or reloading the page.
A composer status line shows the handoff when the main assistant takes a request:
queued, working, completed, stopped, or failed.
**View in chat** opens the associated run's progress and result. Muting or ending
voice does not imply that an admitted assistant task has stopped.
**Request & events** expands inline with the received voice payload, the
actual input dispatched to the assistant, its result, and a compact processing timeline.
A request picker appears when there is more than one request. Individual event
types, captured model inputs, provider body chunks, and request identifiers can be expanded. These details
load only while the inspector is open; they are not included in every status poll.

#### Chat Context And Voice Instructions

Voice receives a bounded text handoff from the selected chat. It never copies the
entire model context, tool outputs, attachments, or private reasoning. Open
**Voice settings** to choose **Starting context**:

- **Recent chat + saved summary** (default) includes recent user/assistant text,
  any existing compaction summary, and saved speech captions after the current
  compaction boundary. Startup history is separate from new caller speech, so
  starting voice does not replay old tasks.
- **Prepare a concise summary** asks the selected main assistant's provider for a
  read-only brief with no tools. It uses a bounded source snapshot, then includes
  the brief and recent turns. Generation has a 15-second deadline and a separate
  2-second cleanup grace. Failure falls back to recent text and a saved summary,
  with the fallback shown in **Chat context**. Briefs are cached in memory for up
  to five minutes against the exact chat snapshot, provider identity, and model.
- **Start without chat context** sends no saved history at startup. The main
  assistant can still consult its conversation when the caller requests help.

The final seed contains at most 14 items and 7,000 UTF-8 bytes of text, leaving
headroom under GPT-Live's startup-input limit. The optional summary source uses
at most 128 messages plus 512 saved caption fragments, bounded together to
96,000 UTF-8 bytes. It explicitly marks missing, compacted, or clipped material;
a generated brief is never presented as the complete conversation. **Chat brief**
or **Chat context** beside the voice timer explains what was included.

GPT-Live can ask the main assistant for an earlier detail or a fuller recap through
its existing client-delegation path. That assistant keeps the authoritative
history, tools, and approval rules. The voice frontend receives the concise result;
it does not gain direct tool execution. Hosted voice remains separate and does
not receive the workspace chat's seed.

**Voice instructions** configures speaking style and behavior, independently of
the main assistant's instructions. The text is saved with the existing Global or
Workspace preferences; uncustomized workspace fields inherit the global value.
It is combined with the application's voice/delegation instructions at the next
voice start. Active conversations keep their startup instructions. Instructions
are limited to 6,000 Unicode characters and 12,000 UTF-8 bytes; oversized input
is rejected rather than silently truncated. History remains data, separate from
these trusted, explicitly configured instructions.

The public transport uses `session.input`; ChatGPT login uses its verified
`initial_items` contract. See OpenAI's [session context guide](https://developers.openai.com/api/docs/guides/live-conversations)
and [delegation guide](https://developers.openai.com/api/docs/guides/live-delegation).

#### Voice Settings

1. Start **`ngn serve`**, then open **Settings → Workspace → Provider connections**.
2. Add an OpenAI or Foundry connection for voice. You can keep your current chat
   connection, including ChatGPT login, Anthropic or Gemini, active.
3. For an OpenAI connection, choose **ChatGPT login** or **Local Codex login** to
   use your existing account. API-key connections and Foundry Entra ID remain
   supported. Use the settings icon beside **Voice**, or **Audio** during voice.
4. Select **Global** for defaults shared by workspaces or **This workspace** to
   override only chosen fields. Select the **Voice connection**, or leave it set
   to follow the active provider connection. Set **Enable GPT-Live**, **GPT-Live model**,
   **Hosted backend model**, and **Default voice** independently of provider
   connection and chat model selection. Unticked workspace fields follow later
   global changes. Choose the reasoning backend. **Main assistant (selected chat provider)** is the
   default: the selected conversation's assistant, provider, model, history, tools,
   and approvals remain in charge, and the Live service handles speech only.
   **Hosted Responses** remains an option for an independent voice conversation;
   its hosted backend model is used only in that mode.
5. Select the chat you want to talk to, then choose **Voice** to start.

Provider endpoint and authentication are edited in **Provider connections**.
Defaults are disabled, voice model `gpt-live-1`, the library's
`LiveConfig.backend_model` (`gpt-6-astra`), and voice `marin` for API-key calls.
ChatGPT/Codex login uses `gpt-live-1-codex` for the default voice model and defaults to
`sol`; its available voices are shown in the selector. Saved voice choices are preserved.
The selected connection's supported voice is resolved without changing your saved defaults for other
connections. Voice, model, and connection settings survive server restarts and
apply to new sessions.

**Audio** in the composer selects the microphone and speaker. Both start
at **System default**, allowing the browser to use your device's default input and
output. During voice, device changes apply immediately without starting another
GPT-Live session. Microphone swaps preserve mute and release the old microphone
after the replacement succeeds; a failed switch retains the previous device.
Choosing **System default** restores default routing. Successful choices are
saved only in this browser. Before voice starts, choices apply to the next session.
**Show devices** requests microphone permission to reveal device names;
opening settings alone does not capture audio. Refresh the list after connecting
hardware. Refreshing during voice never starts an extra microphone capture.
Speaker selection depends on browser support; unsupported browsers use
the system output. A saved device that is unavailable must be changed or reset to
**System default** before starting a call. If an explicitly selected speaker
disappears during voice, a notice offers **Use system default** for the current
call; output is never silently rerouted. Losing the active microphone stops
voice with a reconnect message. Choose a working microphone before retrying.

ChatGPT voice uses the signed-in account through server-owned media and Live
control connections. The browser exchanges audio only with ngn over a same-origin
WebSocket. ChatGPT voice requires **Main assistant** mode. Local Codex login
reads the existing Codex credential store; ChatGPT login uses ngn's existing
sign-in and refresh flow. An explicit login connection uses that login even when
an API key is present in the environment. The API-key path uses the selected
provider's environment reference, and Foundry can use Entra credentials.
Credential values, provider authentication headers, SDP, and raw upstream errors
are never returned by the API. ChatGPT voice follows the first-party Codex Live
contract; it is separate from the public `/v1/live/sessions` API-key protocol.

For a compatible dedicated endpoint, choose `openai_compatible` and enter its API
prefix, for example `https://voice.example.com/v1`. Azure v1 is supported through
`azure_openai_compatible_v1` with an explicit base URL and its API key; the existing
Foundry transport handles sideband authentication. Custom endpoints must implement
GPT-Live native WebSocket sessions (plus hosted Responses only in Hosted
Responses mode). Endpoint URLs
require HTTPS, except loopback HTTP for development, and cannot embed credentials,
query strings, fragments, or generation-route suffixes.

Save without setting the environment variable if you want to finish setup later.
The panel explains missing setup in **Voice settings**. Demo mode also permits
inspecting and saving settings, but real calls require starting `ngn serve`
without `--demo`. Readiness
checks local configuration; provider access and network failures are reported when
connecting. Settings saves conflict while a call is connecting, active, or closing.
After a competing tab changes settings, reload the form before saving or connecting.

#### Browser Requirements And Initial Scope

Use a browser with `getUserMedia` in a secure
context: loopback HTTP or HTTPS. Microphone permission and a working input device
are required. AudioWorklet captures PCM audio and exchanges it with `ngn serve`
over a same-origin WebSocket. The server uses its own Codex WebRTC media connection
for ChatGPT login, or the provider's Live WebSocket for supported API-key/Entra
connections. It owns provider negotiation, authentication, delegation, and
transcript events. The browser never opens a provider connection or receives
provider credentials. Browser-supplied captions or delegation events never trigger
assistant work. Install the `web` extra for the server media relay. No host
microphone, PortAudio, `voice` extra, or container audio-device mount is needed.

In **Main assistant** mode, GPT-Live delegates requests through the normal
Harness run for the chat selected when voice connects. The assistant retains
that chat's history, current provider/model, profile, tools, permissions and
human approval dialog. Voice requests and assistant answers join the selected
chat history; the browser shows tool activity in the same conversation. The
voice service receives the assistant's result to speak, not a copy of workspace
tools or provider credentials. The transcript is partial speech data rather
than a fabricated finished turn: review proposed writes and shell commands
before approving them, just as with typed messages. Saved **Always allow this
tool** workspace decisions also apply to voice work. A new approval without a
connected browser subscriber is denied. A concurrent chat run is serialized
before a voice request. Ending voice or losing its connection leaves an already
started assistant task running, with its result and tool activity available in
the chat. Use **Stop run** to cancel that task; server shutdown still cancels
and joins owned work. Reconnecting voice does not retry the action.

**Hosted Responses** mode has no workspace tools, chat history or main-agent
context. In both modes, provider-confirmed captions are saved against the chat
selected when the call starts, separately from model input. Captions do not create
extra assistant turns. Clearing or permanently deleting a chat removes its
captions; a call already active during a clear cannot repopulate that chat.
Audio is not saved to chat history; the provider's own data policy still applies.
Restarting the server
discards active voice-session state, while saved chat captions remain.

Only one voice call may be active per `ngn serve` instance, shared by its tabs.
End it manually before starting another. Successful snapshot polling renews the
call's browser lease; abandoned calls expire when polling stops. Server shutdown,
failed setup, and transport errors also trigger owned session/sideband cleanup.
Browser close/disconnect cleanup is best-effort, with server expiry as the fallback.
Reconnecting creates a new voice session; it does not replay old audio or silently
retry a provider call. Ending a call and **Stop run** for
chat are separate actions.

#### Live HTTP Contract

All Live routes use the existing same-origin, Host, fetch-metadata, and
`X-Ngn-Token` checks. Writes require the Origin and JSON content-type headers.
`GET /api/live` returns `{available, reason, provider, model, backend_model,
backend_mode, assistant, voice, voices, active_session_id, revision, enabled,
key_configured, voice_auth, transport, context_mode}`; `voice_auth` identifies `chatgpt`,
`api-key` or `entra`, and browser `transport` is always `websocket`. The selected
voice connection uses the normal provider registry, authentication, and defaults.
`key_configured` indicates available authentication, including a login; it does
not imply an API key is stored. `assistant` contains the selected chat provider/model/profile,
not a credential. `reason` is empty
when locally ready and the active ID is empty when there is no active call.

`GET /api/live/settings?scope=global|workspace` returns the effective `values`,
`global_preferences`, the workspace `overrides`, per-field `origins`, connection
reference and a 64-character hexadecimal revision. The workspace's missing
override fields inherit global values. Voice preferences are `{enabled,
connection_id, backend_mode, model, backend_model, voice, instructions,
context_mode}`; an empty `connection_id`
follows the active chat connection. An explicit ID selects a voice connection
without changing the assistant. `backend_model` is used in hosted
mode only. The provider and base URL in `values` describe the selected connection
and cannot be changed by Voice settings. `POST /api/live/settings` accepts
`{scope:"global", revision, preferences}` or `{scope:"workspace", revision,
overrides}`. An empty overrides object clears every workspace override. Unknown
fields and invalid input are rejected without echoing values.

For existing installations without a selected named provider, the unscoped
`GET/POST /api/live/settings` legacy connection/key contract remains available.
Scoped Voice preferences take effect when a named connection is selected; legacy
saved key values are not copied into provider YAML or the Voice records.

Each scope has a revisioned SQLite record. Global defaults live under the ngn
configuration directory and workspace overrides in the workspace session DB;
reads and writes span both in one SQLite transaction. The active provider's v1
YAML `live` preferences are imported once into the appropriate scope if no record
exists. They do not overwrite subsequent Voice edits. Provider YAML is never
updated by Voice settings. Competing saves use the stored revision. A stale
revision or a busy Live service returns `409`; reload settings before retrying.
If a save acknowledgement is lost, fetch the settings again to see the committed
state. Cancellation waits for an admitted transaction to finish.

Creation requires the current committed `revision` and an optional supported
voice string (`""` means the configured default). Main assistant mode also
requires the **selected root `session_id`**, which binds all delegated work and
approvals to that chat even if another tab changes the selection. A stale
permission-pending tab receives `409` before provider setup. Admission captures
one committed connection for that call. You can connect while the assistant is
already working; new voice requests wait for its current work. The normal
64 KiB JSON-body limit also applies. The request accepts no `sdp` field. The server
negotiates provider media and attaches the authenticated control connection before
reporting the call ready. The response contains only application session metadata.

For every provider, the browser exchanges only binary PCM16 mono audio at 24 kHz
on `/api/live/sessions/{session_id}/audio`. The server owns provider session setup.
Each browser WebSocket uses
`ngn.live.v1` and `ngn.token.<bootstrap token>` subprotocols, with no query
parameters or provider credentials. Frames are bounded to 100 ms; a browser
disconnect ends and finalizes the call. Session creation accepts no connection
override, arbitrary command, or tool configuration input; connection changes
go through the revisioned settings route.

Snapshots contain `{session_id, status, model, voice, events, cursor, delegations}` and may
include a safe `message` and startup `context` metadata. The context reports its
mode and method, bound chat ID, source fingerprint, included text counts/bytes,
and omission indicators; it does not return the seed text. Status is `connecting`,
`connected`, `closing`, `closed`,
or `error`. Events have `seq` and a normalized `type` (`transcript`, `delegation`, `status`, or
`error`); transcript records include `speaker`, `text`, and timing when available.
Pass the last cursor as `after`, an integer in `0..9007199254740991`. This is the
only allowed Live query parameter; duplicate cursors and tokens in URLs are
rejected. Polling returns bounded retained observations, not a durable replay log.

Delegation details use the same call/chat/run identities and explicitly identify
`source: "app_callback"`. Request and dispatched-input previews are capped at
32,768 characters each; result previews at 16,384. Each preview includes its
original character count and a truncation flag. The inspector retains the most
recent 96 lifecycle and model-capture events per request, with a timeline truncation flag.
Expanded model requests distinguish post-plugin messages, tools, and generation
settings from observed provider request body chunks. Chunks are identified by their
actual model-call and HTTP-attempt IDs; a captured chunk is not proof that the
provider received a complete request. Up to 32 captures are retained, with a
64 KiB limit per payload and 256 KiB total per delegation. Truncation is explicit;
when serialization stops early, the original total size is unavailable.
Transport authentication headers and URLs, HTTP responses, SDP, and unrelated
requests are not captured.
Details may expire with the bounded ended-session cache; durable
assistant results remain in normal chat history.

`GET /api/live/sessions/{session_id}/context` returns startup instructions and
the actual text-only seed messages captured when preparing provider dispatch.
This is a record of prepared input, not confirmation of provider acceptance.
It retains at most 16 messages / 32 KiB and 16 KiB of instructions, with omission
flags. These details use the same authentication and bounded session retention
as delegation inspection; routine polling and creation responses remain metadata-only.

Main-assistant delegation records contain `delegation_id`, `voice_session_id`,
`chat_session_id`, `run_id`, `status`, `agent`, `provider`, `model`, `text`, and
`seq`. The bridge issues an application delegation ID when it receives each handler
request. It binds that ID to the original call and chat; `run_id` becomes available
when the main assistant accepts the work. The lifecycle is `queued` → `working`
→ `completed`, `failed`, or `cancelled`; a queued request can also end before
admission. Terminal state comes from the actual assistant task, never caption
timing or audio playback. Snapshots retain all active requests and the most recent
32 terminal records independently of the bounded event stream.

The voice layer owns media and spoken delivery; the normal web assistant owns
reasoning, tools, approvals, and durable outcomes. The bridge waits up to 60 seconds
for an available assistant and up to 300 seconds for a spoken result, within the
outer voice timeout. If speech stops waiting while an admitted task continues,
it directs the caller to chat rather than reporting task failure. After a call
ends, the UI points to chat for further progress; live delegation observations
are not durable task history. Hosted Responses does not emit this main-assistant
handoff lifecycle.

These IDs correlate work; they do not provide business-action idempotency.
Duplicate upstream delegation IDs are suppressed, but distinct IDs have no
authoritative speech-turn boundary. Assistant completion confirms the task's
outcome, not that its spoken answer reached the speaker.

The **Context compaction** section in Settings selects the automatic trigger:
the provider default, a token window, a message count, or off. The choice is a
persisted workspace override applied to the shared Harness on the next run. A
`messages` threshold counts stored conversation messages; a `tokens` window
compacts near 70% of the given context size. Manual `Harness.compact()` (and the
channel `/compact` command) always remain available.

### Runtime Settings

#### Workspace Tools

Open **Tools** in the sidebar to inspect registered built-in, channel, and extension
tools. Select a default agent/profile, search or filter tools, and use the switches
to enable or disable them. Tool descriptions and parameter schemas are read-only.
These registered descriptions and schemas are sent to the selected model for enabled
tools; they guide tool selection and argument construction, while the executor still
enforces permissions and input validation. The catalog shows registered definitions:
a trusted `before_model` plugin may change a particular request, so its captured
model context/request is the source for that invocation. Search and category filters
only affect this list; **Enable visible**/**Disable visible** change selections that
apply after saving. Choosing an agent here edits that profile's selections without
switching the active conversation's agent.
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
occurs during approval. Profile restrictions and tool approval rules still apply.
Disabling `schedule_wakeup` also disables its `wake_up_in` alias. **Reset agent**
removes that agent's overrides when saved. Changes use revision checks and atomic
file replacement; no credentials are written to this configuration.

**Saved tool approvals** lists the web operator's saved decisions separately from
these enable/disable selections. **Ask again** removes one saved approval immediately;
it does not change tool availability or discard unsaved selection edits. New grants
are created only by choosing **Always allow this tool** on a live, exact pending
approval, never by enabling tools or editing `.ngn/tools.yaml`.

Grants live in `tool-approvals.db`, beside this workspace's private `sessions.db`,
and apply to normal chat, voice, queued work, and Agent Designer runs in this
workspace. They are not shared with another workspace or the terminal harness.
Built-in and inspectable plain-function grants survive server restarts and are
invalidated when their definition changes. Extension closures and bound objects
can hide server or destination identity, so their grants are restricted to the
current registration: reloading or replacing one asks again. The approval dialog
and saved-permissions list show that distinction. A tool with the same name but a
different definition never inherits a grant. Designer connection setup requests
such as starting an MCP subprocess are not registered tools; their **Always allow**
button is disabled with an explanation, and **Allow once** remains available.

The built-in **`compact_history`** requests compaction of the current conversation.
The agent finishes pending tool calls, persists their results, then compacts before
the next model round. It uses the configured compactor/strategy and emits normal
compaction events; it does not start a nested run. A cancelled or finished run
discards any unprocessed session-scoped request. The tool is also selectable in
Agent Designer.

#### Global and Workspace Preferences

Open **Settings** in the sidebar and switch between **Global** defaults shared by
workspaces and **Workspace** overrides belonging only to that folder. Switching
scope with unsaved settings or connection edits asks for confirmation. While open,
Settings checks for changes every four seconds without replacing an edited draft;
a reload option appears when a conflicting change or uncertain save needs review.
Workspace overrides take priority;
**Use global defaults** removes the workspace override and inherits the current
global values. New workspace saves store only fields that differ from inherited
defaults, so other global fields continue to follow changes to those defaults.
Trusted agent profiles remain workspace-specific. Provider connections carry
environment-variable names rather than key values.

**Message submission** selects what happens when another web message arrives in
the same active conversation. **Queue** is the default: finish the current run,
then process accepted messages in order. **Interrupt** cancels the active run,
waits for cleanup, and then processes queued messages. Other conversations are
not cancelled, and retrying an already accepted message never interrupts a run.

Global defaults live in `data_dir/web-defaults.db`, shared by workspace databases
under that data directory. `GET/POST /api/settings/global` and
`POST /api/settings/global/reset` expose their own revision-checked scope. Saving
global defaults updates inherited fields in the current workspace even when it
has unrelated local overrides; other running workspace servers load those defaults
on restart. Both scopes are web-only.

Trash retention uses its [separate preferences API](#trash-api-contract); it is
not an additional field in the model/tool settings below.

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

Version-1 through version-4 rows without removed fields retain supported
preferences and receive newer fields from trusted defaults. Version-5 rows
store workspace differences. Unknown or removed fields fail loading; see the
[manual upgrade steps](ngn-configuration.md#updating-existing-installations-to-provider-yaml-v2)
before restarting an existing installation. Existing chat preferences and
revision checks are retained when their rows are valid.

Legacy built-in selections migrate to `assistant`. An old read-only selection
remains read-only through the workspace restriction, rather than gaining write
or shell access. Explicit custom profiles retain their configured identity.

After a version-3 save, an older image cannot load that row. Roll back with a
compatible image or use administrator-reviewed settings-row recovery; preserve
the session database and credential store.

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
The endpoint and library method are included in v0.15.0. Older installations
may need an update; consult the release notes for your selected distribution.

## Development Checks

The frontend is organized by responsibility within `src/nagents/web-ui/src/`:

- `app/App.tsx` composes `WorkspaceHeader`, `WorkspaceSidebar`, `WorkspaceComposer`,
  and `WorkspaceDialogs`. `app/useClient.ts` connects feature hooks;
  `app/useOperations.ts` owns mutation admission and feedback, and
  `app/availability.ts` shares action rules between buttons and command handlers.
- `features/chat/` contains conversation/composer rendering, the owning run-stream
  hook, and a tested transcript reducer/history adapter.
- `features/sessions/` owns session transport/state and session navigation;
  `useSessionActions.ts` coordinates deletion, Trash, restoration, and selection races.
- `features/chat/useSessionDraft.ts` owns per-session tab-local text drafts;
  `useUploads.ts` owns attachment staging lifecycle independently of the composer.
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
