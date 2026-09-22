# GPT-Live examples

Each example focuses on one capability. All use the existing **`Agent.run()` +
`AudioDuplex`** interface; the library owns connections and delegation handling.

| Example | Focus | Try asking |
| --- | --- | --- |
| `hosted.py` | OpenAI-managed Responses backend with web search | “What are the latest OpenAI announcements?” |
| `client.py` | An ordinary Nagents backend agent with its own provider/tools | “What is the current UTC time?” |
| `commentary.py` | Optional commentary tool, a call inside another tool, and application calls | “Check the clock, then explain your answer.” |

## Complete example index

Run each path below as `python examples/live/PATH`; use `--help` for required
arguments. Most microphone cases accept `--duration SECONDS` or run until Ctrl+C.

| Focus | Files |
| --- | --- |
| Hosted model and native custom functions | `hosted.py`, `hosted_tools.py` |
| Client agent or plain application service | `client.py`, `client_service.py` |
| Optional model tool, nested call, application commentary | `commentary.py` |
| Quiet UI context and command acknowledgments/errors | `context_updates.py`, `commands_and_errors.py` |
| Proactive greeting and requested disclosure | `greeting.py`, `disclosure.py` |
| Verified local clip and actual device completion | `verified_disclosure.py disclosure.wav` |
| Timestamped overlapping captions | `captions.py --output captions.jsonl` |
| Model input muting and independent playback control | `input_mute.py`, `playback_controls.py` |
| Application guardrail and buffered candidate approval | `guardrails.py`, `checked_playback.py` |
| Changed requests and stale-result suppression | `task_corrections.py` |
| Exact typed values and backend image analysis | `typed_input.py 'My reference is A0042'`, `images.py picture.png` |
| Sparse runtime backend settings | `backend_configuration.py` |
| Text-only startup history and persisted transcript resume | `history.py history.json`, `history_resume.py` |
| Application-owned work continuing after voice closes | `idle_resume.py` |
| Stored source/fork and recording download | `stored_fork.py --store`, `stored_fork.py --source live_ID`, `recording.py live_ID` |
| Duration, token usage and finalization | `usage_and_close.py --duration 20` |
| PCM rates and raw G.711 media bridge | `audio_formats.py --rate 16000`, `media_bridge.py caller.g711 --codec pcmu` |
| Prompt policies, translation and authorized voices | `prompting.py`, `translation.py --language Spanish`, `custom_voice.py voice_ID` |
| Original-audio inspection independent of transcripts | `audio_detector.py` |
| Backend model/service-tier latency controls | `backend_latency.py` |
| Browser WebRTC with native sideband | `webrtc/server.py` |
| Attach an observer without taking over tool execution | `sideband.py live_ID` |
| Verified inbound SIP, rejection and controlled transfer | `sip/server.py` — see `sip/README.md` |
| Crawl/Walk paced replay and Run caller simulation | `evaluation/replay.py caller.wav`, `evaluation/simulation.py` |
| Integrated persistent application | `appointment_desk/app.py` — see its README |

From the repository root, in your virtual environment:

```bash
python -m pip install -e '.[voice]' python-dotenv
python examples/live/hosted.py
python examples/live/client.py
python examples/live/commentary.py
```

The shared `_support.py` reuses transport-independent adapters and rendering from
`examples/_voice/`; Realtime examples live separately in `examples/realtime/`.
You need PortAudio and microphone/speaker devices supporting mono
PCM16 at 24 kHz. Use headphones; the reference adapters do not do echo cancellation.
Ctrl+C closes the connection. Backend history is stored in `live-sessions.db`.

## Authentication

**`CodexProvider()`** discovers `CODEX_HOME` or `~/.codex`, the selected model
provider, and saved authentication. `.env` is loaded for configured environment-key
references. The example does not parse credentials itself.

Like Codex itself, voice uses an API key: a configured/cached key or its
`OPENAI_API_KEY` fallback. Saved ChatGPT authentication can run the delegated
backend, but cannot authenticate GPT-Live voice. Replace the backend provider
with any normal Nagents provider to run inference elsewhere, including locally.

## Commentary is optional

