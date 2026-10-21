# Browser voice E2E capture

This harness sends known spoken audio through the real browser microphone
capture worklet and ngn WebSocket, then records the real rendered speaker graph
after the playback worklet and volume control. It runs only when explicitly
installed into an isolated Chrome CDP context; the production app imports
none of these files.

Two modes serve different evidence:

- **Offline** uses the shipped ngn frontend and real HTTP/WebSocket transport
  with an explicitly scripted server response. It verifies microphone capture,
  network pacing, playback, controls, captions, recording, and gap detection.
  Captions are fixture text, not speech recognition. It does not exercise a
  provider, ngn's provider relay, or actual delegation.
- **Authenticated** runs the normal ngn application and provider configuration.
  Browser audio goes only to ngn; ngn connects to GPT Live using its selected
  Codex login. No public Realtime API or API-key substitute is used. The fixture
  requests real delegated work and may incur account usage. Only this mode can
  establish evidence about actual delegation, provider failures, or the
  Processing label during a real run.

Authenticated tests can also use the optional deployment bridge described below
to exercise the real public ingress and deployed ngn process.

## Prepare and launch

### Automated loopback CDP runner

For repeatable runs, use `cdp_runner.py`. It attaches to Chrome's loopback CDP
endpoint, creates one disposable browser context and page, clicks the ordinary
voice controls with native CDP mouse input, and closes only that owned context.
It launches and stops its own local server unless `--url` points at another
loopback server exposing the same private E2E routes. It never uses
Playwright/Selenium, browser storage from an existing profile, or physical audio
devices.

Prerequisites are the repository `.venv`, FFmpeg with Flite, the built frontend,
and Chrome already listening on `127.0.0.1:9222`. Keep the scenario, config,
workspace, and artifact directory private. The offline runner isolates
`XDG_CONFIG_HOME` and `XDG_DATA_HOME` inside the artifact directory; it makes
no provider calls. The runner captures transcripts and audio PCM, so its output
directory is created with mode `0700`; JSON, logs, and screenshots use `0600`.

Offline baseline should exit `0`:

```bash
.venv/bin/python -m examples.live.browser_e2e.cdp_runner \
  --mode offline \
  --artifacts /tmp/ngn-voice-cdp-baseline \
  --workspace /tmp/ngn-voice-cdp-baseline-workspace
```

An injected 200 ms audio gap without an expected-fault flag should exit `1`:

```bash
.venv/bin/python -m examples.live.browser_e2e.cdp_runner \
  --mode offline --gap-ms 200 \
  --artifacts /tmp/ngn-voice-cdp-gap-fails \
  --workspace /tmp/ngn-voice-cdp-gap-fails-workspace
```

The same fault as an explicit negative control should exit `0` because the
checker confirms the gap was detected:

```bash
.venv/bin/python -m examples.live.browser_e2e.cdp_runner \
  --mode offline --gap-ms 200 --expect-gap-ms 200 \
  --artifacts /tmp/ngn-voice-cdp-gap-control \
  --workspace /tmp/ngn-voice-cdp-gap-control-workspace
```

Authenticated mode is opt-in and requires a private local ngn config, a
separate workspace, and a scenario JSON. The example scenario runs six
conversation turns, holds the live session across five minutes, and requires
real tool and delegation evidence; the runner writes the deterministic
read-only sentinel file into that test workspace. Use a config that explicitly
allows the selected tool and delegate target. It does not infer credentials or
fall back to offline mode:

For an isolated starting configuration, copy `config.example.json`,
`providers.example.json`, and `assistant.example.md` together into a private
directory and use the copied config below. They select normal local Codex
credential discovery, enable Live, and define the read-only `helper` requested
by the scenario. No credentials are copied.
Set `XDG_CONFIG_HOME` and `XDG_DATA_HOME` to separate, private test directories
to avoid editing your normal ngn preferences; leave `CODEX_HOME` pointing to
your existing signed-in Codex installation. Alternatively use your own local
config and adjust the scenario's helper/tool names to match it.

