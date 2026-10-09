/** Output PCM crosses to the audio thread; UI work never sets packet start times. */
export type PlaybackHealth = {
  receivedSamples: number;
  playedSamples: number;
  underruns: number;
  droppedSamples: number;
  bufferedMs: number;
  peakBufferedMs: number;
  targetMs: number;
  sampleRate: number;
  correctionPpm: number;
};
const fields = ["receivedSamples", "playedSamples", "underruns", "droppedSamples", "bufferedMs", "peakBufferedMs", "targetMs", "sampleRate", "correctionPpm"] as const;
export interface PcmPlayback {
  readonly node: AudioWorkletNode;
  write(buffer: ArrayBuffer): void;
  reset(): void;
  fenceReports(): void;
  mute(muted: boolean): void;
  health(): PlaybackHealth;
  close(): void;
}

export function pcmPlayback(context: AudioContext, played: (bytes: Uint8Array, at: number) => void, failed: () => void): PcmPlayback {
  const node = new AudioWorkletNode(context, "ngn-live-playback", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
  const volume = context.createGain();
  node.connect(volume).connect(context.destination);
  let closed = false, muted = false, epoch = 0;
  let stats: PlaybackHealth = { receivedSamples: 0, playedSamples: 0, underruns: 0, droppedSamples: 0,
    bufferedMs: 0, peakBufferedMs: 0, targetMs: 40, sampleRate: context.sampleRate, correctionPpm: 0 };
  node.onprocessorerror = () => { if (!closed) failed(); };
  node.port.onmessage = ({ data }: MessageEvent<unknown>) => {
    if (closed || !data || typeof data !== "object") return;
    const value = data as Record<string, unknown>;
    if (value.epoch !== epoch) return;
    if (value.type === "played") node.port.postMessage({ type: "played-ack", epoch });
    if (value.type === "played" && !muted && context.state === "running" && value.buffer instanceof ArrayBuffer &&
        value.buffer.byteLength > 0 && value.buffer.byteLength <= 960 && !(value.buffer.byteLength % 2) &&
        typeof value.at === "number" && Number.isFinite(value.at) && value.at >= 0 && value.at <= context.currentTime + .05) {
      played(new Uint8Array(value.buffer), value.at);
    } else if (value.type === "health" && fields.every(field => typeof value[field] === "number" && Number.isFinite(value[field]))) {
      const next = { ...stats };
      for (const field of fields) next[field] = value[field] as number;
      stats = next;
      node.port.postMessage({ type: "health-ack", epoch });
    }
  };
  const reset = () => {
    if (closed) return;
    epoch++;
    stats = { ...stats, bufferedMs: 0 };
    node.port.postMessage({ type: "reset", epoch });
  };
  return {
    node,
    // Transfer ownership; the worklet copies into its fixed-capacity sample ring.
    write(buffer) { if (!closed && !muted && context.state === "running") node.port.postMessage({ type: "pcm", epoch, buffer }, [buffer]); },
    reset,
    fenceReports() { if (!closed) { epoch++; node.port.postMessage({ type: "barrier", epoch }); } },
    mute(value) { muted = value; volume.gain.value = value ? 0 : 1; reset(); },
    health: () => ({ ...stats }),
    close() {
      if (closed) return;
      reset(); closed = true; volume.gain.value = 0;
      node.port.onmessage = null; node.onprocessorerror = null;
      node.disconnect(); volume.disconnect(); node.port.close();
      // Fixed numeric counters only: no PCM, transcript, provider or identifiers.
      if (stats.receivedSamples) console.info("ngn voice playback health", { ...stats });
    },
  };
}
