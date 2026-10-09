# Deep resonance — React revision 4

This standalone React demo preserves the revision 3 network, mixed listening/activity states, randomized routing, directional impacts, delegation events, and seven density levels. It uses the web client's existing React, TypeScript, and Vite dependencies.

From `src/nagents/web-ui`:

```sh
npm run demo:voice
npm run demo:voice:build
npm run demo:voice:test
```

The reusable implementation lives in `../../src/components/voiceSphere/`. The demo re-exports that same code, so its rendering and interfaces remain identical to the live UI.

The build writes only `dist/voice-concepts/react-v4/` at the repository root. It does not replace the production web UI. The bundled sample is synthetic speech generated locally with Flite; microphone capture starts only from the demo's button.

## Audio boundary: PCM bytes and streams

Import the public API from `./src/index`. Create one transport per voice connection. Input and output are independent bounded queues, and the renderer derives loudness and frequency bands internally.

```ts
import { createPcmAudio } from "./src/index";

const audio = createPcmAudio({
  inputFormat: { encoding: "s16le", sampleRate: 24000, channels: 1 },
  outputFormat: { encoding: "s16le", sampleRate: 24000, channels: 1 },
  clock: () => playbackContext.currentTime,
  maxBufferedSeconds: 2,
});

// at is the first sample's time on that same clock, in seconds.
audio.input.write(microphoneBytes, { at: captureTime });
audio.output.write(assistantBytes, { at: scheduledPlaybackStart });

// A live ReadableStream<Uint8Array> can pipe directly into either channel.
// Keep the channel open when the stream represents only one utterance.
await outputStream.pipeTo(audio.output.writable, {
  signal: abortController.signal,
  preventClose: true,
});

audio.output.reset(); // interruption/seek: discard queued output immediately
audio.dispose();      // disconnect: release both channels
```

Formats support interleaved mono/stereo, `s16le` or `f32le`, and sample rates from 8–192 kHz. Arbitrarily split PCM frames are assembled across writes. Continuation bytes for a partial frame omit `at` or repeat that partial frame’s timestamp. Bytes are copied, so callers can reuse or transfer their buffers after `write` returns.

The host owns playback. Schedule visual output against its playback clock, rather than the network arrival time. Omitting `at` appends at the later of now or the previous block's end. Overlapping timestamps discard the already accepted prefix. The renderer analyzes the trailing 32 ms and never consumes future samples early.

`maxBufferedSeconds` must be finite, greater than zero, and at most 60. Queues are bounded visual telemetry: writes do not wait for playback, and overflow drops the oldest frames. Feed live/paced chunks or select capacity for the intended scheduling horizon; inspect `channel.stats()` for dropped frames and pending bytes. `close()` rejects further writes while scheduled samples drain; `reset()` clears buffered data and overlap history but does not reopen a closed stream. Create a new transport for a new connection.

`channel.consume(readable, { signal })` is a convenience reader. EOF closes that channel; cancellation/error discards buffered data. With `pipeTo`, its standard `preventClose` option allows multiple utterances on one channel. The transport itself never requests a microphone, plays audio, or opens a network connection.

## React view and typed events

```tsx
import { useRef } from "react";
import {
  VoiceSphere,
  type ActivityMode,
  type PcmAudio,
  type VoiceSphereHandle,
} from "./src/index";

export function ConversationSphere({ audio, mode }: {
  audio: PcmAudio;
  mode: ActivityMode;
}) {
  const sphere = useRef<VoiceSphereHandle>(null);
  return <>
    <VoiceSphere ref={sphere} audio={audio} mode={mode}
      density={3} style={{ width: 280, height: 280 }} />
    <button onClick={() => sphere.current?.dispatch({
      type: "delegation-start", id: "task-42", label: "Research",
    })}>Start task</button>
  </>;
}
```

Activities are `idle`, `thinking`, `speaking`, and `delegating`. Microphone audio is an independent layer and can accompany these activities. `running`, `density`, `speed`, `thinkingSpeed`, `luminance`, `color`, `dark`, and `reducedMotion` are controlled props.

The lifecycle modes `connecting` and `error` suppress audio reactions and ambient activity packets. `connecting` sweeps light through the existing network; `error` keeps a quiet, rose-colored network. Neither mode rebuilds the graph or discards delegation identities.

In the Live composer, a rotating connection outline appears immediately during permission/provider/audio setup, and an interrupted outline marks failure; reduced motion uses static outlines. Listening begins only after the server audio socket is attached and the microphone capture graph is running with a live, enabled, unmuted track. An intentional microphone mute permits a connected speaker-only session; the host must retain its muted indicator and suppress input reactions.

The handle accepts these commands:

```ts
sphere.current?.dispatch({ type: "pulse", node: 12 }); // node is optional
sphere.current?.dispatch({ type: "delegation-start", id: "task-42", label: "Research" });
sphere.current?.dispatch({ type: "delegation-result", id: "task-42" });
sphere.current?.dispatch({ type: "delegation-finished", id: "task-42", outcome: "failed" });
sphere.current?.dispatch({ type: "delegations-reset" });
sphere.current?.dispatch({ type: "view-reset" });
```

`dispatch` returns whether the command was accepted. Duplicate starts are rejected for every displayed task and the 128 most recently retired task IDs; applications own any longer-lived business deduplication. Duplicate results and unknown results are rejected. Results received during launch are queued and animate after launch finishes. Density changes preserve task identities and pending results. `delegation-finished` accepts `failed` or `cancelled`, clears pending motion and queued success results, and frees the slot without animating a successful return. Three visual task slots are available; an additional start returns `false`. Explicit reset clears tasks and remembered IDs.

The Live UI bridge retains all active/unacknowledged tasks and 64 settled records per voice session. Evicting settled records advances a sequence watermark, so old queued/working snapshots cannot restart forgotten work. Already-known active tasks can still settle from later lifecycle records below that watermark. A new voice session or engine generation clears the watermark.

`rotate`, `nextNode`, `zoomBy`, and `getSnapshot` are also available. `onSnapshot` publishes animation metadata at most ten times per second; imperative commands also publish immediately. Frame-level audio and canvas drawing do not rerender the React tree every frame.

For synchronized large and compact views, call `useVoiceSphere(options)` once and pass its controller to multiple `<VoiceSphereCanvas controller={controller} compact />` views. The demo uses this arrangement. Each separately created controller has isolated simulation state and lifecycle cleanup.

## Module boundaries

The following renderer/transport modules are shared with the production web UI; only the page and media demo adapter stay in this directory.

- `VoiceSphere.tsx` and `useVoiceSphere.ts`: React views, controls, frame scheduling, observers, and cleanup.
- `audioStreams.ts`: raw byte queues, timing, stream ingestion, and signal analysis.
- `engine.ts` and `geometry.ts`: typed canvas simulation with no DOM controls, audio capture, or application globals.
- `useDemoAudio.ts`: optional demo adapter for native playback and microphone capture; it writes actual float PCM bytes through the same queues.
- `App.tsx` and `styles.css`: demo-only controls and page styling. The exported sphere does not inject global styles.

Tests cover PCM framing/timing/overflow, audio lifecycle, React StrictMode cleanup and isolated instances, topology, mixed modes, and queued delegation results. Microphone tests use synthetic streams and mocks, not hardware capture.