`commentary.py` explicitly registers `voice.add_comment` as a backend tool. The
backend may choose to call it; no tool is added automatically. The same async
method is callable inside another tool or from application code:

```python
await voice.add_comment("I'm checking availability.")  # Spoken progress/result.
await voice.add_thinking("Thursday is selected; nothing is booked.")  # Quiet context.
await voice.add_instructions("Ask for confirmation before proceeding.")  # Instructions.
```

During delegated work the original delegation ID is attached automatically through
task context. Application calls outside delegated work use `delegation_id: null`.
The backend agent can also call its own methods during an active delegation.
Wait for `session.started` before application updates; calls outside an active
connection fail locally.

These methods return an event ID. Append acknowledgments arrive as raw events
whose `client_event_id` matches that ID. They acknowledge context delivery, **not
audio playback**. Multiple updates may use the same delegation ID. Instructions
do not cancel ongoing work.

Use `await voice.live.wait(event_id)` for commands with acknowledgments. Server
rejections raise `LiveCommandError` for that command while the event stream stays
active. Backend item submission has no separate acknowledgment; follow its
backend events. An observer sideband uses `handle_delegations=False` so it cannot
duplicate the primary application's tool execution. By default the observer
example detaches on exit; `--close-session` explicitly closes the primary too.

## Events and context

The library collects both speakers' transcript fragments and their timestamps.
Delegation events contain metadata, not task text. A serialized worker passes
recent transcript context to the backend, retaining prior task/tool results in
its own conversation while microphone input and playback continue.

**Every backend event bubbles up as it happens**, preserving its type and adding
`extra["source"] == "live_backend"` and `extra["delegation_id"]`. This includes
text, reasoning, tools, errors, compaction, usage, and completion. A tagged
`DoneEvent` finishes the backend task, not the live conversation. Debug events are
not automatically spoken; explicit updates and final results enter voice context.

New speech does not retry or replay tools. When application intent changes, call
`voice.live.invalidate_tasks()`; obsolete client results are then suppressed.
Application confirmation/revision checks still belong inside state-changing tools.
The backend sees the latest 512 transcript fragments and its own task history.
The runtime uses bounded queues and a configurable `backend_timeout` (120 seconds
by default). Public context appends follow the API's 500-token limit, with server
rejections surfaced through command outcomes. Automatic final results are split
into conservative UTF-8 chunks without silently discarding the answer. Backend
events use `extra["application_id"]` when triggered by typed application input.

Live lifecycle/control traffic is `LiveEvent`, separate from Realtime events.
`voice.live.status` retains the session ID, latest cumulative duration, context
ratio, terminal reason and confirmed/unconfirmed finalization. Changing voice or
delegation mode means a new session. Resume and fork are explicit application
operations; the library does not blindly reconnect and replay side effects.

## Using the larger examples

```bash
python examples/live/webrtc/server.py --port 3000
python examples/live/appointment_desk/app.py --port 3000 --mode client
# Alternative hosted backend, with opt-in remote storage:
python examples/live/appointment_desk/app.py --port 3000 --mode responses --store
```

Run one server at a time, then open `http://localhost:3000`. Browser media requires
microphone permission; project access is needed for Live, storage and custom
voices where used. SIP additionally needs project webhook/trunk setup. Examples
which call real models incur their normal API usage; simulation runs two voice
models. The servers bind to loopback and use a local demo token/origin check;
provide real user authentication before exposing them publicly.

The disclosure example reports observed wording **once** and never treats that
as proof of audio delivery. The verified-clip example needs a previously checked
mono PCM16/24kHz WAV and a drain-capable output device. Browser clip completion is
observed in the browser player. Neither an instruction acknowledgment nor a saved
server recording proves what the caller actually heard.

The guardrail, candidate-playback and acoustic-meter examples deliberately use
narrow demonstration rules; they do not claim a general classifier, full-answer
segmentation, or answering-machine detection. The APIs expose the underlying
audio, controls and events so an application can supply those decisions.

## References

- [GPT-Live](https://developers.openai.com/api/docs/guides/live)
- [Client delegation and updates](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client)
- [WebSocket protocol](https://developers.openai.com/api/docs/guides/voice-websockets?api=live)
