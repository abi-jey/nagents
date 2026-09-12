import { WavEncoder } from "./pcm.js";
import type { DictationConfig, Recording, RecordingCallbacks } from "./types.js";

export type AudioEnvironment = {
  getStream: () => Promise<MediaStream>;
  createContext: (sampleRate?: number) => AudioContext;
  createNode: (context: AudioContext, maxFrames: number) => AudioWorkletNode;
  workletUrl: string;
};

// The environment is injectable: tests exercise lifecycle and final-chunk ordering
// without a microphone, AudioContext, or provider request.
export function createRecording(
  config: DictationConfig,
  callbacks: RecordingCallbacks,
  environment: AudioEnvironment,
): Recording {
  let ended = false;
  let started = false;
  let stopping = false;
  let limited = false;
  let context: AudioContext | undefined;
  let stream: MediaStream | undefined;
  let source: MediaStreamAudioSourceNode | undefined;
  let node: AudioWorkletNode | undefined;
  let silence: GainNode | undefined;
  let encoder: WavEncoder | undefined;
  let durationTimer: ReturnType<typeof setTimeout> | undefined;
  let stopTimer: ReturnType<typeof setTimeout> | undefined;

  function cleanup() {
    clearTimeout(durationTimer);
    clearTimeout(stopTimer);
    if (node) {
      node.port.onmessage = null;
      node.port.onmessageerror = null;
      node.onprocessorerror = null;
      node.port.close();
      node.disconnect();
    }
    source?.disconnect();
    silence?.disconnect();
    for (const track of stream?.getTracks() || []) {
      track.removeEventListener("ended", interrupted);
      track.stop();
    }
    if (context) {
      context.onstatechange = null;
      void context.close().catch(() => undefined);
    }
    node = undefined;
    source = undefined;
    silence = undefined;
    stream = undefined;
    context = undefined;
    encoder = undefined;
  }

  function fail(cause: unknown) {
    if (ended) return;
    ended = true;
    cleanup();
    callbacks.failed(cause);
  }
  function interrupted() {
    fail(new Error("Microphone recording was interrupted. Your draft is kept; record again when ready."));
  }

  function running() {
    if (ended || !context) return false;
    if (context.state === "running") return true;
    interrupted();
    return false;
  }

  function stop(atLimit = false) {
    if (ended || stopping || !node) return;
    stopping = true;
    limited = atLimit;
    clearTimeout(durationTimer);
    // FIFO port messages deliver the final partial buffer before the acknowledgement.
    stopTimer = setTimeout(() => fail(new Error("Could not finish recording. Audio was discarded.")), 2000);
    try { node.port.postMessage({ type: "stop" }); }
    catch (cause) { fail(cause); }
  }

  async function start() {
    if (started || ended) return;
    started = true;
    try {
      try { context = environment.createContext(16000); }
      catch (cause) {
        if (!(cause instanceof Error) || cause.name !== "NotSupportedError") throw cause;
        context = environment.createContext();
      }
      encoder = new WavEncoder(context.sampleRate, config.max_seconds, config.max_bytes);
      // Observe before any async startup work. An initially suspended context may
      // await its one user-initiated resume, but losing running state is terminal.
      let wasRunning = context.state === "running";
      context.onstatechange = () => {
        if (ended || !context) return;
        if (context.state === "running") wasRunning = true;
        else if (wasRunning || context.state !== "suspended") interrupted();
      };
      // Resume in the user gesture, before awaiting a potentially unanswered prompt.
      const resumed = context.resume().then(() => {
        if (running()) wasRunning = true;
      });
      void resumed.catch(fail);
      const acquired = await environment.getStream();
      if (ended) {
        for (const track of acquired.getTracks()) track.stop();
        return;
      }
      stream = acquired;
      for (const track of stream.getTracks()) track.addEventListener("ended", interrupted);
      await resumed;
      if (ended || !context) return;
      if (!running()) return;
      await context.audioWorklet.addModule(environment.workletUrl);
      if (ended || !context) return;
      if (!running()) return;
      source = context.createMediaStreamSource(stream);
      node = environment.createNode(context, context.sampleRate * config.max_seconds);
      silence = context.createGain();
      silence.gain.value = 0;
      node.port.onmessage = ({ data }: MessageEvent<unknown>) => {
        if (ended || !encoder) return;
        try {
          if (!data || typeof data !== "object" || !("type" in data))
            throw new Error("Invalid audio capture response.");
          if (data.type === "samples" && "samples" in data && data.samples instanceof Float32Array) {
            encoder.push(data.samples);
          } else if (data.type === "stopped" && "limited" in data && typeof data.limited === "boolean") {
            const blob = encoder.finish();
            const result = { blob, seconds: (blob.size - 44) / 32000, limited: limited || data.limited };
            ended = true;
            cleanup();
            callbacks.stopped(result);
          } else throw new Error("Invalid audio capture response.");
        } catch (cause) { fail(cause); }
      };
      node.port.onmessageerror = interrupted;
      node.onprocessorerror = interrupted;
      source.connect(node);
      node.connect(silence);
      silence.connect(context.destination);
      if (!running()) return;
      durationTimer = setTimeout(() => stop(true), config.max_seconds * 1000);
    } catch (cause) { fail(cause); }
  }

  return {
    start,
    stop: () => stop(),
    cancel: () => {
      if (ended) return;
      ended = true;
      cleanup();
    },
  };
}
