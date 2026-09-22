# GPT-Live: guide-based feature map and implementation plan

Research date: 2026-09-21. Status: **implementation approved; examples and native support being verified**.

The runnable index is now in [examples/live/README.md](../../examples/live/README.md).
The feature map below records the design and its acceptance criteria.

This map comes from visiting OpenAI's guides in the shared browser, following
relevant navigation, in-page links, delegation/migration selections, and linked
partner guides. The SDK-source research is deliberately not used as the feature
inventory. Twilio is excluded from this plan.

## 1. Scope and principles

- Keep GPT-Live and Realtime as separate paradigms and packages.
- Keep the normal Nagents entry point: configure the provider, agent and
  `AudioDuplex`, then consume `agent.run()`.
- Implement protocol behavior, resource ownership, state, errors and observable
  events in the library. Examples should demonstrate a feature rather than
  implement their own WebSocket/delegation runtime.
- A client backend can be an ordinary Nagents `Agent`, or application-owned work
  that does not need an LLM. Preserve its provider, tools, instructions and state.
- Commentary is optional. Show explicit tool registration, a direct call inside
  another tool, and a programmatic application call. Do not inject tools.
- Bubble backend events as they happen, with origin, delegation, backend-run and
  session correlation. Backend completion is distinct from voice-session closure.
- OpenAI's Live guides describe the API. Partner guides describe their adapters;
  an adapter limitation is not automatically a Live limitation.
- Shared audio formats, device adapters and playback primitives can be reused.
  Realtime turn/commit/cancel semantics must not leak into Live.

## 2. Important findings from the guides

1. **Continuous audio, not alternating model turns.** Live has no equivalent of
   Realtime's authoritative per-response audio-done event. A transcript gap is
   not proof of silence or completed playback. [Migration], [Sessions]
2. **Delegation metadata is not the task.** The client receives an ID and timing,
   and supplies transcript context plus application state. A notification may
   arrive before enough speech has been transcribed. Retain it until the request
   can be resolved rather than permanently consuming it with an empty context.
   [Delegation], [Text migration]
3. **An acknowledgment is not delivery.** Match `client_event_id`, preserve
   append timing, and separately observe generated audio and actual playback.
   [Sessions], [Controls]
4. **Speech interruption and task cancellation are different.** A user saying
   “stop talking” must not implicitly replay or cancel a booking. Application
   revisions and operation records decide what happens to work. [Prompting],
   [Migration]
5. **Backend work can outlive voice.** The guide explicitly describes closing
   voice during a long job and resuming later. Voice cleanup cannot always mean
   destroying the application-owned worker. [Costs], [Sessions]
6. **Hosted function calls need a complete tool loop.** Collect completed items
   from nested `response.output_item.done`; completion snapshots can have
   `output: []`. Return every required function result before continuing.
   [Delegation], [Migration]
7. **Not every `error` ends the session.** Correlate rejected commands and keep
   receiving; distinguish command failure, moderation, transport failure and
   terminal `session.closed`. [Sessions]
8. **Live manages its voice context.** It summarizes older conversation and can
   replace the voice engine within the same session near the context limit.
   Authoritative task records remain in the application. This is separate from
   a Nagents backend agent's compaction. [Sessions]
9. **The same HTTP and WebSocket assumptions do not fit every transport.**
   WebRTC starts through HTTP and carries media on tracks; sideband attaches to
   an already-running session; SIP negotiates media. [WebRTC], [Controls], [SIP]
10. **Voice duration and backend tokens are different usage streams.** Replace
    cumulative duration snapshots, deduplicate backend responses, and preserve
    confirmed versus unconfirmed final usage. [Costs]

## 3. Feature map

Each row requires actual library behavior, a focused example, and tests of the
named success and failure paths. Example names below are targets, not a claim
that all files already exist.

