# Browser voice E2E capture

This harness sends known spoken audio through the real browser microphone
capture worklet and ngn WebSocket, then records the real rendered speaker graph
after the playback worklet and volume control. It runs only when explicitly
installed into an isolated Chrome DevTools MCP tab; the production app imports
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
normal capture worklet converts its real `MediaStream` to PCM frames. Speaker
output feeds a silent virtual sink, so it cannot feed back into a physical mic.
The recorder receives actual post-volume audio; it does not construct a WAV by
concatenating incoming WebSocket data.

`play()` first arms speaker PCM capture and awaits an AudioWorklet acknowledgement
with the actual starting audio frame. Only then does it schedule the known
utterance 250 ms later. The PCM measurement window therefore begins before the
test utterance, rather than at connection creation. Pre-arm render-clock changes
remain in the trace as `phase: "warmup"` events; they are not misrepresented as
recorded speech. Every discontinuity after arming remains a validation failure.

Observe real captions and, in authenticated mode, the delegation status via
snapshots. Open Request & events through normal clicks if inspecting a failure.
Use `() => globalThis.__voiceE2E.status()` for bounded progress reads. Add a
timestamped operator marker with `() => globalThis.__voiceE2E.mark("opened request inspector")`.
Allow the spoken answer to finish, then end voice via the UI. Finally run:

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
The first output frame must match the recorder's start acknowledgement. Warmup
clock anomalies remain visible separately. Every measured gap, overlapping
block, or post-arm clock jump fails continuity, including jumps inside a partially
accumulated block. No missing samples are silently removed or declared continuous.
Older captures without an explicit measurement-start acknowledgement do not pass
the current measurement contract; retain them as historical evidence.

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
  --min-rounds 3 --min-active-span-seconds 300 --max-buffered-ms 250
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

Real-time output limits each local reservoir to 240 ms and explicitly discards
stale audio only on overload. This prevents a local queue from accumulating
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

Audio-thread frame positions are authoritative for waveform continuity.
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
```