```bash
XDG_CONFIG_HOME=/path/to/private/isolated-config \
XDG_DATA_HOME=/path/to/private/isolated-data \
.venv/bin/python -m examples.live.browser_e2e.cdp_runner \
  --mode authenticated \
  --scenario-json examples/live/browser_e2e/authenticated-scenario.example.json \
  --config /path/to/private/ngn-config.json \
  --workspace /path/to/private/e2e-workspace \
  --artifacts /path/to/private/e2e-artifacts
```

The report records the scenario/round IDs, mode, package and source revision,
source-module hashes, and the hashes of same-origin JS/CSS build assets. A
failure leaves private status, the full bounded capture when available,
timestamped server logs, cleanup/finalization status, and a screenshot.
Re-run the same command after each relevant source change; use a fresh
workspace/artifact path to avoid reusing stale fixture state.

When the capture includes the complete received-PCM ledger, `artifacts.py
unpack` also writes `speaker-received.wav` at mono 24 kHz. This is the exact
network PCM presented to the playback worklet, before playback DSP. The
unpacker validates packet sequence, offsets, byte/sample totals, omissions, and
the byte bound before exporting it. Incomplete captures produce no received
WAV and are marked `receivedPcm.status: incomplete`; older v2 captures that
predate this channel are marked `not-captured` and are not replayable. The
human-readable `trace.json` keeps packet metadata but strips `pcm16` from
input, output, and received channels, avoiding a second copy of the audio.

The runner requires authenticated scenario rounds to include expected spoken
text and actual call progress. `require_delegation` means the voice handoff
reported `queued`, `working`, and `completed`; `require_subagent` separately
requires a native child task to start and complete. Tool lifecycle events are
read from the app's `/api/events` socket for the subscribed session, and only
safe IDs/names/statuses are retained. Arguments, tool results, prompts, and
token-bearing WebSocket subprotocols are not copied to the event trace.

Before sending a fixture the driver waits for the normal UI's connected state.
It advances only after the expected final phrase, required work completion,
and three seconds of rendered silence with a bounded playback queue. Live RTP
can continue sending silence, so an empty queue is not required. This settling
rule is a heuristic, not a provider turn-final acknowledgment: use concise
answers with the expected phrase at the end, and inspect the timeline if a
provider pauses longer in mid-answer. `--max-seconds` bounds the whole test's
measurement window; it does not change the application's agent run policy.

Underruns are not treated as proof of a glitch by themselves. The report
correlates the audio-thread underrun timestamp with captured non-silent output
PCM, while queue-empty periods remain explicitly ambiguous between provider
DTX/natural pauses and starvation. A passing report confirms the configured
PCM continuity, drop, response, and rendered-speech age gates; it cannot prove what a
person would perceive through a physical speaker.

The report keeps the existing all-queued packet-age metric as a diagnostic and
gates on `graphRenderedSpeechAge` per minute. The isolated E2E playback node opts
into a bounded numeric worklet trace: every flushed source-coordinate interval is
batched and acknowledged independently of the visual played reports. The trace
contains no PCM. Trace intervals split at discontinuous raw browser render-clock
labels, preserving those observations without stretching the PCM timeline across
them. The checker validates batch sequence, omissions, source
coordinates, then correlates voiced received PCM with actual post-volume
recorder PCM within one output quantum. Before the first fixture, the runner
checkpoints the graph after it drains preloaded warmup audio. This gives the
measured window an exact source-sample boundary even when the first packet is
not quantum-aligned. Before normal UI End, it requests a second checkpoint that
snapshots accepted source samples and drains the trace through the matching
rendered coordinate. The graph gate uses only this complete atomic prefix;
capture continues through normal close/finalization, and later received or
rendered PCM remains visible as post-measurement tail data. Stale periodic health
samples cannot substitute for either checkpoint. It reports packet
arrival-to-software-render-graph age and detects added gaps between voiced source
samples. Missing intervals, incomplete receive/output PCM, silent or unrelated
output, dropped samples, and added gaps over 40 ms during short source pauses
fail closed. Overlapping half-interval windows must also match at the same alignment,
reducing the chance that a loud section hides a muted or reordered segment. These
checks do not certify arbitrary sub-window glitches or perceptual smoothness. It does not require a
fixed amplitude threshold on each short span, so valid low-level tails remain
verifiable without hiding coordinate holes. A gap is compared against source
sample time: a 61 ms source pause rendered as 78 ms adds 17 ms and passes, while
the same pause stretched to 209 ms fails. Source pauses over 200 ms and packet
gaps without corresponding source silence remain visible as long-pause or
unclassifiable-input-pause diagnostics; they are not silently discarded or
claimed as measured smoothness.