| Feature | Required native support | Focused example(s) |
| --- | --- | --- |
| Primary WebSocket and full duplex | `session.start` first; wait for `session.started`; continuous paced input and concurrent output/control; ordered audio, sample alignment, bounded queues and cleanup. | `hosted.py`, `audio_formats.py` |
| Audio formats | PCM16 mono at 16/24 kHz; PCMU/PCMA at 8 kHz where documented; validate input/output compatibility. Selecting a format must not pretend to resample bytes. | `audio_formats.py`, `media_bridge.py` |
| Hosted Responses inference | Independently selected backend model, instructions and supported settings; hosted web search; nested event and usage handling. | `hosted.py` |
| Hosted custom functions | Reuse Nagents tool definitions/executor; correlate delegation, response and call IDs; collect completed calls, return every result, continue once; handle duplicates, denied calls and failed results. | `hosted_tools.py` |
| Client-agent inference | Supply both-speaker transcript context and application task state to a normal `Agent`; preserve backend history; stream all backend events; return verified results. | `client.py` |
| Client service/workflow | Allow application-owned work without requiring another model. Support deferred context readiness, streaming updates and explicit outcome/error reporting. | `client_service.py` |
| Optional commentary | `session.commentary.append`; automatic ID binding inside delegated work and explicit/general context outside it; multiple updates per task; no implicit tool injection. | `commentary.py` — model tool, nested tool, application call |
| Quiet context and UI state | `session.thinking.append`; update selections and facts directly from application state; coalesce rapid changes and skip identical updates. Keep typed data distinct from trusted instructions. | `context_updates.py` |
| Instructions and proactive greeting | `session.instructions.append`; after startup, request greeting language and immediate speech while input audio continues, including silence. | `greeting.py` |
| Requested disclosure | Request exact wording using instructions, not paraphrasable commentary. Track command acceptance, generated wording/audio and playback separately. | `disclosure.py` |
| Verified disclosure playback | Gate model output, discard stale queued audio, play an application-verified recording/rendered clip and observe player completion; keep input flowing. | `verified_disclosure.py` |
| Command acknowledgments | Correlate IDs, acknowledgments and errors; support awaiting an outcome without blocking the event reader/audio; expose timeout and pending-on-close outcomes. | `commands_and_errors.py` |
| Transcripts and captions | Preserve exact fragments, speaker, `start_ms` and `end_ms`; support overlap and late fragments; provide application-defined grouping without inventing server turn boundaries. | `captions.py` |
| Input muting | Send mute/unmute and observe their acknowledgments. Distinguish server input muting from stopping local recording; keep output and backend work independent. | `input_mute.py` |
| Output/playback control | Drop/mute model playback, flush queues and resume under application control; expose playout state separately from generation. Sideband alone cannot mute the primary player's output. | `playback_controls.py` |
| Guardrails and speech checks | Allow concurrent transcript checks, action blocking, task cancellation where supported, and instruction-based redirection. For pre-playback checks, support buffering, candidate segments and rejecting stale approvals. | `guardrails.py`, `checked_playback.py` |
| Corrections and task versions | Accept application task revisions; clear outdated confirmations; avoid duplicate work; suppress obsolete result delivery. Do not equate every transcript delta with a new business revision. | `task_corrections.py` |
| Cancellation and uncertain outcomes | Distinguish requested cancellation, confirmed cancellation, completed action and unknown outcome. Preserve records across retries; an interrupted local task does not prove an external action stopped. | `task_corrections.py` |
| Startup history and current context | Encode supported text-only startup messages and limits; preserve trusted instructions separately. Expose context utilization without confusing voice-context management with backend compaction. | `history.py` |
| Typed input | Route typed values/corrections to the active backend task. In hosted mode, use supported backend input items and continue only after pending function results are satisfied. | `typed_input.py` |
| Images/screens | Route image plus context to a vision-capable backend; return relevant text. Do not send images to the Live audio frontend or its text-only startup history. | `images.py` |
| Runtime backend configuration | Sparse `session.update` for supported Responses settings; retain settings until acknowledged. Frontend model, original prompt, voice, audio format and delegation mode require their documented startup semantics. | `backend_configuration.py` |
| Persistence and reconnect | Save application task state, context and session identity; recover uncertain operations before retries. Seed a replacement session or fork a finalized stored recording; never blindly replay side effects. | `history_resume.py` |
| Idle close with continuing work | Keep application-owned backend jobs alive when requested; save status; close voice; resume from verified state. If locally waking on speech, buffer opening audio through setup. | `idle_resume.py` |
| Stored sessions and fork | Opt-in storage; wait for finalization; fork on a new connection with supported overrides and a new ID; preserve inherited immutable configuration. | `stored_fork.py` |
| Recording download | Stream the documented stereo WAV recording after finalization; distinguish server recording from audio actually played by the application. Handle unavailable/expired/non-stored recordings. | `recording.py` |
| Usage and close | Maintain latest voice seconds, context ratio, per-response backend usage and terminal reason. Drain through `session.closed`; report incomplete finalization if it never arrives. | `usage_and_close.py` |
| Browser WebRTC | Native server-side session creation and returned SDP/session ID; browser media tracks and data channel; no second `session.start` or WebSocket-style audio on the data channel. | `webrtc/` |
| Sideband | Attach by opaque session ID without restarting; observe transcripts/delegations/reflected audio; send controls; select exactly one application owner for actions observed on multiple connections. | `sideband.py` |
| Direct SIP | Incoming webhook verification/deduplication, accept/reject, sideband, DTMF notifications, transfer/hangup and finalization; media is negotiated separately. | `sip/` |
| Carrier/media bridge | Keep carrier call IDs distinct from Live IDs; relay matching codecs, ordered audio and termination with player ownership. Direct Live SIP is inbound; do not invent a Live outbound-create endpoint. | `media_bridge.py`, optional Telnyx integration |
| Language/style/translation | Separate voice persona and backend procedure; exercise backchannels, interruptions, pronunciation, clarification and translation-only prompting. These are prompt behaviors, not imaginary wire commands. | `prompting.py`, `translation.py` |
| Named/custom voices | Named voice or authorized voice ID at creation; appropriate access/startup error handling; change by creating a new session. Keep voice asset creation separate from conversation execution. | `custom_voice.py` |
| Audio-sensitive decisions | Expose received/reflected audio to an application detector when transcript text is insufficient. A separate Realtime detector can be connected without merging the two protocols. | `audio_detector.py` |
| Backend performance | Reuse clients/connections and stable prompt/tool prefixes; expose supported model/reasoning/service-tier controls; allow independent work and coherent progress/results. Keep standalone Responses WebSocket features on that backend API. | `backend_latency.py` |
| Evaluation | Paced replay, continuous simulated calls, structured event/audio/task evidence and independent usage accounting. Test actual state and playout, not just plausible speech. | `evaluation/` |

