# Inspecting and messaging delegated agents

The WebUI uses the authenticated ngn server connection for task history, live
activity, settings, and messages. Opening an agent does not execute it, change
the selected root session, or send its transcript to a provider or channel.

The task index includes configured agent profiles, including discovered agent
definitions. Profiles report whether a human can start them under the current
root's delegation policy; a profile that is only available as a main agent is
not advertised as launchable. Starting an unused profile creates a real child
through the usual delegation lifecycle, quotas, and inherited permissions.

The Team tree groups each parent's direct children by definition, then Running
and Past runs. The same definition under two parents remains two separate groups.
Waiting is active; completed, failed and cancelled runs are past. Empty buckets
are hidden. Past buckets and all-past definition groups start collapsed. Selecting
a conversation reveals its ancestor path and keeps its draft with that recipient.

## HTTP contract

All routes require the same web token and origin checks as ordinary chat.
`root_session_id` must be an accessible root session. A raw child session ID
never grants access. An absent child or a child belonging to another root
returns 404.

| Route | Result |
| --- | --- |
| `GET /api/sessions/{root_session_id}/tasks` | `{tasks, activations, profiles, profile_revision}` and `subagent_usage` while its runtime is available |
| `GET /api/sessions/{root_session_id}/tasks/{session_id}` | Task, bounded history, current activity, receipts, context |
| `POST /api/sessions/{root_session_id}/tasks` | Start `{message_id, agent, text, parent_session_id?, title?}`; returns `{task, receipt}` |
| `POST /api/sessions/{root_session_id}/tasks/{session_id}/messages` | Send `{message_id, text}`; returns receipt |
| `PUT /api/sessions/{root_session_id}/tasks/{session_id}/settings` | Save next-activation choices; returns `{task}` |

The start body requires a UUID `message_id`, a configured `agent` profile, and
nonblank `text` of 1–16,000 characters. `parent_session_id` is an optional short
session ID; omit it or send an empty string to start under Main. `title` is an
optional one-line display title. The host trims its surrounding whitespace,
then accepts at most 120 Unicode code points; embedded control, surrogate, line
separator, and paragraph separator characters are rejected. Emoji joiners are
allowed. A blank title on a new child falls back to its short session ID; a
blank title on an existing child preserves its current title. The legacy
`task_id`, `parent_task_id`, and `name` body fields are rejected. Responses use
`session_id`, `title`, `root_session_id`, and `parent_session_id`; private child
storage IDs are not exposed. Short IDs come from one monotonic base-36 workspace
counter shared across root conversations, so an alias is not reused while that
workspace database is retained.

Messages require a UUID and 1–16,000 characters of nonblank text. Identical
retries return the same receipt without another quota debit or execution.

`subagent_usage` reports accepted `limits` (`max_concurrent_subagents`,
`max_subagent_executions`, `max_subagent_depth`), `used_executions`,
`remaining_executions`, `occupied_slots`, `available_slots`, `retained_tasks`,
`max_retained_tasks`, `depth`, and the `reset` explanation. It is omitted for
unavailable runtimes: persisted history does not reconstruct live budget state.
Reusing a UUID for another root, parent session, profile, or text returns 409. Check the
receipt's status; HTTP acceptance does not mean that the model read the message.

Omitting `parent_session_id`, or sending an empty string, starts under Main. A
nonempty parent is resolved from that root's retained runtime; a parent from
another root returns 404. Only a running local parent can accept a direct nested
start. Idle, completed and remote A2A parents return 409; the server does not wake
them or silently create a root-level replacement. The chosen parent's accepted
profile catalog, `user_invocable` policy, ancestor permission ceilings and shared
quotas apply. The task index exposes `can_start_children`, `child_agents` and
`child_start_unavailable_reason` for the UI; these hints never replace admission
checks. Persisted history alone grants no ability to start a child.

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

Each model-facing `to_agent` activation also records `activation_history_id`,
`activation_run_id`, `activation_call_id`, and `activation_call_position` from the
host's exact committed invocation. These correlate a follow-up with its own chat
line without replacing the original spawn anchor or borrowing an older result.
Human, scheduled, and host-started activations have no model-call provenance.
These display fields do not grant execution authority.

The server also retains an immutable start and terminal record for each
activation. Root snapshots and child details expose these as
`retained_activations`; the task index exposes `activations`. They preserve the
title, model, request and result that belonged to each activation, so refreshing
the page cannot replace earlier results with a later follow-up. Incomplete
records from a previous server are displayed as interrupted. Historical records
are read-only and never reconstruct workers or authorize a new request.

Activation projections include visible call anchors after compaction and the
latest unanchored activation for each child. Detail previews initially allow
512 UTF-8 bytes for the prompt, 2,048 for the result, and 1,024 for an error.
They shorten further before omitting records to keep the projection within
2 MiB and 1,024 records. `details_truncated` identifies shortened previews;
`activations_omitted` on the index and `retained_activations_omitted` on snapshots
and child details report any exceptional omissions. The immutable database
records and paged child conversation retain the recorded content. These display
limits do not change model inputs, context budgets, or execution state.

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
accepted choice. A provider must name a configured connection.

Task workers are process-local. After restart, retained sessions created by the
current session contract and their receipts remain readable, but old workers
cannot be resumed through the web API. Unread receipts become errors and are
never automatically replayed. The new task index initializes automatically;
legacy task-index rows are not read, copied, or replayed. Recorded root chat
history remains available. Refresh browser clients to load the current session
contract; no active worker handles or running state are migrated.

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
