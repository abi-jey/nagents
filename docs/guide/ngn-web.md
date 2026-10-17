# Local Web Client

`ngn serve` serves a React client for the existing **ngn Harness**. It shares the
terminal client's workspace-scoped sessions, tools, provider configuration, and
per-call approvals.

The Team pane shows the main conversation and delegated agents in an activity
tree. Select an agent to inspect its conversation and send it a message through
the shared composer. Discovered profiles that have not run appear as **Not
started**. See [Inspecting and messaging delegated agents](task-inspection.md)
for message delivery, settings, and restart behavior.

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
described below. Textual is not required.

### Your Own Connection

Open **Settings** for the **Agent Navigator**. Its left-hand tree contains
**Global defaults**, the workspace, and every available agent, including unused
definitions. Each has **Overview**, **Model**, **Instructions**, **Tools**, and
**Voice** sections. Select **Model** under Global defaults to manage connections
shared by all workspaces, or under the workspace to manage local connections and
defaults. Add named OpenAI,
Azure AI Foundry, Anthropic, Gemini, OpenRouter, Azure v1, LiteLLM or custom
OpenAI-compatible connections. Select a provider type to see its supported API
and authentication modes. Connection settings show the credential source and
effective endpoint without showing secrets. Select a connection in the left column
to browse its model catalog on the right. Select a model to choose its connection
and model together; browsing does not activate the connection. Enter a model ID directly when
discovery is unavailable. **Save workspace settings** or **Save global settings**
saves the provider/model reference, reasoning effort and other defaults separately from connection
changes. Unsaved settings require an explicit discard before switching connections.
The same connections appear in the TUI's `/provider` menu. The
global registry lives in `$XDG_CONFIG_HOME/ngn/providers.json` (normally
`~/.config/ngn/providers.json`); the workspace registry lives under
`$XDG_CONFIG_HOME/ngn/workspaces/<workspace-hash>/providers.json`. Use an API key **environment-variable name**
(`OPENAI_API_KEY` or `${OPENAI_API_KEY}`), never a literal key. The variable
must be set in the `ngn serve` process environment. OpenAI can use ngn's
ChatGPT device login or local Codex discovery; Foundry can use API keys or
Microsoft Entra ID with the optional `azure-identity` package.

The first named connection becomes active if no global default exists. A workspace
can use a global connection or its own connection; **Use global connection** removes
the workspace selection. Activating another switches chat's
provider; trusted agent profiles may also bind to a named provider. Voice
models, backend mode and voice are separate Global, Workspace, and Agent preferences in
the settings icon beside **Voice**. Select a voice connection there or follow the active
provider-registry default connection. Every browser voice connection uses the same-origin ngn audio
relay. The server owns ChatGPT/Codex media and control connections, or the Live
WebSocket for supported API-key and Foundry connections. The **Main assistant**
voice backend delegates reasoning to the selected conversation agent; **Separate hosted
backend** uses a standalone model. No named-connection
API key is stored in the web settings database. Provider connections never store
a chat model; global/workspace model preferences are shared by web and terminal.
Existing saved settings are not deleted when you add a
connection; set its environment variable before making requests.

Both model-default scopes can reference a global or workspace connection. An
agent model overrides the workspace default, which overrides the global default.
Open an agent's settings in Team to configure the same provider/model selection
for `assistant` or any unused profile. **Inherit** removes its explicit model
choice; **Restore definition** restores the model declared in its source file.
Accepted executions keep their connection and model until the next activation.
New trusted agent definitions appear in Team without restarting the server.
If a global model references a workspace-only connection that is absent in
another workspace, Settings shows that selection as unavailable. Choose an
available connection and model there to repair it; ngn does not send the request
through a substitute provider.

`ngn serve` logs resolved trusted config paths, provider registry path and
connection names/types, settings storage paths, session/run and tool lifecycle,
Live connection changes, and model catalog requests/results. Prompt contents,
tool arguments, keys, authorization headers and upstream response bodies are
not logged by these operational messages.

Model discovery uses the selected **saved connection** in the panel's scope,
with credentials resolved by the backend. A workspace connection with the same
name cannot replace a global connection during global discovery. Browsing a
catalog never activates a connection or changes authentication or billing mode.
Keep an existing private deployment's ChatGPT/Codex login; enabling
catalog discovery does not require switching it to an OpenAI Platform API key.

The selected-model summary distinguishes configured context limits from metadata
advertised for that exact model. Missing metadata stays unknown. Exact model
overrides take priority over the connection-wide limit, then provider metadata.
**Context compaction** controls when history is compacted; it does not change the
model's input capacity. Agent profiles can override the workspace model reference
and reasoning effort; the panel shows those overrides and their configuration
source. Edit an agent's model reference from its Team settings.

For your own installation, choose your own connection deliberately:

- **OpenAI Platform API key:** set `OPENAI_API_KEY` securely in the backend
  environment using your own key. In Provider connections add and activate an
  OpenAI connection with `auth: api-key` and `api_key_env: OPENAI_API_KEY`, then
  choose your model. Platform API usage and billing are separate from a
  ChatGPT subscription. Never put key values into browser settings, URLs, or Git.