### Supported update semantics

The library should expose the concrete append operations and their outcomes:

| Operation | Meaning | What success does not prove |
| --- | --- | --- |
| Commentary | Text the voice model may paraphrase aloud | Exact wording or completed playback |
| Thinking | Factual context/quiet progress; may influence later replies | Secrecy from the caller |
| Instructions | Application-authored behavioral guidance | Backend cancellation or retraction of played audio |
| Mute input | Stop input reaching the model according to the command | Stopped recording, stopped speech, or stopped tasks |
| Tool result | Evidence about the executed operation | The caller heard its confirmation |
| Session closed | Terminal session snapshot, reason and final usage | A required external business action succeeded |

Use the documented **500-token** append contract. Replace the draft's
480-byte-only policy and silent fallback replacement with explicit validation,
errors and application-chosen coherent updates. Determine tokenizer behavior
from applicable API documentation; do not advertise a byte heuristic as an exact
token count.

## 4. Ownership and state

| Owner | Responsibilities |
| --- | --- |
| Live service | Continuous voice behavior, voice context management, session configuration and authoritative server acknowledgments/final usage |
| Native Live runtime | Protocol validation, connection/lifecycle, command correlation, audio flow, transcript records, delegation IDs, event propagation, bounded resource ownership |
| Backend Agent/workflow | Its provider, prompt, tools/executor, conversation history, compaction and emitted inference/tool events |
| Application | Authorization, confirmations, business records, operation IDs, meaningful task revisions, stale-result policy, retries/reconciliation and playback-delivery decisions |
| Audio player/media path | Queued versus played audio, interruption, output gating, clip playback and completion evidence |

State changes must be observable. In particular, keep these identities distinct:
voice session ID, delegation ID, backend run/session ID, hosted response ID,
tool call ID, application operation ID, task revision and carrier call ID.