Raw underrun-near-speech classifications remain in the report. The complete
trace can override those proximity-only checks only when received PCM, trace
coordinates, final counters, and post-volume waveform matching all validate and
the source-aware gap/latency bounds pass. Missing trace data cannot override a
counter failure. A long packet gap without source-silence samples is
unclassifiable and fails the smoothness gate.

The separately reported cached `getOutputTimestamp()` estimate maps context time
to output-device presentation time. It is diagnostic only: in one measured
Chrome/software-sink run it differed from software-graph render time by about
27 ms. That offset is not acoustic latency, and the
required `sinkId: none` has no physical speaker presentation to measure. The
default gate therefore uses graph time. Neither metric measures provider
generation time or what a person hears through physical hardware.

### Manual Chrome DevTools MCP procedure

Use the repository virtual environment. Build the frontend first with
`npm --prefix src/nagents/web-ui run build`. Keep generated audio, init scripts, and traces outside Git.

```bash
umask 077
poetry run python examples/live/browser_e2e/artifacts.py prepare /tmp/ngn-voice-e2e
poetry run python examples/live/browser_e2e/server.py \
  --fixtures /tmp/ngn-voice-e2e --workspace /tmp/ngn-voice-e2e/workspace --port 8876
```

Preparation uses FFmpeg's local Flite synthesizer. There is no TTS network call.
If unavailable, supply `--wav caller.wav --reply-wav reply.wav` with mono
PCM16/24 kHz recordings, each at most 30 seconds. Set `--text` to the exact
caller transcript. The default caller asks the main assistant to calculate
17 × 23 and explain the result. `fixture.json` retains text and SHA-256 hashes.

An offline fault run adds `--gap-ms 200`, inserting one real 200 ms gap between
server audio sends. Run baseline and fault cases in separate output directories
and fresh isolated tabs. The fault is server-side; browser worklet events and
health reports are never fabricated.

For a full run, prepare with `--mode authenticated` and launch the server with
`--authenticated --workspace /path/to/isolated/configured/workspace`. This uses
`load_config` and the normal app's authentication/settings. Select the intended
Codex-backed connection in the UI if needed. Pass `--config /path/to/test.json`
to load an explicit test configuration; project configuration is not implicitly
trusted. Session state always uses the test workspace's `voice-e2e-state`
directory, keeping it separate from another ngn server. Arrange an isolated workspace,
data/configuration, and idle test session before launching: the harness does
not clone or modify the user's provider settings, credentials, existing chats,
or Chrome startup flags. The extra asset routes serve the recorder worklet and
caller WAV. The same-origin worklet respects the application's CSP, which
forbids blob worklet modules. The small init script downloads the WAV separately
and verifies its SHA-256 before decoding it.

## Chrome DevTools MCP procedure

Follow the repository [shared-browser skill](../../../.agents/skills/shared-browser/SKILL.md)
for the configured shared Chrome connection. Never replace browser flags or
close existing user tabs. Create one dedicated context with `new_page`:

```json
{"url":"about:blank","isolatedContext":"ngn-voice-e2e-baseline","background":true}
```

