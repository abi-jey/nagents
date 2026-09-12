import { createRecording, type AudioEnvironment } from "./recorder.js";
import type { DictationConfig, RecordingCallbacks } from "./types.js";

export function recordingSupport(): string {
  if (!globalThis.isSecureContext) return "Microphone access needs HTTPS or localhost. You can still type a message.";
  if (!globalThis.navigator?.mediaDevices?.getUserMedia ||
      typeof globalThis.AudioContext !== "function" ||
      typeof globalThis.AudioWorkletNode !== "function" ||
      !("audioWorklet" in AudioContext.prototype))
    return "This browser does not support microphone dictation. You can still type a message.";
  return "";
}

export function browserRecording(config: DictationConfig, callbacks: RecordingCallbacks) {
  const environment: AudioEnvironment = {
    getStream: () => navigator.mediaDevices.getUserMedia({ audio: true }),
    createContext: (sampleRate) => new AudioContext(sampleRate ? { sampleRate } : {}),
    createNode: (context, maxFrames) => new AudioWorkletNode(context, "ngn-pcm-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: { maxFrames },
    }),
    workletUrl: new URL("./pcm-worklet.js", import.meta.url).href,
  };
  return createRecording(config, callbacks, environment);
}