### Disclosure handling

Maintain independent evidence rather than a single `disclosure_done` flag:

- Instruction sent, with its event ID.
- Instruction accepted or rejected, correlated to that ID.
- Generated disclosure audio/wording observed and checked.
- Playback completed, interrupted, discarded or unknown.
- Application delivery decision based on the required evidence.

These observations can arrive at different times; do not require a fabricated
strict event ordering. For exact wording and a known completion point, use the
verified-clip example and the actual player's completion signal. The voice input
continues while the application controls output.

### Delegation handling

- Retain notices while sufficient transcript/task context is unavailable.
- Stream backend events immediately; forwarding diagnostics is separate from
  choosing what enters the voice model's context.
- Keep tool execution off the audio/event reader.
- Register commentary tools explicitly; the application can also call updates
  directly without an LLM choosing a tool.
- Use application revisions before state-changing tools and before delivering
  old results. A result check alone cannot undo an action already executed.
- Expose keep-running versus cancel/join policies when voice closes. Preserve
  task state independently of an ephemeral socket and its delegation IDs.

## 5. Separate package and example layout

Target separation (the implementation uses a shared `audio.py` module for the
existing primitives rather than expanding it into a package):

```text
src/nagents/
  audio/                 shared formats, duplex, devices and playback primitives
  realtime/              Realtime configuration, protocol and runtime
  live/                  GPT-Live configuration, protocol, controls and runtime
    config.py
    events.py
    connection.py
    controls.py
    delegation.py
    api.py               WebRTC/SIP/fork/recording HTTP operations
    runtime.py           Agent.run integration

examples/
  realtime/              existing Realtime examples
  live/
    hosted.py
    hosted_tools.py
    client.py
    client_service.py
    commentary.py
    ...                  focused cases from the map above
    webrtc/
    sip/
    evaluation/
    appointment_desk/    integrated application
```

Keep top-level `AudioDuplex` compatibility. Live should have its own lifecycle/raw
event types instead of labelling Live traffic `RealtimeRawEvent`. Share genuinely
transport-independent audio/events where appropriate, not command names or
session semantics. Remove the draft example helper's path dependency on
`examples/realtime-live` when separating the directories.

Focused examples should generally be small enough to read in one sitting. The
integrated example can be a multi-file application; no giant all-in-one script
and no duplicated protocol engine hidden in example helpers.

## 6. Integrated example: appointment desk

Build a runnable appointment desk with a real local SQLite appointment store and
an application event timeline. Its tools actually read and change that store;
spoken success must agree with committed records.

### User journey

1. Connect by desktop microphone or browser; optionally accept an inbound SIP
   call using the same backend and application state.
2. Select language and voice at startup; greet and deliver the configured
   disclosure through the requested or verified-playback path.
3. Load known customer/task context. Accept a spoken request, typed appointment
   reference, or image of an appointment card through the appropriate backend.
4. Check availability while the conversation continues. Emit optional commentary
   from a model tool, directly from a lookup tool, and from the application UI.
5. Let the caller correct a date during the lookup. Advance the task revision,
   clear an old confirmation, and avoid announcing or executing obsolete work.
6. Collect the required confirmation and perform one idempotent booking/change.
   Return verified state only after the transaction succeeds.
7. Demonstrate blocked/unauthorized actions, clarification, failed tools, late
   results and unknown outcomes without claiming an action was cancelled or done
   merely because speech stopped.
8. Close voice during a long application-owned task and resume with its state.
   Offer history-seeded restart and stored-fork scenarios where available.
9. Close cleanly, preserve voice/backend usage separately and optionally download
   a finalized recording. Inspect business state, transcript and playback traces.
10. Run fixed regression scenarios and recorded/simulated conversation trials
    against the same store/tools.

Hosted and client delegation are selectable application modes with shared domain
tools and scenarios. They require separate session configurations; the example
must not pretend a running session can change delegation mode. Likewise, WebRTC,
primary WebSocket and direct SIP are alternative media paths rather than three
simultaneous primary connections for one demo call.

## 7. Initial draft snapshot, before the example expansion