Read `/tmp/ngn-voice-e2e/init.js` and pass its complete contents to
`navigate_page` as `initScript`, with the returned `pageId`, `type: "url"`, and
`url: "http://127.0.0.1:8876"`. This installs instrumentation before app code.
Use the MCP `take_snapshot` and `click` tools to open/start voice normally.
Keep the test tab foreground while measuring; background throttling is a
separate scenario. The virtual microphone and virtual speaker require no
physical devices or shared-browser fake-device flags.

Once the UI says Live, run `evaluate_script` on that explicit page ID:

```javascript
async () => {
  const status = globalThis.__voiceE2E.status();
  if (!status.microphone) throw new Error("Start voice through the UI first");
  return await globalThis.__voiceE2E.play();
}
```

The virtual microphone keeps sending silence before/after the scheduled phrase;
it does not call app internals or send invented VAD/turn/response events. The
normal capture worklet converts its real `MediaStream` to PCM frames. Both
microphone and speaker AudioContexts use the browser's software sink
`sinkId: { type: "none" }`, verified through `context.sinkId.type`. This avoids
depending on the host's physical output-device clock. Unsupported browsers fail
explicitly; there is no fallback to a muted physical device. The trace records
`audio.virtual-sink` for each role. Physical speaker selection is unavailable
during this dedicated test. Speaker output cannot feed back into a physical mic.
The recorder receives actual post-volume audio; it does not construct a WAV by
concatenating incoming WebSocket data.

`play()` first arms speaker PCM capture and awaits an AudioWorklet acknowledgement
with the actual starting audio frame. Only then does it schedule the known
utterance 250 ms later. The PCM measurement window therefore begins before the
test utterance, rather than at connection creation. Pre-arm render-clock changes
remain in the trace as `phase: "warmup"` events; they are not misrepresented as
recorded speech. Raw clock changes after arming remain explicit diagnostics;
missing or reordered PCM fails the independent sample-counter checks.

Observe real captions and, in authenticated mode, the delegation status via
snapshots. Open Request & events through normal clicks if inspecting a failure.
Use `() => globalThis.__voiceE2E.status()` for bounded progress reads. Add a
timestamped operator marker with `() => globalThis.__voiceE2E.mark("opened request inspector")`.
Allow the spoken answer to finish, then end voice via the UI. Wait for the UI's
ended state and, for authenticated runs, **Finalization confirmed**. Poll
`__voiceE2E.status()` until `voiceClosed` and `providerFinalized` are true;
offline scripted runs require only `voiceClosed`. A click on End is not proof
that the asynchronous close/finalization completed. Only then run:

```javascript
async () => {
  await globalThis.__voiceE2E.stop();
  return globalThis.__voiceE2E.export();
}
```

Set the DevTools `evaluate_script` tool's `filePath` to
`/tmp/ngn-voice-e2e/capture.json` to save the potentially large result rather
than printing it into conversation. The unpacker accepts the raw returned object,
pretty JSON, fenced DevTools text, and MCP content/result envelopes:

```bash
poetry run python examples/live/browser_e2e/artifacts.py unpack \
  /tmp/ngn-voice-e2e/capture.json /tmp/ngn-voice-e2e/result
```

The unpacker always preserves received PCM in `speaker-callback-order-*.wav`,
including failed captures. Those files use message delivery order, without
sorting samples or inserting silence; they do not establish timing continuity.
It writes `speaker-rendered-*.wav` only when processed sample counters, block
sequence, and actual PCM are continuous. Raw browser clock observations stay in
`trace.json`, separately from sample continuity.

Validate the original capture with a command that exits nonzero on failure:

