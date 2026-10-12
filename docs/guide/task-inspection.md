# Inspecting and messaging delegated agents

The WebUI uses the authenticated ngn server connection for task history, live
activity, settings, and messages. Opening an agent does not execute it, change
the selected root session, or send its transcript to a provider or channel.

The task index includes configured agent profiles, including discovered agent
definitions. Profiles report whether a human can start them under the current
root's delegation policy; a profile that is only available as a main agent is
not advertised as launchable. Starting an unused profile creates a real child
through the usual delegation lifecycle, quotas, and inherited permissions.

## HTTP contract

All routes require the same web token and origin checks as ordinary chat.
`root` must be an accessible root session. A raw child session ID never grants
access. An absent task or a task belonging to another root returns 404.

| Route | Result |
| --- | --- |
| `GET /api/sessions/{root}/tasks` | `{tasks, profiles}` |
| `GET /api/sessions/{root}/tasks/{task}` | Task, bounded history, current activity, receipts, context |
| `POST /api/sessions/{root}/tasks` | Start `{message_id, agent, text}`; returns `{task, receipt}` |
| `POST /api/sessions/{root}/tasks/{task}/messages` | Send `{message_id, text}`; returns receipt |
| `PUT /api/sessions/{root}/tasks/{task}/settings` | Save next-activation choices; returns `{task}` |

Messages require a UUID and 1–16,000 characters of nonblank text. Identical
retries return the same receipt without another quota debit or execution.
Reusing a UUID for another root, task, profile, or text returns 409. Check the
receipt's status; HTTP acceptance does not mean that the model read the message.

Receipt statuses are:

- `queued`: retained by this server for the target child's next safe boundary.
- `picked_up`: the child's user-history row and receipt were committed in one
  SQLite transaction. The model or tools may still fail afterward.
- `error`: the input could not be read, for example because the agent stopped.

The receipt includes its revision, target IDs, accepted activation, queue and pickup timestamps,
history row ID, and public reason. A running child's message waits until the
entire model response and tool batch finish, including pending approvals. It
does not approve a tool or interrupt a running tool. An idle retained child
continues through the normal owned root execution and parent-result lifecycle.
If the previous core producer has finished but its web journal is still closing,
admission waits up to five seconds for that exact execution to release. The wait
does not hold the workspace admission lock or join a newly starting model run.
An input committed during that transition remains owned by its pending admission
until the accepted execution binds it; the previous run cannot mark it unread.
Timed-out admissions return an explicit error, and do not leave a background job
to execute later.

## History and live activity

History accepts only `limit` (1–200, default 100) and `before` (an earlier row
ID). Rows are capped individually and the history page is capped at 256 KiB;
truncation is explicit. Provider-specific tool metadata, hidden reasoning,
system messages, and developer messages are not projected into the inspector.

The root WebSocket carries nested `task_activity` envelopes. Only task views
expand their payloads; ordinary root text, Live captions, and external channel
replies do not. The task and activation identities come from the host, not from
provider extras.

Native delegations retain `origin_history_id`, the exact committed root
assistant tool-call row that spawned them, and `origin_run_id`. Nested agents
inherit that root anchor. Continuation preserves it, so the same timeline can
be placed after its originating message following a reload. A direct profile
start has no fabricated main-history anchor; its origin fields remain empty.

Each `history_committed` marker contains a row watermark captured in the child
producer. A detail response combines history through that watermark with the
in-flight activity after it, avoiding duplicate messages while tools run.
`activity_cursor` identifies the latest included task sequence. A client applies
later sequences, and refreshes details when it sees `resync_required`.
`activity_truncated` explicitly signals a bounded or incomplete draft. The next
committed marker restores the complete persisted history prefix.

## Settings, restart, and deletion

Task settings accept `provider`, `model`, `compact_trigger`, `compact_tokens`,
and `compact_messages`. They appear as `next_settings` until the next idle
activation. A message queued into an active child keeps that activation's
accepted settings. Empty provider, model, or compaction trigger retains the
accepted choice. A provider must name a configured connection. Compiled Agent
Designer definitions keep their provider and model controlled by the definition.

Task workers are process-local. After restart, recorded task history and
receipts remain readable, but old workers cannot be resumed through the web
API. Unread receipts become errors and are never automatically replayed.

Pending inputs are bounded to 128 messages and 512,000 characters across the
workspace. Core per-task and root execution quotas also apply. New admission
prunes terminal receipt bodies to the latest 1024 per root. Older receipt identities and
SHA-256 text fingerprints preserve retry deduplication; the normal conversation
history has its existing session retention policy.

Trash removes access and terminalizes unread messages while preserving evidence
for restoration. Permanent deletion removes the manifest and its mapped hidden
child histories, and scrubs receipt text. Receipt identities remain as dedup
tombstones. Existing guards against deleting roots with retained runtime handles
still apply.
