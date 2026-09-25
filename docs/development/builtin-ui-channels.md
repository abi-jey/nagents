# Built-in TUI and web channels: design proposal

**Status:** package A (#42) accepted for implementation; packages B–E remain proposals.
**Origin:** [issue #40](https://github.com/abi-jey/nagents/issues/40).
**Baseline:** v0.13.4 (`b317873`), investigated on 2026-09-25.

The maintainer first selected **design first**, then accepted package A (#42):
shared dispatch and capability declarations. Concrete UI adapters, persistence
and browser media remain proposed. This design is not a claim that those features
are available in the current release; the channel guide documents implemented APIs.

## Recommendation

Model each interface as a **first-party, host-managed channel for explicit
delivery**, while retaining its existing input admission and Harness execution.
The TUI accepts text deliveries. The web UI accepts text, screenshots/images and
supported audio/video files, with durable storage and actual browser rendering.

Do not start `Agent.listen()` alongside either UI. Both already own execution,
queueing, cancellation and approvals. Share the channel catalog and dispatch
contract without introducing a second consumer of the same session.

Ordinary assistant responses continue through the existing event stream. An
explicit `channel_send` creates a separate, identifiable delivery; final assistant
text is never automatically broadcast or copied into a channel send.

### Alternatives considered

| Approach | Benefit | Cost / decision |
| --- | --- | --- |
| Convert all UI input to `Agent.listen()` | One ingress abstraction | Conflicts with current Harness ownership; rewrites commands, task continuation, approvals and web routing. Defer. |
| Independent UI-specific media tools | Small initial integration | Duplicates discovery, file loading, validation and dispatch. Prefer the existing channel tool boundary. |
| Host-managed `Channel` delivery adapters | Reuses explicit send, capabilities and errors; preserves UI execution | Requires separating dispatch from listening and adding a durable delivery surface. Recommended. |

This is a delivery integration, not a claim that every UI interaction becomes a
`ChannelMessage`. The owning host still handles user input and approval decisions.

## Current implementation evidence

| Area | Source and finding |
| --- | --- |
| Channel contract | `src/nagents/channels/types.py`: optional hooks and a flat `capabilities` tuple; `listen` and `send` are abstract. |
| Standalone execution | `src/nagents/agent.py`: `add_channel()` only attaches; `listen()` owns Agent execution and guards independent runs. |
| Dispatch | `src/nagents/channels/runtime.py`: catalog, action schemas, file loading and send validation currently live inside `ChannelRuntime`. |
| Existing host-managed precedent | `src/nagents/web/channel_host.py`: registers tool wrappers and reuses `ChannelRuntime` with `_active = True`, without running its listener. |
| TUI | `src/nagents/tui/app.py`: submits through its own queue and calls `Harness.run()` / task continuation; it has no channel integration. |
| Browser ingress | `src/nagents/web/app.py`, `routing.py`, `service.py`: queued `/api/messages` enters a durable, deduplicated inbox and survives browser disconnection. The legacy streaming response cancels unfinished work on disconnect. |
| Approval enforcement | `src/nagents/harness/tools.py`: the executor checks schemas, profiles, approval and callable identity. |
| Browser media presentation | `src/nagents/web-ui/src/features/chat/ChannelMessage.tsx`: images have markup, documents/audio have labels; there is no video player. |
| Browser input | `Composer.tsx` and `/api/messages`: text submission; no chat upload/paste path. Dictation produces editable text rather than an audio attachment. |

Current outbound `channel_send` already reads workspace-relative files, rejects
symlink/path escapes and limits a send to **3 files, 20 MiB per file, 30 MiB total**.
Inbound native content is narrower: **3 attachments, 8 MiB each**, restricted to
JPEG, PNG, GIF, WebP and PDF. Audio/video playback is independent of model input
support.

The production CSP currently allows `img-src 'self'`, while the image component
uses `data:` URLs. It also lacks a `media-src` allowance. Existing component code
therefore does not establish working end-to-end image or playable-media support.

## Execution and registration

Extract a listener-independent dispatcher from the existing channel runtime. It
owns catalog construction, action-schema validation, bounded workspace-file
loading, dispatch, delivery validation and sanitized failure semantics.

- `ChannelRuntime` uses it while retaining its durable inbox, listener lifecycle
  and execution ownership guards.
- The web host uses it through its existing tool wrappers, including those
  copied into designed Harness instances.
- The TUI installs owned tool definitions after trusted extension initialization
  and removes only those definitions on shutdown.
- Hosts explicitly construct their built-in adapters. They are not third-party
  entry points, saved connector configurations or background source workers.
- Proposed names are `builtin.tui` and `builtin.web`. Detect collisions explicitly;
  never overwrite a configured connector or tool.

Adapters implement `Channel.send`. They do not advertise the legacy `receive`
capability. Calling their `listen()` directly fails with a clear `ChannelError`;
it must not silently pretend to start a working listener.

Only the running host's built-in route is advertised. A TUI process does not
implicitly start a web server, and this proposal does not enable two processes to
own the same session database. Cross-process TUI-to-web delivery would require a
separately designed host/transport connection.

## Capability model and negotiation

Keep the existing tuple and its meanings compatible with third-party connectors.
Add an **optional structured catalog descriptor** for precise directional media
support. Existing connectors that do not provide it remain supported; absence
means *undeclared*, not *text-only* or *supports everything*.

Expose it through an optional typed `Channel.content_capabilities` attribute,
defaulting to no descriptor. Omit the catalog field entirely for legacy instances,
preserving their existing catalog shape. Validate fixed directional fields,
boolean text support, `via` values, positive integer limits and bounded MIME lists
(at most 32 entries per direction, 127 characters per MIME type, 8 KiB total).
Reject invalid declarations at binding; do not accept arbitrary nested metadata.
An absent descriptor never becomes a new authorization or send-dispatch gate.

The descriptor distinguishes:

- **Receive:** content admitted from a human/transport into the agent's input.
  Declare whether admission is host-managed or uses `Channel.listen`.
- **Send:** content accepted by an explicit agent-to-channel delivery.
- **Render:** content the recipient interface can present, separately from model
  understanding and transport acceptance.

Proposed catalog shape (names subject to implementation review):

```json
{
  "name": "builtin.web",
  "capabilities": ["send_text", "send_files"],
  "content_capabilities": {
    "receive": {"via": "host", "text": true, "file_media_types": []},
    "send": {
      "text": true,
      "file_media_types": [
        "image/png", "image/jpeg", "image/gif", "image/webp",
        "audio/mpeg", "audio/wav", "audio/ogg", "video/mp4", "video/webm"
      ],
      "max_files": 3,
      "max_file_bytes": 20971520,
      "max_total_bytes": 31457280
    },
    "render": {
      "text": true,
      "file_media_types": [
        "image/png", "image/jpeg", "image/gif", "image/webp",
        "audio/mpeg", "audio/wav", "audio/ogg", "video/mp4", "video/webm"
      ],
      "playback": "browser_dependent"
    }
  }
}
```

`receive.via = host` describes UI admission, not support for standalone
`Channel.listen()`. For the first increment, web receive remains text-only even
though its send/render directions support media. Browser upload/paste is a
separate, dependent increment.

`text` describes the message text field; it does not authorize `.txt` attachments.
The separate `file_media_types` allowlist describes files. `playback` is a bounded
enum indicating native browser decoding with an explicit download fallback.

Use static per-instance capabilities and `channel_list` discovery; no network
handshake is needed for an in-process interface. Supply the active destination
through request-local trusted context. Refresh discovery when the host changes
capabilities, and always validate the actual send at dispatch time. Descriptive
capabilities never grant authorization or override provider modality limits.

### Initial end-to-end capability matrix

**Supported** means implemented and tested in the future delivery increment;
**degraded** means an explicit fallback; **unsupported** means reject the operation.
Transport support remains subject to tool availability, profile restrictions and
approval. Current reviewer/demo policies reject these custom tools; even
`channel_list` is not inherently approval-free.

| Operation | TUI | Web |
| --- | --- | --- |
| Human text input → agent | Supported through existing host | Supported through existing host |
| Agent text → interface | Supported | Supported |
| Agent screenshot/image file → interface | Unsupported; no partial text send | Supported for the listed image types |
| Agent audio/video file → interface | Unsupported | Supported storage and player; degraded to download if the browser cannot decode it |
| Human pasted/uploaded image → agent | Unsupported | Unsupported initially; follow-up |
| Human uploaded PDF → agent | Unsupported | Unsupported initially; follow-up, subject to provider support |
| Human audio/video attachment → model | Unsupported | Unsupported in this proposal; dictation remains text input |
| Generic file delivery | Unsupported | Unsupported initially; do not silently serve arbitrary content as active media |
| Historic media viewed in text-only UI | Degraded to filename/type/channel note | Supported for retained assets |
| Tool approval | Existing terminal modal | Existing browser dialog |
| Execution/status notices | Existing text/tool presentation | Existing richer tool presentation |

A screenshot is an image file supplied to `channel_send`; screenshot capture,
recording and transcoding tools are outside this design. A successful media send
means the complete delivery was stored in the target conversation, not that a
human viewed it or a browser successfully decoded it.

## Identity, routing and approval ownership

| Identity | Proposed mapping |
| --- | --- |
| Built-in destination / conversation | Executing root session ID |
| Browser tab | Subscriber to that root, not a conversation or listener |
| TUI process | Current presentation/lifecycle owner, not durable identity |
| Delivery ID | Opaque, persisted local ID returned in `ChannelDelivery` |
| Origin | Host-derived root/actor-session/run/task/activation/tool-call correlation |
| `thread_id` / `reply_to` | Reject nonempty values initially; do not silently discard them |

Use a small host-private execution context with destination root session ID,
executing actor session ID, host run ID, task ID, activation and current tool-call
identity. The web host already has a run ID; the TUI mints one at its owned
run/continuation boundary and resets it on completion. One host run can include
multiple Agent turns. Capture the context at the real executor and verify its
live owning task/call, rather than trusting an inherited context variable or a
mutable reference alone. No public `ChannelSend` origin field is needed.

Resolve the root from that execution context, including designed root Harnesses.
Never route through mutable browser selection or model-supplied origin metadata.
After an approval wait, validate live execution and the destination again.
Switching the selected conversation must not redirect a pending send.

Preserve existing child restrictions: ordinary subagents do not inherit channel
tools/plugins and reject custom tools. Root and actor identities are distinct,
but recording child provenance does not authorize child sends. Enabling sends
from ordinary or designed children requires separate registration, authorization
and persistence work.

Initially expose the built-in web route only in ordinary, externally unowned web
roots. Preserve the existing owner-only catalog and `(channel, conversation)`
dispatch restriction for externally owned roots. Allowing a local web delivery
while inspecting an externally owned chat is a possible later extension requiring
an explicit same-root exception, not removal of the ownership check.
This restricts new sends, not historical viewing: acquiring external ownership
must not hide already committed local deliveries or their accessible assets.

All channel tools continue through normal Harness approval/profile enforcement.
Being first-party does not make a tool approval-exempt. Keep TUI modal and web
dialog decisions in their current host paths; `Channel.approval()` remains an
external-transport recognition hook. Preserve the existing in-chat approval
correlation checks and text-only automatic-reply policy. Built-in adapters must
not masquerade as external connections eligible for automatic replies.

## Durable delivery and media lifecycle

### Journal and ordering

Use a small delivery journal in the existing session database, separate from
model conversation messages. A record contains:

- stable delivery ID and root session ID;
- channel and host-derived root/actor-session/run/task/activation/call identity;
- committed transcript anchor and a monotonic delivery sequence;
- text and file metadata;
- attachment bytes, stored atomically with the delivery.

SQLite BLOB storage is the proposed initial choice: existing bounds keep each
delivery finite and metadata/bytes commit together. Repeated sends can still grow
the database; session deletion must reclaim data, and retention/quota policy is
separate work. A filesystem store would require additional orphan recovery,
atomic publication and backup handling.

Anchor a delivery to the committed assistant tool-call row and the call's position
within that row, not to the later tool-result row. The Agent persists the
assistant row before execution but currently discards the returned row ID at
that call site. An owned session-adapter bridge must capture the committed ID,
scoped to the actual executing task/turn, and hand it to the private context.
Do not guess from the latest message, timestamps, text equality or an unscoped
call ID.

If a custom persistence adapter cannot establish a durable anchor, disable or
reject only built-in local delivery before sending. Ordinary Harness execution,
external connector sends and legacy `Agent.listen()` must remain independent of
the journal/anchor contract.

Compaction retains old messages but advances the history window. Interleave
deliveries whose anchors remain in current context after their respective tool
calls. Present pre-boundary deliveries in a labeled **Deliveries from earlier
context** section, ordered by their original anchors and delivery sequence,
before current-context entries. Never reattach them to a newer call. Delivery
sequence resolves delivery ordering; it does not replace the assistant-row/call
anchor for placement among ordinary messages.

Do not add duplicate assistant messages to model history. The normal tool result
provides the delivery receipt; the journal supplies the UI presentation.

### Send transaction

1. Check the execution-bound destination, supported types and all file bounds.
   Bound file reads and validate actual byte lengths at storage time. The current
   loader's `stat()` followed by `read_bytes()` is not sufficient against a file
   growing between those operations.
2. Reject the entire send if any part is unsupported. In particular, a TUI media
   rejection must not first emit the accompanying text.
3. Commit the complete delivery and its bytes atomically.
4. Publish a small delivery notification and return its persisted ID.

Commit is the delivery boundary. A failed notification after commit does not
erase a successful delivery; retain its successful receipt and let reload recover
the presentation. Cancellation around commit must join/resolve the owned
persistence operation. Report an unknown outcome only when the commit status
genuinely cannot be established; inability to return a receipt does not undo a
known committed delivery or its identity. Do not automatically retry an uncertain send.
Replayed notifications deduplicate by delivery ID; two separate approved sends
remain two deliveries, even if their text is identical.

### Asset access and browser rendering

Add an authenticated endpoint addressed by opaque delivery/asset IDs, scoped to
the root and its current deletion/access state. Never expose arbitrary filesystem
paths or put the web token in a media URL.

The existing API uses `X-Ngn-Token`; native image/player URL requests cannot attach
that header. Initially fetch authenticated bytes into a bounded `Blob`, then use
an object URL for `<img>`, `<audio controls>` or `<video controls>`. Allow `blob:`
only in the relevant image/media CSP directives. Load larger media on demand,
abort stale requests and revoke object URLs on unmount/session change. Provide
filename, type, loading/error state and an explicit download fallback. Do not
autoplay.

Enforce response byte limits during streaming fetch, before constructing the Blob;
checking only after `response.blob()` finishes is insufficient. Bound concurrent
fetches and aggregate retained object-URL bytes, including many small images.

Treat MIME declarations and bytes as untrusted. Restrict the initial allowlist,
serve with the effective content type and `nosniff`, and avoid active formats such
as HTML/SVG. Playback compatibility depends on codecs as well as containers;
capabilities must not promise universal decoding.

Media bytes must not enter tool-result JSON, WebSocket records or `RunReplay`.
Their bounded queues are much smaller than a permitted file. Live events, replay
and cold history carry metadata/references and reconcile using the same delivery
ID. Multiple tabs observe one committed delivery.

Soft deletion revokes asset access; restore reinstates it; permanent purge removes
logical metadata and bytes and makes assets inaccessible. It does not promise
physical SQLite file shrinkage or secure erasure. Cover supported non-web
session-clear/delete paths as well as web trash hooks. Test backup/restore coverage
and media recovery after the original workspace file is changed or deleted.

### TUI rendering

Flush pending assistant text before presenting a distinct explicit-delivery entry.
Keep the compact tool card as execution evidence; do not manufacture additional
assistant text events. Restore deliveries from the journal and render text-safe
attachment notes when viewing compatible shared history. Preserve F6 navigation,
user-controlled disclosures and approval focus restoration from v0.13.4.

## Browser upload/paste: separate input increment

Browser-originated images need a dedicated authenticated, bounded upload path,
opaque session-scoped references and attachment-aware message admission. Include
picker/paste/drop draft previews, removal, explicit submission and image-only
messages. A clipboard event alone must not submit a model turn.

Reuse the existing image/PDF input limits and untrusted-content treatment. Bind
the submitted text and attachment references into durable admission/deduplication;
reusing a message UUID with different attachments must fail. Define staged-upload
expiry and cleanup after cancellation, deletion and abandoned drafts. Uploading a
file does not authorize forwarding it to an external connector.

Check provider modality support before claiming native model input support.
Arbitrary audio/video understanding is not implied by outbound browser playback.

## Nonduplication and lifecycle invariants

1. One UI input admission and one Harness producer per accepted turn.
2. No new `Agent.listen()` call or source worker for a built-in UI adapter.
3. Ordinary assistant text uses only the existing stream/history path.
4. Each explicit send has one persisted delivery identity.
5. Live events, replay and saved history converge on that identity.
6. Delivery notifications never re-enter the model as user input.
7. Root ownership survives selection changes and approval waits; child execution
   cannot accidentally inherit channel tools or expand permissions.
8. Final responses are never automatically broadcast to external channels.

Instructions should explain that normal replies already appear in the active UI;
explicit delivery is useful for media or a specifically requested send. Do not
heuristically suppress equal text: intentionally repeated sends are valid.

## Implementation work packages

Package A is accepted. The other reviewable follow-ups each require their own
acceptance before application changes begin.

| Package | Scope | Dependencies |
| --- | --- | --- |
| [A: dispatch and capabilities (#42)](https://github.com/abi-jey/nagents/issues/42) | Listener-independent dispatcher, additive directional descriptor, catalog context, legacy connector compatibility | None |
| [B: durable delivery journal (#43)](https://github.com/abi-jey/nagents/issues/43) | Atomic text/media storage, trusted anchors, receipts, cancellation, compaction and deletion lifecycle | A's identity/receipt contract |
| [C: TUI channel (#44)](https://github.com/abi-jey/nagents/issues/44) | Real tool registration and text delivery, history restoration, whole-send media rejection, approvals/focus | A, B |
| [D: web channel and playback (#45)](https://github.com/abi-jey/nagents/issues/45) | Real host integration, authenticated assets, transcript reconciliation, image/audio/video UI, designed Harness and ownership regressions | A, B |
| [E: browser upload/paste (#46)](https://github.com/abi-jey/nagents/issues/46) | Staged uploads, attachment-aware admission, composer UX and provider input compatibility | D and shared storage/admission contracts |

The first coherent delivery milestone is **A–D**. Publishing two adapter classes
alone would not demonstrate a working UI channel. E adds the browser-to-agent
media direction independently.

Deferred decisions include externally owned roots' local delivery exception,
cross-process UI routing, range streaming, aggregate storage quotas, screenshot
capture/transcoding, a full inbound `ChannelMessage` conversion and any new
approval exemptions.

## Validation gates

The implementation must prove these with real Harness tool execution and actual
interface tests, not only direct calls to an adapter:

- Legacy channel/listener and third-party connector behavior remains compatible.
- TUI text send appears once, survives reload, and rejects media without partial
  delivery; keyboard/disclosure/approval behavior is retained.
- Web image/audio/video survives restart and workspace-file deletion.
- Reconnect, replay truncation, cold reload and two tabs show one delivery.
- Approval denial creates no delivery; session switching cannot redirect it.
- Cancellation before/around/after commit has defined receipt/outcome behavior.
- Designed root Harnesses route correctly; child origin/ordering remains accurate
  without accidental tool inheritance or newly authorized child sends.
- Existing external ownership, in-chat approval and automatic-reply checks hold.
- Authentication and production CSP permit legitimate media and reject invalid
  asset access; playback failures have a useful fallback.
- Trash/restore/purge and model compaction preserve the defined lifecycle.
- Browser uploads respect limits, retry identity, draft cleanup and provider
  capabilities when package E is implemented.

### Feasibility evidence versus implementation proof

Source inspection establishes the existing host-managed dispatch precedent and
the conflicting ownership of a second listener. A disposable local probe at
`/tmp/opencode/issue40_feasibility_probe.py` ran successfully with the repository's
virtual-environment Python on 2026-09-25:

```bash
.venv/bin/python -B /tmp/opencode/issue40_feasibility_probe.py
```

It exercised the real web `ChannelHost`, its current dispatch reuse and the real
Harness tool executor with a scripted provider and fake sink. Assertions passed
for **one Harness/Agent execution, one explicit delivery and zero channel listener
invocations**. Ordinary assistant text remained separate. Cross-chat and
cross-channel sends from an owned root were rejected, and browser selection did
not replace execution identity. One execution contained two scripted provider
rounds (tool request, then final answer); no model service was called and outbound
network connections were blocked.

This was a disposable probe, not a shipped test or registration API. It directly
attached the sink to host state and used a local approval callback. The actual
TUI/web integration, UI approval flows, durable delivery journal, media pipeline
and reconnect invariants remain proposed and require the implementation gates
above. Production registration must not call `ChannelHost.open()` unchanged,
because that method starts a source listener.