```bash
# Baseline: exactly one known response, no interruption before its final sample.
poetry run python examples/live/browser_e2e/artifacts.py check \
  /tmp/ngn-voice-e2e/capture.json --mode offline \
  --reply-wav /tmp/ngn-voice-e2e/reply.wav --expect-text "three hundred and ninety one"

# Negative control: this must fail the baseline check and pass this expectation.
poetry run python examples/live/browser_e2e/artifacts.py check \
  /tmp/ngn-voice-e2e-gap/capture.json --mode offline \
  --reply-wav /tmp/ngn-voice-e2e-gap/reply.wav --expect-gap-ms 200

# Full path: additionally requires queued/working/completed real delegation,
# provider finalization, and the expected spoken transcript phrase.
poetry run python examples/live/browser_e2e/artifacts.py check \
  /tmp/ngn-voice-e2e-auth/capture.json --mode authenticated \
  --expect-text "three ninety-one"
```

All checks require actual PCM in both directions, stopped capture, closed voice,
playback health, no runtime errors/dropped samples, and recorder continuity.
The v2 recorder numbers every delivered block and every captured sample from
zero, independently of the browser's `currentFrame` labels. Start/stop
acknowledgments must match the first frame, total samples, and total block count.
Missing, duplicate, reordered, truncated, or overlapping data fails validation;
no missing samples are silently repaired. Older captures without these independent
counters cannot pass the current contract; retain them as historical evidence.

`frame` follows processed samples anchored at recording start; `sampleOffset`
and `sequence` establish data continuity. `observedFrame` preserves the raw
browser frame label, and every raw clock jump remains in the trace and
`renderClockConsistent` diagnostic. A raw clock jump alone does not establish
PCM loss: Chromium's [worklet clock update](https://github.com/chromium/chromium/blob/main/third_party/blink/renderer/modules/webaudio/base_audio_context.cc)
can skip updating the global frame when the graph lock is unavailable. An
independent known-ramp calibration reproduced paired label jumps with no lost or
reordered source samples. The recorder therefore does not sort or repair PCM
using those labels. Playback starvation, dropped samples, network gaps, and
context timing remain separate observations; this does not measure the physical
speaker clock or guarantee perceptual smoothness.

The offline checker uses the known response sample count, packet intervals,
and playback-health counters. One queue exhaustion after the complete response
is terminal EOF. Earlier underruns or a packet gap within the response fail the
baseline; the negative control requires both an early underrun and the expected
network interruption. Natural pauses in the waveform are never failures by
themselves. This fixed-response policy is not applied blindly to live providers.

Stop the owned test server and close only the test tab/context when finished.
The harness stops recording after 120 seconds **from arming**; setup time does
not consume the measurement window. It does not silently end the application's
voice session. Use `prepare --max-seconds 180` for two delegated
requests in one voice session, and call
`play()` again only after the first result has finished speaking. Mark each round
with `mark("round1")`/`mark("round2")`. End the session explicitly through the UI.
The test wrapper flushes the recorder and waits for its stop acknowledgement
before the application's AudioContext actually closes. The acknowledgement's
final frame must equal the last captured block's end, including a partial tail.
An absent acknowledgement or missing tail fails validation; a timed sleep is
never used as proof that capture has finished.

## Deployment and long-session tests

`deployed.py` is an explicitly launched test bridge bound only to
`127.0.0.1`. It serves the recorder and caller WAV locally, while streaming all
other allowed ngn requests to one HTTPS origin configured at startup. This
loads the deployment's actual HTML, JavaScript, WebSocket, backend, and selected
server credentials. Nothing is installed into the pod or production frontend.
The local same-origin assets are necessary because ngn's CSP disallows blob
AudioWorklet modules; a direct injected script alone cannot load the recorder.

```bash
umask 077
poetry run python examples/live/browser_e2e/artifacts.py prepare \
  /tmp/ngn-voice-e2e-deployed --mode authenticated --max-seconds 660
poetry run python -m examples.live.browser_e2e.deployed \
  --origin https://ngn.example.net --fixtures /tmp/ngn-voice-e2e-deployed --port 8880
```