- **ChatGPT/Codex login:** enter `/login` in the browser composer and choose
  **Sign in with ChatGPT**. Open the displayed OpenAI device page and approve
  the code you requested. ngn performs the exchange and saves credentials on the
  server. Completion updates the selected default OpenAI connection to
  `auth: chatgpt`; an unconfigured server gets a named `chatgpt` connection.
  Custom endpoints and API overrides must be changed in Provider connections first.
  Terminal `ngn login --device-auth` and `ngn login --status` remain available.
  Use your own eligible ChatGPT account; protected credential storage currently
  requires POSIX and persistent `XDG_DATA_HOME` in containers. See
  [device login](ngn.md#openai-device-login) for limitations.

The API-key and Codex catalogs are not interchangeable, and discovery never falls
back between them. In particular, a Codex OAuth access token is **not** an OpenAI
API key. Login/refresh remains on the backend; the browser receives only model
IDs, available context and reasoning metadata, and a non-secret source label.
Device login exposes only the verification
URL, one-time user code, expiration and status; it never returns tokens or the
private device authorization ID. Signing in changes the server's selected
connection. This does not add multi-user account isolation to a shared deployment.

`POST /api/login/chatgpt` with `{}` starts one server-owned flow; repeated starts
while it is active return that attempt. `GET /api/login/chatgpt` only reads its
status. `POST /api/login/chatgpt/cancel` with `{"id":"<attempt id>"}` cancels and
joins the matching attempt. These routes require the normal same-origin web
token. Finish active assistant or Live work first: sign-in holds the host's
configuration boundary until completion, cancellation or expiration. Server
shutdown cancels the flow, and terminal states clear the displayed code.

### Agent Navigator And Inheritance

Settings, Team's agent settings, the composer Voice gear, and Tools open the same
navigator at the relevant agent and section. The selected chat and active voice
connection remain mounted while navigating settings. Microphone and speaker
choices are local to the browser, separate from reusable agent preferences.

**Overview** shows saved effective values and their sources. **Instructions**
edits additional profile instructions; bundled instructions and loaded workspace
`AGENTS.md` files remain inspectable. The inherited text is visible in the editor;
**Customize** copies it into an editable draft, while inheritance restores the
parent or authored definition. Saving writes the selected scope's Markdown
preference without changing its model or tool selections. **Tools** selects a reusable allowlist and
shows unavailable tools with reasons. Workspace tool policy, MCP configuration,
and approval management remain accessible inside that section. A preference
cannot grant a tool excluded by an authored definition, ancestor, runtime mode,
or workspace policy.

Model, instructions, tools, and Voice have section-specific save actions. Drafts
survive navigation and closing the panel; an unsaved marker identifies them.
Concurrent changes require an explicit reload rather than replaying a stale
save. Saving or resetting one section preserves the others. Empty instructions,
an empty tool allowlist, and disabled Voice are explicit values, not inheritance.
Removing an agent override restores its authored definition; Voice also offers
**Use workspace defaults** to bypass that definition without deleting it.

Global and workspace instruction/tool defaults live in `agent-defaults/defaults.md`
beside each scope's model preferences. Agent overrides use the existing
`agent-models/<agent>.md` store, with `model`, `instructions`, `tools`, and `live`
sections. These are operator preferences; editing them does not rewrite imported
source definitions. Instructions refresh at the next model round, while model
and Voice changes apply at their next activation. Permission checks can tighten
tools during a run; existing children never gain a wider inherited permission
ceiling from later changes.

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
queued input, task activity, or other session references. A draft open in another
browser is protected. `--continue` prefers meaningful history over an abandoned
blank draft; an explicit session ID is still honored. Used conversations and
named sessions remain available.
**Settings**, **Channels**, **Tools**, **Trash**, and the workspace information overlay stay
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
Switching chats ends the current Live voice connection without cancelling the
assistant's work. Its answers stay in the original chat. Other chats can run
at the same time, and **Stop run** targets only the chat being viewed.

### Chat Folders

Use **New folder** beside Sessions to group chats by project or topic. Folders
form a tree: create subfolders or move a folder beneath another folder, up to 12
levels deep. Names must be unique among siblings, so different projects can each
have a Notes folder. Open a
chat's actions menu using its **More actions** button, right-click, or
**Shift+F10** / the keyboard Menu key. Opening the menu does not switch chats.
Choose **Move to folder…** to move it, or choose
**Ungrouped** to remove its folder assignment. Moving a chat changes only its
sidebar organization; its selected conversation, active work, approvals, and
history are unaffected.

Folders can be renamed, collapsed, or removed. Search matches chat titles and
folder names, temporarily revealing matching chats inside collapsed folders.
Folder membership and collapse state are stored with the workspace, survive a
server restart, and remain attached through Trash and restore. Removing a folder
moves its chats and direct subfolders to its parent (or Ungrouped and the top level
when removing a top-level folder); it never deletes conversations. Conflicting
subfolder names must be renamed or moved before removing their parent. Permanently deleting
a chat also removes its folder membership. Updates are revision-checked so two
browser windows cannot silently overwrite each other's folder edits.
An otherwise empty draft explicitly moved to a folder is kept as an organized
chat; untouched, ungrouped drafts continue to use normal empty-session cleanup.

Click the workspace name to see its server folder, current assistant/provider/model,
chat activity, enabled tool selections, and MCP connection status. **Manage tools**
and **Settings** open their corresponding controls. Provider logos are bundled
with ngn and served locally; custom endpoints use a neutral connection icon.

Choose **Fork chat** to immediately create a branch named `<chat title> · fork 1`,
then `fork 2`, and so on. Numbers advance per parent chat for each successful fork,
including forks given custom names with `/fork [title]`; renaming or deleting a
branch does not reset the sequence. A fork of a fork has its own sequence.

### Browser Slash Commands

Type `/` in the composer for suggestions, or submit `/help` to see the supported
browser actions. Commands execute through the same controls and permissions as
their corresponding buttons; unsupported names and invalid arguments keep the
draft and display an error.

| Command | Browser action |
| --- | --- |
| `/login` | Open server-side ChatGPT device sign-in. |
| `/skills [search]` | Browse discovered skills and insert a selected skill into the draft. |
| `/<skill-name> [task]`, `/skill:<skill-name> [task]` | Invoke a discovered skill through the conversation's normal input queue. |
| `/new` | Start or reuse a blank conversation. |
| `/fork [title]` | Copy the current idle conversation into an independent branch and select it. Title is optional: defaults to `<chat title> · fork 1`, `fork 2`, etc. Other chats may continue running. |
| `/rename <title>` | Persist a name of 1–80 printable characters for the current conversation. |
| `/sessions [session ID]`, `/resume [session ID]` | Browse conversations or select one. |
| `/compact` | Queue compaction of the submitted conversation. |
| `/model [model ID]`, `/provider [name]` | Open settings or select a model or connection. |
| `/agent [name]` | Open the conversation agent picker, or choose the profile for this chat. |
| `/agent --workspace <name>` | Change the default agent for new chats in this workspace. |
| `/context` | Show the conversation's context estimate. |
| `/settings`, `/tools`, `/channels`, `/voice` | Open the corresponding settings. |
| `/stop` | Stop the active run. |

`/compact` accepts no arguments or attachments. The browser submits
`command: "compact"` alongside the exact `/compact` prompt to `POST /api/messages`;
the server includes that field in the message's retry identity and processes it
through the existing serialized queue. The ordinary API prompt `/compact`, with
no explicit command field, remains model input. Compaction preserves the selected
session when it runs against another submitted root, and reports normal
compaction/error events. There is no browser endpoint for arbitrary Python command
callbacks or terminal-only actions.

Discovered skills appear alongside commands in the slash picker with a **Skill**
label and description. Built-ins such as `/login` keep their normal behavior;
use `/skill:login` to invoke a skill with that name. The picker refreshes when
opened, when returning to the tab, and after runs; **Refresh** reloads its catalog
on demand. Selecting a skill inserts its command without executing it.

Skill submissions carry `command: "skill"` and the original slash text to
`POST /api/messages`. The server resolves discovered skills and loads their
instructions at the normal queued response boundary. The transcript shows the
original command, and multiline task arguments are preserved. Unknown or disabled
skills fail validation; the browser retains an unaccepted draft. Skill invocation
does not enable disabled tools or execute scripts at load time. Attachments must
be removed before submitting a skill command. See [Skills](skills.md) for discovery
locations, argument substitution, and load budgets.

### Delete A Session

Choose the sidebar row's **Move to Trash** action to remove a session in **one
click, without confirmation**. You do not need to select it first. Row actions
appear on hover or keyboard focus and remain available on touch screens. A pending
state prevents duplicate submissions. Failed admission leaves the session,
conversation, and draft visible, with an error explaining what needs attention.

Soft deletion hides the root from the active session list and prevents ordinary
resume, message admission, channel selection, history access, or subscription to
it. Its saved conversation and all subagent conversations remain available for restoration until its retention
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
intact. Permanent deletion removes that root's saved history and all its subagent
histories, and offers no Undo.
It removes logical database records, not backups or SQLite free pages.

#### Task Tree And Routing Cleanup

Moving to Trash or deleting forever stops the selected root and its running
subagents, denies their pending approvals, cancels their scheduled wakeups, and
releases completed task handles. It waits for their cleanup before changing
saved history; no restart is required. Other chats keep running. Task ownership
comes from the root's task registry and persisted journal, never from a session
prefix, task name, or text in old messages.

Restore recovers the saved tree as read-only task history. It does not restart
cancelled work, timers, or queued messages; start a new agent to continue work.
An untitled conversation containing only subagent history or saved task input
counts as a used chat and is preserved by empty-session cleanup after restoration.
If storage fails after cancellation, the conversation remains saved and its work
remains stopped. Deletion does not undo tools that already completed.

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
ID to another chat. Workspace files, unrelated conversations and their child histories, saved
settings, channel configuration, and private credentials are preserved.

Cancellation joins the SQLite transaction and selection/subscription cleanup
before releasing the mutation boundary, avoiding partially applied membership changes.
Deleted-root subscribers are revoked, and clients refresh session membership;
ordinary resume/reconnect cannot restore a trashed root. Restore publishes the
updated session list but never replays a prompt, tool, or old subscription event.

A lost HTTP acknowledgement can mean the mutation committed. Refresh the active
session list and Trash before deciding what to do; do not blindly retry. Each
new soft deletion has an opaque `deletion_id`. Undo, Restore, and permanent purge
of a Trash item must supply that exact generation, so an old view cannot act on
a later deletion of the same session. See the [Trash API contract](#trash-api-contract).

### Execution And Access

- One local app instance owns up to four concurrent chat executions on one async
  lifespan/event loop. Each active chat has its own Harness, tool-call state,
  approvals and background-task handles. Web messages and channel notifications
  keep FIFO order within their chat; additional chats wait in the durable inbox
  when all four slots are occupied. Each message
  carries an explicit target session; changing the sidebar selection does not
  reroute already accepted input. Shared settings require all runs to be idle.
  Scheduled wakeups wait for their own chat and an available execution slot.
  Idle runtimes with retained children or pending wakeups keep those handles;
  unused additional runtimes release their clients and MCP resources.
  Messages and routing-command acknowledgements from one external channel
  conversation also keep their source order across session switches. Unavailable
  connector acknowledgements remain queued without blocking accepted model work.
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
  **Review later** dismisses only the approval dialog. Switching chats or losing
  the browser subscription keeps the pending decision until its deadline; return
  to that chat to review it. Browser decisions still require a live subscription
  to the exact chat, its run, call and approval nonce. Explicit **Deny** and
  **Stop run** retain their usual behavior.
- Partial streamed output remains visible after failure/cancellation. Subscription
  reconnects use a cursor and server-instance epoch. A gap or restart requests a
  fresh snapshot instead of replaying model/tool work. Web input has a stable
  client message ID for durable admission deduplication. Approval decisions are
  never automatically retried.
- Chat messages render Markdown headings, lists, tables, quotes, emphasis, links,
  and fenced code in both streamed responses and saved history. Tables and code
  scroll within their message on narrow screens. Embedded HTML stays literal;
  Markdown images become explicit links instead of loading remote resources.
  Fences labeled with YAML, Python, JavaScript, TypeScript, JSON, shell, HTML/XML,
  CSS, SQL, or diff receive syntax highlighting, including common aliases such as
  `yml`, `py`, `js`, and `ts`. Unknown or unlabeled languages stay plaintext.
  Highlighting uses locally bundled grammars; blocks above 32,000 characters or
  1,000 lines stay complete and scrollable as plaintext. The reusable
  [CodeBlock component](https://github.com/abi-jey/nagents/blob/main/src/nagents/web-ui/src/components/CodeBlock.md)
  documents the interface, supported aliases, and rendering limits.
  Tool records retain separate inputs,
  streamed output, final result/error, call identity, and measured duration when
  provided. Their payloads stay in literal or structured views. Nested tasks are
  grouped by their actual parent task IDs, not their
  names or event arrival order. Later activations retain separate execution
  evidence beneath the same task identity. Child approval decisions stay with
  that child's call, even when another agent uses the same call ID.
  Tool calls, task branches, the retained registry, and notification payloads start collapsed
  to keep the conversation visible. Expand a record for its full identifiers,
  inputs, results, and errors; explicit expansion choices survive live updates.
  The first live tool-output chunk opens its tool card and selects **Output**,
  unless you already closed that card or explicitly selected another tab.
  Task branches remain collapsed until you open them.
  Tool summaries use compact rows on the conversation surface, with a readable
  tool name, a bounded plain-text target when recognized, the observed state,
  and measured duration when available. Channel sends show the channel and
  destination. Narrow screens keep the name and target inline, truncate them
  as needed, and leave approval metadata in **Details**. Full tool names and exact
  inputs remain in the inspector, which uses a subtle inset for bounded output.
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
  and focus; differing partial output remains selected when the final result arrives.
  Native tool icons distinguish commands, file reads, edits, writes, file lists,
  searches, skills, team requests, scheduling, history compaction, and channels.
  MCP rows show their connection and method; **Details** retains the exact qualified
  tool name. Unknown tools retain their original names and a generic icon.
  Recognized file results show numbered content and line metadata, unified diffs
  with addition/deletion styling, or path/search lists. Instructions, skipped-file
  counts, and reported truncation remain visible when present; hashes and newline
  information are available under **Result metadata**. Edit and
  write **Inputs** label content as proposed; only the result reports changes.
  Scheduling shows the requested delay and reason, then the returned due time and
  wake-up ID. Its acknowledgement is separate from clock-marked wake-up activity
  and never establishes that the wake-up fired.
  **Raw** and **Copy** retain the exact input/result text. Unknown schemas, duplicate
  keys, unsafe integer IDs, malformed JSON, and oversized payloads use the bounded
  general view without dropping diagnostic fields. File paths and MCP results
  remain inert text. Native result layouts do not replace a live reading surface.
  Updates never reopen an explicitly closed card or switch an explicitly selected tab.
  Focusing a tool pauses conversation
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
| `GET sessions/{session_id}/agent` | Conversation profile, revision, effective and active identities, pending state, and allowed profile choices |
| `POST sessions/{session_id}/agent` | `{revision, agent}` selects this conversation’s next-run profile; supports active runs and rejects stale revisions |
| `GET sessions` | Selected session ID/history and this workspace's session list; idle only |
| `GET sessions/{session_id}/context` | Read-only estimated token breakdown of the request that session would send; safe while idle and during a run |
| `GET activity/{session_id}/{after}` | Read bounded, session-scoped wakeup/background activity after a cursor; does not start a run |
| `POST sessions/new` | `{}` creates/selects a session and returns the updated snapshot |
| `POST sessions/{session_id}/fork` | `{title?: string}` atomically copies the quiescent root, selects its independent fork, and returns the new snapshot. `forked_from` records ancestry separately from subagent relationships. The source's pending work, approvals, wakeups, and channel authority are never copied. |
| `POST sessions/{session_id}/rename` | `{title: string}` saves a nonblank name of at most 80 printable characters and returns the current selection's snapshot. Renaming another row does not navigate or cancel work. |
| `POST sessions/resume` | `{session_id}` checks workspace membership and returns its snapshot |
| `GET session-groups` | Folder tree `{revision, groups: [{id, name, collapsed, parent_id}], memberships: {session_id: group_id}}`; empty `parent_id` means top level |
| `POST session-groups/create` | `{revision, name, parent_id?}` creates a folder; sibling names are unique ignoring case, 1-80 characters; up to 128 folders and 12 levels |
| `POST session-groups/update` | `{revision, group_id, name, collapsed}` renames or collapses a folder |
| `POST session-groups/reparent` | `{revision, group_id, parent_id}` moves a folder and its subtree; cycles, excessive depth, missing parents, and sibling name collisions are rejected |
| `POST session-groups/move` | `{revision, session_id, group_id}` moves an existing root; an empty `group_id` means Ungrouped; allowed while chats run |
| `POST session-groups/remove` | `{revision, group_id}` removes a folder, promoting direct subfolders and chat assignments (including Trash) to its parent or the top level; chats are preserved; name collisions return 409 |
| `DELETE sessions/{session_id}` | `{}` or `{permanent: false}` stops the root's task tree and moves it to Trash; returns `Snapshot & {deleted_session_id: string, trash: TrashItem}` |
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
| `POST cancel` | `{run_id}` cancels/joins exactly that active run |
| `POST approval` | `{run_id, approval_id, call_id, decision: "allow" or "deny"}` |
| `GET live` | Dedicated GPT-Live readiness/reason, effective provider/model/backend/voice choices, and active voice session ID |
| `GET agent-settings?scope=global\|workspace\|agent&agent=<name>` | Effective instruction/tool preferences, inherited values, sources, available tools, and composite revision; agent is optional outside agent scope |
| `PUT agent-settings` | `{scope, agent?, revision, preferences}` replaces only instruction/tool preferences at that scope; omitted fields inherit and sibling model/Live sections are preserved |
| `GET live/instructions?scope=global\|workspace\|agent&agent=<name>` | Effective full Markdown prompt, inherited text/source, customization mode, file paths, and revision; agent is required only at agent scope |
| `PUT live/instructions` | `{scope, agent?, revision, mode: "inherit" or "custom", text}` saves only the Markdown instruction selection; inheritance requires empty text, while custom empty text is explicitly empty |
| `GET live/settings?scope=global\|workspace\|agent&agent=<name>` | Scoped Voice preferences, field sources, revision, and provider connection reference |
| `POST live/settings` | Revisioned `{scope, revision, preferences}` for global defaults or `{scope, agent?, revision, overrides}` for workspace/agent fields; applies to the next call |
| `DELETE live/settings` | `{scope: "agent", agent, revision}` removes only the operator Live section and restores the authored definition |
| `POST live/sessions` | `{voice?: string, revision, session_id}` starts a GPT-Live call through the server. `session_id` binds main-assistant work to the selected chat. Returns `201 {session_id, model, voice, context?}` with an opaque application session ID. No provider SDP or credentials reach the browser. |
| `WS live/sessions/{session_id}/audio` | Same-origin authenticated PCM16 mono 24 kHz binary audio frames in both directions; the server relays them to/from GPT-Live. Required `ngn.live.v2` also accepts server-only `{"type":"interrupt"}` to clear queued playback. |
| `GET live/sessions/{session_id}?after=0` | Bounded normalized transcript/status snapshot after a sequence cursor; renews the active call's browser lease |
| `GET live/sessions/{session_id}/delegation-details?delegation_id=…` | On-demand request, dispatched input, result, and event timeline for one exact opaque provider ID, including slash and dot-only IDs; authenticated and bounded. |
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

### Response Usage And Provider Summaries

Completed responses show provider-supplied reasoning text in a subdued section,
collapsed by default. Per-response token counts are not shown inline; reported
usage remains available in the collapsed recorded-run breakdown. In stored usage,
an absent reasoning count stays absent and an actual zero remains zero.
OpenAI and Anthropic report reasoning as part of output when that breakdown is
available. Gemini reports its thought count separately from candidate output.
The usage details explain that distinction without adding tokens twice.

A collapsed **Thinking** section contains displayable text supplied by the
provider. It streams independently of the answer and tool results. Providers that
return counts without text do not create an empty summary. The harness requests supported
summaries by default; set `reasoning_summaries` to `false` in harness configuration
to disable that request. Explicit library callers can use
`GenerationConfig(reasoning_summary="auto")` or `"off"`. Unsupported models keep
their normal request shape.

The web protocol uses revisioned `response_metadata` snapshots. Each has a unique
`response_id`, actual `response_session_id`, `run_id`, and `turn_id`; child activity
also retains its task and activation scope. A completed record is attached by its
exact SQLite assistant-row ID, never by matching answer text. Reconnect snapshots
replace earlier revisions. A cancelled response without a saved answer remains a
display-only history item. Forks receive new response/run identifiers and copied
usage, so later updates to the original cannot change the fork.

Per-run totals sum each recorded response once, including tool rounds and any
reported usage on failed calls. `run_usage.response_count`,
`usage_response_count`, and `reasoning_response_count` expose coverage.
`prompt_response_count` and `completion_response_count` identify coverage for
each displayed counter; missing counters make those totals partial. Root and child
runs are counted separately. `run_usage.complete` describes counter coverage among
recorded responses, not every attempted request. An attempt that ends before
response metadata arrives may be absent; the UI labels these recorded run totals.
The context indicator continues to describe the current model input and window,
not these usage totals.

Display metadata lives in a separate SQLite sidecar. Usage is retained with the
conversation. Summary text is retained for the latest 100 response records per
actual session, at most 16,384 characters each; older records keep their usage and
set `summary_expired`. Active summaries checkpoint at most every 100 ms, plus
phase changes, usage updates, and completion. Graceful cancellation persists the
latest producer snapshot even if its queued browser event was not yet consumed.
After an abrupt process loss, up to the checkpoint interval of summary text can
be missing; the next startup marks unfinished records interrupted.

Deleting or clearing a session removes its display metadata. Compaction does not
match or reattach summaries to rewritten context messages: metadata remains tied
to the original row, subject to the same visible-history boundary as that row.
Summaries and usage sidecars never enter model prompt/history replay, Live seeding,
or voice playback. Opaque provider reasoning items and protocol signatures are
not summary text.

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

The web Harness provides `schedule_wakeup`. For example, an agent can call:

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
execution.

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

Images delivered by the assistant appear inline automatically as their cards
approach the visible chat area. **Download** saves the original file; **Unload
preview** releases its memory, and **Show image preview** loads it again. Several
images load in turn, with bounded memory use and a retry control if a preview
cannot load. Audio and video deliveries retain their explicit load controls.

### GPT-Live Voice Conversations

The **Voice** button beside **Attach** starts voice in the current chat. Grant
microphone access when prompted. A connected network sphere sits beside the
composer controls. It responds independently to microphone PCM and the audio
actually scheduled for speaker playback, with quick, small contractions and
expansions. Signals travel between nodes and briefly displace them on arrival.
Listening continues alongside speaking or assistant work; idle signals and
gentle rotation keep the network alive between turns. Click the sphere to mute
or unmute your microphone.
The chat stays visible and typing remains available.

Status, elapsed time, microphone mute, speaker mute, **Audio**, and **Stop** live
in the composer. Mute controls are independent and do not end voice. **Stop**
finishes the connection. The settings icon beside **Voice** opens a complete
**Agent Navigator** directly at the selected agent's **Voice** section, without
starting the microphone. Voice, instructions, starting context, and the speech
model belong to that agent; the summary includes browser audio devices and the
currently connected voice session. The overlay fills the screen on phones.
During voice, **Audio** opens the same overlay; opening and closing it keeps voice
connected, and device changes remain available during the call.
Reduced-motion preferences disable the sphere's motion.
Delegation launches and successful results travel through the network once per
request; failed or cancelled work clears its activity without showing a successful
return. The textual event details remain available below the sphere. The renderer
uses the existing browser audio stream and never opens another microphone or
provider connection.
Speech captions appear in the same chat, marked **Live** with their speaker and
call-relative time. Ordinary messages and assistant work retain their usual
presentation. Captions remain visible after ending the call or reloading the page.
A composer status line shows the handoff when the main assistant takes a request: queued, processing, completed, stopped, or failed.
**Processing…** is a lifecycle status, not an assistant response. The assistant
receives the explicit delegated request as plain user text. For transcript-only
requests, ngn joins fresh user speech fragments; assistant speech, timestamps, and
delegation identifiers stay in the inspection data instead of the user message.
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

The final seed contains at most 14 text items and 7,000 UTF-8 bytes. This is ngn's
budget for starting chat text, not the model's context window; the main assistant
retains the saved conversation. The optional summary source uses
at most 128 messages plus a bounded window of saved caption fragments and
96,000 UTF-8 bytes. It explicitly marks missing, compacted, or clipped material;
a generated brief is never presented as the complete conversation. **Chat brief**
or **Chat context** beside the voice timer opens a compact overview. Exact voice
instructions and startup history remain expandable; **Seed details** contains the
size, omissions, and budget explanation.

GPT-Live can ask the main assistant for an earlier detail or a fuller recap through
its existing client-delegation path. That assistant keeps the authoritative
history, tools, and approval rules. The voice frontend receives the concise result;
it does not gain direct tool execution. Hosted voice remains separate and does
not receive the workspace chat's seed.

**Voice instructions** shows the full effective Live prompt as Markdown,
independently of the main assistant's instructions. **Inherit workspace** follows
the workspace prompt, which in turn inherits **Global defaults** when it has no
customization. The inherited text is visible and copyable. **Customize** starts
with that text; **Save voice instructions** saves a Markdown override at the
selected scope. Returning to inheritance removes the custom text and follows
future changes to the parent. At Global scope, **Use built-in default** restores
the packaged Markdown prompt.

Global and workspace prompts are saved in `live-instructions.md` beside their
model preferences, independently of external provider configuration paths.
Agent prompts are saved under the workspace configuration
directory in `agent-live-instructions/<agent>.md`. Each file has a small
`mode: custom` or `mode: inherit` YAML header; custom instructions are the Markdown
body. An empty custom body is explicitly empty and differs from inheritance.
These operator files leave authored agent definitions, model selections, and
other Voice preferences intact. Existing additional guidance is shown as part of
its effective prompt until a Markdown override is saved.

The saved full prompt is sent to the next voice conversation without a hidden
default being prepended. Active conversations keep their captured instructions.
The built-in prompt describes ngn's delegation and history behavior; customizing
it does not change the backend's tool permissions or approval rules. Instructions
are limited to 12,000 Unicode characters and 24,000 UTF-8 bytes; oversized input
is rejected rather than silently truncated. History remains data, separate from
these trusted, explicitly configured instructions.

The public transport uses `session.input`; ChatGPT login uses its verified
`initial_items` contract. See OpenAI's [session context guide](https://developers.openai.com/api/docs/guides/live-conversations)
and [delegation guide](https://developers.openai.com/api/docs/guides/live-delegation).

#### Voice Settings

1. Start **`ngn serve`**, then open **Settings → Workspace → Model** for provider connections.
2. Add an OpenAI or Foundry connection for voice. You can keep your current chat
   connection, including ChatGPT login, Anthropic or Gemini, active.
3. For an OpenAI connection, choose **ChatGPT login** or **Local Codex login** to
   use your existing account. API-key connections and Foundry Entra ID remain
   supported. Use the settings icon beside **Voice**, or **Audio** during voice.
4. Select **Global defaults**, the **Workspace**, or an **Agent**, then **Voice**.
   Select the **Provider connection** and **Speech model ID**, or inherit their
   defaults. Set **Live voice**, **Voice**, and **Voice instructions**. Each field
   identifies its source; **Use global** or **Use workspace** restores inheritance.
   Choose the reasoning backend. **Main assistant · this chat's agent** is the
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
apply to new sessions. Agent fields inherit workspace, global, then built-in values.
The normal chat model and Live model remain separate. Saving an empty agent Live
profile explicitly uses workspace defaults; **Restore definition** reveals the
source profile again. A call captures the selected chat agent's Live profile at
admission and keeps it throughout the call, including after settings edits or
later agent selection changes.

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
**System default** before starting a call.

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
connecting. Voice preferences can be saved during a call and apply to the next
voice conversation. The accepted call keeps its original profile. Browser device
changes apply immediately.
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

Assistant playback uses a continuous AudioWorklet sample queue, independent of
React rendering and per-packet main-thread scheduling. PCM remains mono 24 kHz
on the wire; the worklet resamples continuously to the AudioContext's actual
sample rate, including an anti-alias filter for lower-rate outputs. Its startup
queue target follows measured short delivery variation between 40 and 160 ms.
Short late arrivals raise the next startup target immediately; clean delivery
reduces it slowly. This helps absorb repeated jitter after re-priming, but cannot
recover audio missing during the first unexpected gap or a long upstream pause.
A one-second sample capacity bounds memory and latency; exceptional overflow
trims stale samples with a short fade instead of stopping every queued source.
The server paces unpaced provider bursts into 20 ms frames and backpressures the
producer at two seconds of buffered PCM, preserving long utterances without
flooding that browser queue. Already-paced RTP is sent as it arrives unless it
gets ahead of the cumulative sample clock; no second silence stream is inserted.
Interrupt and shutdown invalidate blocked writers and unsent frames, and a fixed
server interruption message clears already-delivered browser samples. New clients
explicitly negotiate `ngn.live.v2` with the existing token; cached v1 clients keep
their PCM-only wire contract and are never sent text control frames.
A slow correction capped at 0.1% prevents small remote/device clock differences
from accumulating over long calls. Microphone capture is unchanged.

The sphere consumes PCM tagged with the worklet's actual playout time. Mute,
suspension, reconnect and close reset the queue generation so delayed messages
cannot revive old speech or animation. Speaker changes retain the same audible sample queue,
audio clock and output worklet while advancing a separate report barrier. `LiveMedia.audioHealth()` exposes numeric playback
counters, and closing a call writes a numeric-only `ngn voice playback health`
summary to the browser console: received/played samples, queue depth and peak,
starvation count, dropped samples, target depth, device rate and clock correction.
Starvation also includes intentional gaps between replies; counters alone do not
prove network loss. Played-PCM and health reports each allow only one message in
flight; a busy UI skips old visualization frames without dropping audible PCM.
While connected, the composer control stays labelled **Live**. Speaking and
listening activity animate the sphere without changing that label; microphone
and speaker mute states remain on their separate controls.

Browser playback warnings and server audio timing logs include a validated voice
session identifier, so reconnects can be distinguished. Rate-limited anomaly
logs and final summaries separate microphone drops, event-loop lateness,
provider frame gaps, output queue waits, and WebSocket send delays. A provider
frame gap can include downstream backpressure: compare it with the write-wait
counter before attributing it to the network. The server also samples inbound
RTP packet/loss/jitter counters alongside decoded frames and receive/write wait
states, so packet arrival can be distinguished from decoder or output stalls.
Saved-permission database reads
run outside the audio event loop; their results are rechecked against current
run ownership, tool binding, and permission changes.

Fatal provider run failures log at **ERROR**; recoverable failures remain
warnings. The diagnostic includes an allowlisted transport category, exception
and cause types, numeric OS error codes, request phase and elapsed time, plus
model-call and HTTP-attempt identifiers when available. The existing run and
session identifiers join this to tool/delegation activity. Raw exception text,
provider URLs, headers, credentials, audio, and conversation content are not
included in these diagnostics. Full payload observation is not enabled by them.
Fatal voice lifecycle failures also produce an **ERROR** with the voice session
identifier and a fixed operation stage. Login transport failures are logged
before they become a generic user-facing error, preserving safe cause and OS
error details. Ordinary cancellation does not produce a failure log.

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
connection_id, backend_mode, model, backend_model, voice, instructions, context_mode}`; an empty `connection_id`
follows the configured provider-registry default, independently of the normal agent model. An explicit ID selects a voice connection
without changing the assistant. `backend_model` is used in hosted
mode only. The provider and base URL in `values` describe the selected connection
and cannot be changed by Voice settings. `POST /api/live/settings` accepts
`{scope:"global", revision, preferences}` or `{scope:"workspace", revision,
overrides}`. An empty overrides object clears every workspace override. Unknown
fields and invalid input are rejected without echoing values.

`GET /api/live/settings?scope=agent&agent=NAME` additionally returns nested
`effective` and `inherited` Live profiles, `origins`, `definition_live`,
`agent_overrides`, saved `overrides`, `saved_live_present`, and preference and
accepted-definition revisions. The nested model is `{provider, id}` and must name
an existing connection. `POST /api/live/settings` also accepts
`{scope:"agent", agent, revision, overrides}`. This replaces the saved Live layer;
`overrides: {}` explicitly inherits workspace/global values.
`DELETE /api/live/settings` with `{scope:"agent", agent, revision}` removes only the saved
Live section and restores the source definition. Other agent preference sections
are preserved by every save/reset.

Both reads and writes require an explicit scope. Configure authentication in the
provider registry; Voice preferences select a connection and contain no API keys.

Global and workspace scopes have revisioned SQLite records; agent Live preferences
share the revisioned Markdown operator overlay. Global defaults live under the ngn
configuration directory and workspace overrides in the workspace session DB;
reads and writes span both in one SQLite transaction. Existing saved preferences
are retained, including historical automatic connection selection. Retired
workspace Live settings are imported only when no current workspace record
exists. Provider credentials and registry files are never rewritten by Voice
settings. Competing saves use the stored revision. A stale
revision or a busy Live service returns `409`; reload settings before retrying.
If a save acknowledgement is lost, fetch the settings again to see the committed
state. Cancellation waits for an admitted transaction to finish.

Creation requires the current `revision` returned by `GET /api/live`, which binds
the current chat target agent, accepted definition generation, and preferences.
An optional supported voice string overrides the configured default (`""` uses
that default). Main assistant mode also
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
`ngn.live.v2` and `ngn.token.<bootstrap token>` subprotocols, with no query
parameters or provider credentials. Frames are bounded to 100 ms; a browser
disconnect ends and finalizes the call. Session creation accepts no connection
override, arbitrary command, or tool configuration input; connection changes
go through the revisioned settings route.

Snapshots contain `{session_id, status, model, voice, events, cursor, delegations}` and may
include a safe `message`. Status is `connecting`, `connected`, `closing`, `closed`,
or `error`. Events have `seq` and a normalized `type` (`transcript`, `delegation`, `status`, or
`error`); transcript records include `speaker`, `text`, and timing when available.
Pass the last cursor as `after`, an integer in `0..9007199254740991`. This is the
cursor query parameter. The separate delegation-details route accepts exactly one
`delegation_id` (nonblank, at most 256 characters). Duplicate selectors and tokens in URLs are
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

Details also retain `live_updates`: at most 96 sent or failed append attempts,
each with its sequence, thinking/commentary/instructions kind, actual wire event,
and a text preview bounded to 2,000 UTF-8 bytes. `live_updates_truncated` reports
older entries being dropped. A sent update records transport submission, not a
provider acknowledgment or proof of speaker playback. Payloads are available only
in expanded details; ordinary polling retains metadata and delivery warnings.

`GET /api/live/sessions/{session_id}/context` returns startup instructions and
the actual text-only seed messages captured when preparing provider dispatch.
This is a record of prepared input, not confirmation of provider acceptance.
It retains at most 16 messages / 32 KiB and 16 KiB of instructions, with omission
flags. These details use the same authentication and bounded session retention
as delegation inspection; routine polling and creation responses remain metadata-only.

Main-assistant delegation records contain `delegation_id`, `voice_session_id`,
`chat_session_id`, `run_id`, `status`, `agent`, `provider`, `model`, `text`, and
`seq`. The bridge preserves the provider's original delegation ID and captures
its request context when the notice arrives. It binds that ID to the original call and chat; `run_id` becomes available
when the main assistant accepts the work. The lifecycle is `queued` → `working`
→ `completed`, `failed`, or `cancelled`; a queued request can also end before
admission. Terminal state comes from the actual assistant task, never caption
timing or audio playback. Snapshots retain all active requests and the most recent
32 terminal records independently of the bounded event stream.

The voice layer owns media and spoken delivery; the normal web assistant owns
reasoning, tools, approvals, and durable outcomes. Typed and voice inputs share
the durable web inbox and FIFO order within their conversation. In queue mode,
the next input enters after the current model response and all of its returned
tool calls finish, before the next model request. It can also wake an assistant
waiting for background children. This keeps one model/tool owner and runs normal
input hooks, upload validation, history receipts, and approvals for every input.
Other conversations remain queued; `/compact` waits for the run to become idle.
While voice is attached, both typed and voice inputs steer at this boundary even
if the text-only submit preference is interrupt. Existing voice-owned work keeps
that protection after voice ends. Text-only runs retain their interrupt preference;
the explicit Stop control still cancels the active assistant run and its children.

Completed assistant messages, including interim, background-task, and scheduled
wakeup answers, are forwarded while voice is attached. Task lifecycle changes
use quiet thinking updates; child reasoning and raw child outputs are not spoken.
Known original delegation IDs remain attached to later updates; unrelated chat
work uses session-wide updates. Long work no longer loses its answer at the old
300-second voice waiter limit. Voice end detaches delivery; already queued work
continues like queued typed input. Caller labels and inbox receipts survive a
server restart, and an old call ID never attaches to a new call.

Public Live uses `session.thinking.append` for progress and
`session.commentary.append` for answers. When a background result arrives after
at least two seconds without speech, a fixed `session.instructions.append` can
request the caller's attention; quietness is checked again when sending. Native
ChatGPT login maps progress to its quiet `commentary` context channel and answers
to `speakable` context. Its protocol has no verified instruction-append equivalent,
so that attention instruction is omitted rather than spoken as assistant content.
All actual updates are chunked below the Live append limit. A blocked or failed
voice update stream reports a delivery warning and retains results in chat.
Live observations remain bounded, process-local inspection data; hosted Responses
does not emit this main-assistant handoff lifecycle.

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
These registered descriptions and schemas are sent to the selected model for enabled,
currently available tools; they guide tool selection and argument construction, while the executor still
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
Saved selections are intersected with the current profile's permissions, delegation
depth, and client capabilities before model requests. For example, read-only profiles
do not receive modifying or custom tools, and native wake-up tools require a client
scheduler. The complete registered catalog remains editable even when a tool is
unavailable; changing these capabilities does not replace its registered definition.
**Reset agent** removes that agent's overrides when saved. Changes use revision checks and atomic
file replacement; no credentials are written to this configuration.

**Saved tool approvals** lists the web operator's saved decisions separately from
these enable/disable selections. **Ask again** removes one saved approval immediately;
it does not change tool availability or discard unsaved selection edits. New grants
are created only by choosing **Always allow this tool** on a live, exact pending
approval, never by enabling tools or editing `.ngn/tools.yaml`.

Grants live in `tool-approvals.db`, beside this workspace's private `sessions.db`,
and apply to normal chat, voice, and queued work in this
workspace. They are not shared with another workspace or the terminal harness.
Built-in and inspectable plain-function grants survive server restarts and are
invalidated when their definition changes. Extension closures and bound objects
can hide server or destination identity, so their grants are restricted to the
current registration: reloading or replacing one asks again. The approval dialog
and saved-permissions list show that distinction. A tool with the same name but a
different definition never inherits a grant.

The built-in **`compact_history`** requests compaction of the current conversation.
The agent finishes pending tool calls, persists their results, then compacts before
the next model round. It uses the configured compaction strategy and emits normal
compaction events; it does not start a nested run. A cancelled or finished run
discards any unprocessed session-scoped request.

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

**Reasoning effort** appears below the model in Settings and in each agent’s settings.
Choose **Provider default** to omit an effort override. Supported levels come from
the selected provider/model capability metadata or documented native support;
unknown combinations keep the provider default. Workspace/global preferences
persist independently, and an explicit trusted profile choice takes precedence.
Changing a setting leaves accepted executions and their queued child notes unchanged;
the next independent run or child activation uses the saved choice.

`GET /api/reasoning-options` returns `{options, source, provider, model}` for
`scope=workspace|global`, a saved connection `provider`, and a draft `model`.
Options exclude the empty default. The route uses the saved provider connection;
it does not open a provider connection or alter an active model.

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
| `reasoning_effort` | Empty for provider default, or a supported `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` level |
| `read_only` | Boolean workspace restriction; startup-enforced read-only operation cannot be relaxed by web preferences |
| `provider` | An allowlisted provider name from `providers`, for example `openrouter` or `openai_compatible` |
| `base_url` | Empty for the provider default, or an HTTP(S) endpoint without credentials, query parameters, or fragments, at most 300 characters; required for `litellm` |
| `api` | One of `auto`, `chat_completions`, `responses`, or `messages` |
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

POST carries `{revision, values, model_binding: {provider, id}}`, where
`values.model` must match `model_binding.id`. The binding's provider is a saved
connection ID; `values.provider` remains the read-only provider kind. Responses
include `model_binding`, `default_model_binding`, `model_binding_source`,
`provider_origin`, `model_binding_available`, and an actionable unavailable
reason. Both scopes choose from the merged global/workspace connection registry.
Provider, endpoint, API, authentication, and
key-environment fields are read-only projections of the selected connection;
edit them through Provider connections. Unknown fields, numeric strings,
booleans used as numbers, and nonfinite numbers are
rejected with HTTP 422, as are invalid provider combinations (for example,
`litellm` without an endpoint, `chatgpt` with a custom endpoint, credentials in
an endpoint URL, or an unsupported API mode). Changing profile selects its
trusted instructions/mode; an explicit profile model overrides the workspace
model preference. Model IDs are free text: settings GET/save never query
a model catalog or test entitlement. A later provider request may reject an
unavailable ID. Permission ceilings and per-call approvals remain enforced.
Existing sessions, history, provider credentials, and tools are retained. New
children resolve their own agent model, then workspace and global defaults;
an explicit invocation model has highest priority. Retained children keep their
prior history under the existing child continuation contract.

`GET/PUT/DELETE /api/agent-models/{name}` reads, saves, and resets a reusable
agent's model selection, including `assistant`. PUT carries
`{revision, model: {provider, id}}`; both empty strings explicitly inherit
workspace/global defaults. DELETE carries `{revision}` and removes the
model-only override to reveal the source definition again. A missing override
is returned as `model: null`. Replies include `definition_model`, `inherited`,
`effective`, `source`, `provider_origin`, `source_path`, and availability.
These endpoints do not edit instructions, permissions, or provider credentials.
Saves publish `agent_profiles_changed` to refresh Team and model metadata.

`compact_trigger = auto` leaves the provider's context-limit default in place,
`tokens` compacts near 70% of `compact_tokens`, `messages` compacts once the
conversation reaches `compact_messages`, and `off` disables compaction.
The choice applies to the shared Harness on the next run and persists with the
other overrides. Manual `Harness.compact()` and `/compact` use the configured
strategy; enable one before compacting when this preference is `off`.

Settings requests reject `api_key` and `clear_api_key`. Configure the selected
provider's credential reference or ChatGPT/Codex login in Provider connections.
Credential values never enter the settings response.

Settings can be saved during an execution; its accepted runtime keeps its
configuration and the next activation receives the saved choice. Mutations
reject HTTP 409 when the submitted revision is stale. Reload before retrying; do not automatically overwrite
another tab's settings. Reads expose only the last committed snapshot, including
during a save. Error `detail` is a safe string, never raw request/provider data.

The versioned `ngn_web_settings` singleton row lives in the **existing workspace
session SQLite database**, resolved by the Harness under `data_dir`. It stores only
the approved preferences and revision, never a key value or the full
configuration. Existing credential rows from older installations remain private
and are not rewritten by ordinary preference saves; reset removes the stored
workspace overrides and their old credential rows. The override applies
across sessions and web-server restarts for that resolved workspace. One process
must own the workspace; this is not multi-process settings synchronization.
ConfigMap/JSON, CLI, profile and initial authentication/model resolution
establish startup defaults, followed by global defaults, **before** the saved
workspace override is applied. Reset deletes the workspace rows and inherits
current global defaults. The
CLI/TUI do not load this web-only override.

Version-1 through version-4 rows without removed fields retain supported
preferences and receive newer fields from trusted defaults. Version-5 rows
store workspace differences. Unknown or removed fields fail loading. Check the
[current configuration schema](ngn-configuration.md#complete-top-level-schema)
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
The server revalidates OpenAI's public stable Codex release metadata for every
catalog lookup and uses that version in its fixed catalog request while retaining
ngn's own client identity. Overlapping version checks are coalesced; a metadata
outage retains the last known version (bundled `0.162.0` before the first successful
check) with a five-minute retry backoff. The model list itself is always fetched
fresh. This adds at most two seconds for metadata discovery, sends no provider
credentials to the metadata host, and requires no Codex installation in the
container. See the [official Codex client contract and fallback behavior](../api/provider.md#local-configuration-and-chatgpt-authentication),
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

For a spoken browser round trip, use the
[browser voice E2E harness](../../examples/live/browser_e2e/README.md). It feeds a
known clip through a virtual microphone and records the rendered speaker graph,
with socket, caption, delegation, UI, and audio-frame timestamps. An offline
baseline and an injected-gap case validate playback and the recorder; an opt-in
authenticated mode exercises the normal ngn provider and real delegation. These
test assets are never mounted by the production application.

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


### Conversation agent profiles

The agent picker beside the chat composer selects who answers **this conversation**.
Each chat stores its own choice. Reloading and `/resume` restore it, and `/fork`
copies it with the conversation. **Settings → Agent profile** and
`/agent --workspace <name>` set the default for new chats; they do not change the
profiles saved in existing conversations. When upgrading an existing workspace,
its existing chats capture the current workspace default once.

A running response keeps its profile, model, provider, and instructions. A changed
selection applies to the next run; queued messages and voice requests wait for that
run instead of joining the old profile’s response. Selecting a read-only profile
immediately tightens future tool permissions in the current execution and its
children and denies pending approvals. Selecting a less restrictive profile does
not loosen that execution. An already executing tool is not interrupted.

The main row and context panel report the actual running agent while a change is
pending. A small **Agent: name** marker records an applied change in the transcript.
These markers are display metadata, excluded from model history and voice seeds.
Profiles restricted to delegation cannot be selected as the main conversation agent.
Administrator read-only restrictions remain effective for every profile.
