import assert from "node:assert/strict";
import test from "node:test";
import { readAudioDevices, requestAudioDevices, saveAudioDevices } from "./devices.js";

test("system defaults are unpinned and corrupt or legacy device preferences recover safely", t => {
  let stored: string | null = null;
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: { getItem: () => stored, setItem: (_key: string, value: string) => { stored = value; } } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, "localStorage", previous); else Reflect.deleteProperty(globalThis, "localStorage"); });
  assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
  saveAudioDevices({ inputId: "mic", outputId: "speaker" });
  assert.deepEqual(readAudioDevices(), { inputId: "mic", outputId: "speaker" });
  saveAudioDevices({ inputId: "default", outputId: "default" });
  assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
  stored = "bad-json";
  assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
});

test("closing device settings during enumeration stops the temporary microphone immediately", async t => {
  let stopped = 0;
  let enumerating!: () => void;
  const entered = new Promise<void>(resolve => { enumerating = resolve; });
  const previous = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia: async () => ({ getTracks: () => [{ stop: () => { stopped++; } }] }),
    enumerateDevices: () => { enumerating(); return new Promise<MediaDeviceInfo[]>(() => {}); },
  } } });
  t.after(() => { if (previous) Object.defineProperty(globalThis, "navigator", previous); else Reflect.deleteProperty(globalThis, "navigator"); });
  const abort = new AbortController();
  const request = requestAudioDevices(abort.signal);
  await entered;
  abort.abort();
  await assert.rejects(request, { name: "AbortError" });
  assert.equal(stopped, 1);
});