The current slice has primary PCM24 WebSocket audio, hosted web search, native
client-Agent delegation, optional context methods, scoped commentary calls,
backend event bubbling, local Codex discovery and streaming console output.
Its latest reported checks passed 2,524 Python tests with four skips, plus
pre-commit on tracked and new files. These checks validate that slice, not the
complete guide map.

Before presenting full Live support, address at least:

- Live/Realtime directory and event separation.
- Command outcome tracking; nonterminal errors currently terminate too broadly.
- Backend function-call execution/continuation for hosted delegation.
- Deferred task context, application revisions, explicit result-delivery policy
  and configurable backend lifetime beyond voice closure.
- Real playback control and disclosure evidence; device write is not playout.
- Full documented audio formats, startup history and supported configuration.
- WebRTC, sideband, SIP, fork, recording and recovery operations.
- Structured final usage and incomplete-finalization reporting.
- Replace arbitrary draft limits/defaults with documented validation or explicit
  configurable policy, particularly the byte cap, fragment window and timeout.

## 8. Implementation sequence and acceptance criteria

1. **Separate and stabilize the protocol foundation.** Split packages/examples;
   add Live-specific events, command correlation, lifecycle states, documented
   configuration and audio formats. Test startup ordering, error recovery,
   pending commands on close, malformed events, format mismatch and queue limits.
2. **Finish delegation support.** Complete hosted tool batches and client
   Agent/workflow ownership, context readiness, task revisions and event
   propagation. Test slow/parallel tools, duplicate events, stale results,
   cancellation, commentary scopes and backend continuation without voice.
3. **Implement conversation and playback controls.** Add greeting, requested
   disclosure, verified clip playback, input muting, context/UI updates and
   guardrail paths. Verify output gating while input/event processing continues.
4. **Implement transports and persistence.** WebRTC provisioning, sideband,
   SIP controls/webhooks, history restart, fork and recording download. Test
   duplicate deliveries, action ownership, missing stored recordings and lost
   final events. Run live transport checks separately from offline mocks.
5. **Build the integrated application and evaluations.** Use the same domain
   tools in focused examples, integration scenarios and replay/simulation tests.
   Measure task success and actual audible response/playback separately.

Every completed feature gets a short example and protocol/state tests before the
next phase relies on it. Continue using pytest-xdist and required pre-commit.

### Evaluation coverage

- **Crawl:** controlled synthetic request, fixed initial state and expected tools.
- **Walk:** approved real recording, paced audio, noise/echo/packet-loss variants.
- **Run:** independent simulated caller with continuous full-duplex audio.
- Check final application state, tool arguments/counts, delegation decisions,
  corrections, authorization, spoken confirmation and playback independently.
- Distinguish infrastructure failures from valid runs where the agent fails.
- Record first useful audible result separately from progress acknowledgments.
- Keep caller/evaluation usage separate from application usage; report repeated
  trials rather than treating one successful conversation as a benchmark.

## 9. Source differences and questions to resolve

- OpenAI documents supported Responses backend instruction updates. LiveKit's
  guide restricts such changes in its adapter; implement the OpenAI behavior.
- OpenAI's delegation guide describes function tools and web search for the
  managed backend. LiveKit additionally lists provider tools. Do not assume
  those additional tools are native Live capabilities without confirmation.
- The Telnyx guide retains an alpha header in its tested configuration. Treat
  this as an integration/project compatibility question, not a universal default.
- LiveKit's reply timeouts, turn segmentation and history handling are adapter
  policies, not server events or universal Live limits.
- Sideband playback limitations are fundamental to media ownership. An attached
  control socket cannot promise to stop audio in a browser or direct SIP leg.
- Confirm precise request fields and limits in the browser-visible API reference
  as each operation is implemented; do not infer missing behavior from an SDK
  union or copy standalone Responses options into Live's smaller command surface.

## 10. Browser source inventory

### Core Live navigation and selectable views

- [Getting started with GPT-Live][Live]
- [Prompting][Prompting], including expanded optional controls: response length,
  language/pronunciation, translation, noise/silence, selected requests,
  clarification and result reuse.