Use the same Chrome DevTools procedure, navigating the owned isolated tab to
`http://127.0.0.1:8880` with the generated `init.js`. The bridge rejects other
Host/Origin values, foreign fetch metadata, non-ngn paths, and redirects. It
does not forward browser cookies/Authorization or use ambient proxy credentials.
The existing ngn process token is forwarded only to the configured HTTPS origin;
it is never logged. If the deployment requires separate cookie-based ingress
authentication, this bridge deliberately does not bypass or import it.

**This is the real deployment and its real session storage.** Check that it is
idle, create a clearly named test session through the normal UI, and select no
saved context. Do not select an existing user conversation. It uses the
deployment's normal approvals and may incur provider usage. End voice explicitly,
retain the artifacts privately, then remove only the owned test session if
appropriate. Do not delete or reset production state to prepare a test.

Run at least three spoken turns spanning five minutes or longer. Call `play()`
only after the previous response has finished speaking, mark each round, and
place the last utterance at least 300 seconds after the first. A successful
first answer followed by several minutes of silence does **not** pass the
long-session contract:

```bash
poetry run python examples/live/browser_e2e/artifacts.py check \
  /tmp/ngn-voice-e2e-deployed/capture.json --mode authenticated \
  --min-rounds 3 --min-active-span-seconds 300 --max-buffered-ms 320
```

Capture windows are capped at 900 seconds, with bounded event counts. Full PCM
for a long session can produce a large private artifact; save the export to a
file rather than rendering it in the MCP conversation. Minute summaries expose
queue depth, target reserve, underrun/drop deltas and receive intervals. They
also align known-utterance ends with the first rendered audible output before
the next utterance. This is observed timing, not proof that a particular output
belongs to a particular delegation.

For a normal authenticated run, also check the matching backend's final
`Live audio output timing` record: `dropped_samples=0` and `recoveries=0`.
Browser counters alone cannot establish this because a server recovery uses an
ordered playback interrupt. The browser's separate `interruptedSamples` counter
includes queued audio cleared by interruptions, mute, and closing; correlate it
with those actions rather than treating every reset as an audio fault.

Real-time output limits the server reservoir to 240 ms and browser playback to
320 ms, and explicitly discards stale audio only on overload. The browser learns
up to 280 ms of reserve and replenishes it during sustained digital silence,
preserving all queued samples and the existing speech playback rate. This
prevents a local queue from accumulating
seconds of delay; it is not an end-to-end latency guarantee for provider,
network, kernel, or physical speaker buffering. Recovery fault tests must report
their discarded samples separately from normal runs, which require zero drops.

While no samples are dropped/reset and only one socket/epoch is present, the
analyzer maps cumulative consumed samples back to their received packet. New
builds' audio-thread `renderedAt` timestamp excludes delayed main-thread health
delivery; older builds fall back to callback time and say so. This estimates
browser receive-to-consumption age, not provider generation time. A server-side
backlog can exist while the browser queue stays small, so correlate these
metrics with pod RTP/relay/output timings. The bridge adds a loopback hop;
its final numeric send/receive wait summary records that hop's peak forwarding
cost. A clean bridged run does not prove a phone's physical speaker is clean.

## Evidence and interpretation

Artifacts contain:

- `microphone-transmitted.wav`: actual browser worklet PCM sent over ngn's
  socket, including transmitted silence. Packet arrival times live in the trace.
- `speaker-rendered-N.wav`: real rendered mono speaker samples at the browser's
  native sample rate; missing recorder frames are explicitly zero-filled and
  counted, never silently concatenated away.
- `trace.json`: monotonic `performance.now()` and wall `Date.now()` for observed
  traffic, UI state, captions, delegations, errors, and controls; audio render
  frame positions and sample rates; context clock snapshots; playback-worklet
  health; main-thread stalls. No auth headers or token-bearing socket protocols
  are recorded. Transcript/error text and voice audio are retained intentionally.
