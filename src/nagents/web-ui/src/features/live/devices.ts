import type { AudioDeviceSelection, LiveTransport } from "./types.js";

export interface AudioDeviceOption { id: string; label: string }
export interface AudioDeviceList {
  inputs: AudioDeviceOption[];
  outputs: AudioDeviceOption[];
  needsPermission: boolean;
}

export const DEFAULT_AUDIO_DEVICES: Readonly<AudioDeviceSelection> = { inputId: "", outputId: "" };
const STORAGE_KEY = "ngn.live.audio-devices.v1";
let unsaved: AudioDeviceSelection | undefined;

function deviceId(value: unknown): string {
  return typeof value === "string" && value.length <= 4096 && value !== "default" ? value : "";
}

export function readAudioDevices(): AudioDeviceSelection {
  if (unsaved) return { ...unsaved };
  try {
    const value: unknown = JSON.parse(globalThis.localStorage.getItem(STORAGE_KEY) || "{}");
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const record = value as Record<string, unknown>;
      return { inputId: deviceId(record.inputId), outputId: deviceId(record.outputId) };
    }
  } catch { /* Unavailable storage or an old/corrupt preference keeps system defaults. */ }
  return { ...DEFAULT_AUDIO_DEVICES };
}

export function saveAudioDevices(selection: AudioDeviceSelection): boolean {
  const next = { inputId: deviceId(selection.inputId), outputId: deviceId(selection.outputId) };
  try {
    globalThis.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    unsaved = undefined;
    return true;
  } catch {
    unsaved = next;
    return false;
  }
}

export function supportsAudioOutput(transport: LiveTransport = "websocket"): boolean {
  const prototype: object | undefined = transport === "webrtc"
    ? globalThis.HTMLMediaElement?.prototype : globalThis.AudioContext?.prototype;
  return !!prototype && "setSinkId" in prototype && typeof prototype.setSinkId === "function";
}

type OutputPicker = MediaDevices & { selectAudioOutput?: () => Promise<MediaDeviceInfo> };

export function canChooseAudioOutput(): boolean {
  return typeof (globalThis.navigator?.mediaDevices as OutputPicker | undefined)?.selectAudioOutput === "function";
}

export async function chooseAudioOutput(): Promise<AudioDeviceOption> {
  const media = globalThis.navigator?.mediaDevices as OutputPicker | undefined;
  if (!media?.selectAudioOutput) throw new Error("Choose a listed speaker or use your system default.");
  const selected = await media.selectAudioOutput();
  return { id: deviceId(selected.deviceId), label: selected.label || "Selected speaker" };
}

export async function listAudioDevices(): Promise<AudioDeviceList> {
  const media = globalThis.navigator?.mediaDevices;
  if (!media?.enumerateDevices) throw new Error("This browser cannot list audio devices. System defaults are still available.");
  const devices = await media.enumerateDevices();
  function options(kind: MediaDeviceKind, label: string): AudioDeviceOption[] {
    const seen = new Set<string>();
    return devices.filter(device => {
      if (device.kind !== kind || !device.deviceId || device.deviceId === "default" || seen.has(device.deviceId)) return false;
      seen.add(device.deviceId); return true;
    }).map((device, index) => ({ id: device.deviceId, label: device.label || `${label} ${index + 1}` }));
  }
  return {
    inputs: options("audioinput", "Microphone"),
    outputs: options("audiooutput", "Speaker"),
    needsPermission: !devices.some(device => device.kind === "audioinput" && device.label),
  };
}

export async function requestAudioDevices(signal?: AbortSignal): Promise<AudioDeviceList> {
  signal?.throwIfAborted();
  const media = globalThis.navigator?.mediaDevices;
  if (!media?.getUserMedia) throw new Error("Microphone access needs a supported browser on localhost or HTTPS.");
  const stream = await media.getUserMedia({ audio: true });
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let abort: (() => void) | undefined;
  try {
    signal?.throwIfAborted();
    return await Promise.race([
      listAudioDevices(),
      new Promise<never>((_resolve, reject) => {
        if (signal) {
          abort = () => reject(new DOMException("Device discovery cancelled", "AbortError"));
          signal.addEventListener("abort", abort, { once: true });
        }
      }),
      new Promise<never>((_resolve, reject) => {
        timeout = setTimeout(() => reject(new Error("Device discovery timed out. Try refreshing the devices.")), 10_000);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
    if (abort) signal?.removeEventListener("abort", abort);
    stream.getTracks().forEach(track => track.stop());
  }
}