- [Managing sessions][Sessions]: startup/history, all context channels, captions,
  mute/unmute, greeting/disclosure, context management, forks, recordings,
  idle/resume, errors/moderation and close/usage.
- [Delegation and tools][Delegation]: **both Responses and client selections**,
  custom functions, direct updates, typed/image input and latency patterns.
- [Migration][Migration]: **both Realtime and text-agent/chained selections**,
  including audio-sensitive decisions and intervention checks.
- [Partner integrations][Partners], with the remaining partner guides below.

### Shared voice menu pages, with Live selected where applicable

- [WebSockets][WebSockets]
- [WebRTC][WebRTC]
- [Telephony and SIP][SIP]
- [Server-side controls][Controls]
- [Audio and voice overview][Audio]
- [Voice agents][Voice agents]
- [Custom voices][Custom voices], including the GPT-Live-specific section.
- [Cost optimization][Costs]

### Relevant linked material

- [Voice-agent evaluation][Evaluation]: Crawl, Walk, Run and task/audio evidence.
- [GPT-Live model][Model]: modalities, pricing and concurrent-session limits.
- [Data controls][Data]: Live storage, fork/download retention and ZDR behavior.
- [Webhooks][Webhooks]: signatures, retries, acknowledgment and deduplication.
- [Images and vision][Vision]: backend image input, not frontend vision.
- [Responses WebSocket mode][Responses WS]: backend connection reuse/warmup;
  its response-chain and multiplexing features are not Live commands.
- [Latency optimization][Latency] and [Prompt caching][Caching]: relevant backend
  design and measurements, not assumed extra Live configuration fields.
- [Text to speech][TTS] and [File transcription][STT]: related clip preparation
  and audio-verification workflows, separate from the Live conversation API.
- [LiveKit GPT-Live guide][LiveKit]
- [Telnyx direct-SIP guide][Telnyx]
- [Pipecat GPT-Live guide][Pipecat]

[Live]: https://developers.openai.com/api/docs/guides/live
[Prompting]: https://developers.openai.com/api/docs/guides/live-prompting
[Sessions]: https://developers.openai.com/api/docs/guides/live-conversations
[Delegation]: https://developers.openai.com/api/docs/guides/live-delegation
[Migration]: https://developers.openai.com/api/docs/guides/live-migration
[Text migration]: https://developers.openai.com/api/docs/guides/live-migration?migration-path=text-agent
[Partners]: https://developers.openai.com/api/docs/guides/live-partner-integrations
[WebSockets]: https://developers.openai.com/api/docs/guides/voice-websockets?api=live
[WebRTC]: https://developers.openai.com/api/docs/guides/voice-webrtc?api=live
[SIP]: https://developers.openai.com/api/docs/guides/voice-sip?api=live
[Controls]: https://developers.openai.com/api/docs/guides/voice-server-controls?api=live
[Audio]: https://developers.openai.com/api/docs/guides/audio
[Voice agents]: https://developers.openai.com/api/docs/guides/voice-agents
[Custom voices]: https://developers.openai.com/api/docs/guides/custom-voices
[Costs]: https://developers.openai.com/api/docs/guides/voice-latency-cost?api=live
[Evaluation]: https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation
[Model]: https://developers.openai.com/api/docs/models/gpt-live-1
[Data]: https://developers.openai.com/api/docs/guides/your-data#v1livesessions
[Webhooks]: https://developers.openai.com/api/docs/guides/webhooks
[Vision]: https://developers.openai.com/api/docs/guides/images-vision
[Responses WS]: https://developers.openai.com/api/docs/guides/websocket-mode
[Latency]: https://developers.openai.com/api/docs/guides/latency-optimization
[Caching]: https://developers.openai.com/api/docs/guides/prompt-caching
[TTS]: https://developers.openai.com/api/docs/guides/text-to-speech
[STT]: https://developers.openai.com/api/docs/guides/speech-to-text
[LiveKit]: https://docs.livekit.io/agents/models/realtime/plugins/gpt-live/
[Telnyx]: https://developers.telnyx.com/docs/voice/sip-trunking/gpt-live-configuration-guide
[Pipecat]: https://docs.pipecat.ai/api-reference/server/services/s2s/openai-live