- Browser user agent, viewport, visibility transitions, and loaded application
  script paths are captured automatically. The public bootstrap/DOM does not
  expose a server revision, so retain that revision alongside the artifacts.
- `metrics.json`: input counts, recorder discontinuities, playback health,
  main-thread stalls, and digital-silence candidates of at least 40 ms inside
  speech. Offline `server-events.jsonl` adds server monotonic/wall timestamps,
  microphone RMS, and each planned/sent response frame.

Processed-sample offsets and block sequences establish recorder data continuity.
Raw `observedFrame` labels remain clock diagnostics, not proof of PCM loss.
Callback timestamps describe main-thread delivery and may lag rendering.
Browser and server monotonic clocks have different origins; correlate with
wall time and corresponding socket events, not by subtracting them directly.
This records the virtual speaker graph, not physical DAC/acoustic output.

Compare waveform gaps with real worklet `underruns`, `droppedSamples`, queue
depth, input/output packet timing, delegation/UI transitions, and server logs.
Natural speech has silence: a silence candidate alone is not proof of a glitch.
Missing recorder blocks invalidate a clean-continuity claim for that interval. A scripted
baseline cannot prove the authenticated provider/delegation path works.

For a repeatable report, retain fixture hashes, browser/server versions, build
revision, mode, foreground/background state, expected arithmetic result (391),
actual caption/result, connection errors, and all four artifact types. A
successful full run requires nonzero transmitted speech, nonzero rendered
assistant audio, observed real delegation completion, and the expected result;
a clean WebSocket connection by itself is insufficient.

The checker and recorder have synthetic regression tests covering terminal EOF,
injected interruptions, missing bytes, delegation/finalization requirements,
enveloped exports, and render-clock jumps inside an accumulated worklet block:

```bash
poetry run pytest tests/test_browser_voice_e2e.py
node --test examples/live/browser_e2e/capture-worklet.test.mjs
node --test examples/live/browser_e2e/browser-init.test.mjs
```

## Private received-PCM replay proposal

The review-only received-PCM prototype captures each accepted binary packet from the live audio WebSocket before the app transfers its original `ArrayBuffer` to the playback worklet. It copies the bytes immediately, but defers base64 encoding until export to keep the message callback lightweight. Captured blocks retain packet order, measurement-relative and transport-global sample counts, 24 kHz PCM16 format, the active playback epoch/realtime mode, output-context time, and monotonic/wall receipt clocks. `playback.submit` records numeric metadata when that same buffer reaches the worklet port; a received packet without a matching submission is not treated as played or replayable. `playback.control` records only reset/barrier/mode metadata so offline replay can reproduce epoch changes.

This is decrypted user/provider audio. Treat it like the existing microphone and rendered-speaker PCM: artifact directory mode `0700`, JSON/audio files mode `0600`, isolated disposable browser context, and software sinks only. The packet payload is stored only under the new `received` array; no tokens, socket protocols, transcripts, or payloads enter logs or browser storage. The copy is limited to `min(30,000,000 bytes, 24,000 samples/s × 2 × maxSeconds)`; invalid frames and data beyond the limit increment omission counters and make exact replay fail closed. The runner retains its 256 MB export bound.

A private trace can be replayed through the production playback worklet without a provider call:

```bash
umask 077
node examples/live/browser_e2e/replay-received.mjs /private/path/capture.json > /private/path/replay-summary.json
chmod 600 /private/path/replay-summary.json
```

The offline Node VM uses the recorded 48 kHz capture origin, port-submission audio times, 128-frame quanta, captured playback controls, and health/played acknowledgements. It emits a bounded numeric health series alongside counters, underruns, drops, queue/target maxima, and output signal statistics; it writes no audio. Replay models when the app submitted PCM, but excludes MessagePort scheduling delay and pre-capture buffered playback state. It isolates playback DSP from browser/network scheduling and physical output, so it cannot establish whether a captured packet gap was intentional provider DTX or a transport stall.
