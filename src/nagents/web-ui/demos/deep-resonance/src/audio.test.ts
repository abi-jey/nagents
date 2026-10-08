import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { useDemoAudio, type DemoAudio } from "./useDemoAudio.js";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class TestTrack {
  readyState: MediaStreamTrackState = "live";
  enabled = true;
  muted = false;
  stopped = 0;
  private events = new Map<string, (() => void)[]>();
  stop() { this.stopped++; this.readyState = "ended"; }
  addEventListener(name: string, callback: () => void) { this.events.set(name, [...this.events.get(name) || [], callback]); }
  end() { this.readyState = "ended"; this.events.get("ended")?.forEach(callback => callback()); }
}
function testStream() {
  const track = new TestTrack();
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
  return { track, stream };
}

function fixture() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost/" });
  const saved = new Map<string, PropertyDescriptor | undefined>();
  const streams: ReturnType<typeof testStream>[] = [], requests: MediaStreamConstraints[] = [], createdElements = new WeakSet<HTMLMediaElement>();
  let clock = 100_000, permission: ReturnType<typeof deferred<MediaStream>> | undefined, failResume = false;
  const contexts: TestContext[] = [];
  const paused = new WeakMap<HTMLMediaElement, boolean>();
  const mediaPrototype = dom.window.HTMLMediaElement.prototype;
  Object.defineProperties(mediaPrototype, {
    paused: { configurable: true, get(this: HTMLMediaElement) { return paused.get(this) !== false; } },
    readyState: { configurable: true, get: () => 4 },
    ended: { configurable: true, get: () => false },
    seeking: { configurable: true, get: () => false },
    play: { configurable: true, value(this: HTMLMediaElement) { paused.set(this, false); this.dispatchEvent(new dom.window.Event("play")); this.dispatchEvent(new dom.window.Event("playing")); return Promise.resolve(); } },
    pause: { configurable: true, value(this: HTMLMediaElement) { if (!this.paused) { paused.set(this, true); this.dispatchEvent(new dom.window.Event("pause")); } } },
    load: { configurable: true, value(this: HTMLMediaElement) { this.dispatchEvent(new dom.window.Event("emptied")); } },
  });
  class TestNode {
    outputs: TestNode[] = [];
    gain = { value: 1 };
    fftSize = 1024;
    smoothingTimeConstant = 0;
    constructor(readonly context: TestContext, readonly kind: string) {}
    get frequencyBinCount() { return this.fftSize / 2; }
    connect(node: TestNode) { this.outputs.push(node); return node; }
    disconnect() { this.outputs = []; }
    getFloatTimeDomainData(samples: Float32Array) {
      const media = this.context.media;
      samples.fill(this.context.amplitude * (media ? media.muted ? 0 : media.volume : 1));
    }
  }
  class TestContext {
    state: AudioContextState = "suspended";
    sampleRate = 48_000;
    amplitude = .12;
    destination = new TestNode(this, "destination");
    nodes: TestNode[] = [];
    media?: HTMLMediaElement;
    onstatechange?: () => void;
    constructor() { contexts.push(this); }
    createAnalyser() { const node = new TestNode(this, "analyser"); this.nodes.push(node); return node; }
    createGain() { const node = new TestNode(this, "gain"); this.nodes.push(node); return node; }
    createMediaStreamSource() { const node = new TestNode(this, "input"); this.nodes.push(node); return node; }
    createMediaElementSource(element: HTMLMediaElement) {
      if (createdElements.has(element)) throw new Error("Media element rebound to a second source");
      createdElements.add(element); this.media = element;
      const node = new TestNode(this, "output"); this.nodes.push(node); return node;
    }
    resume() { if (failResume) { failResume = false; return Promise.reject(new Error("Resume rejected")); } this.state = "running"; return Promise.resolve(); }
    suspend() { this.state = "suspended"; this.onstatechange?.(); return Promise.resolve(); }
    close() { this.state = "closed"; return Promise.resolve(); }
  }
  const mediaDevices = {
    getUserMedia(options: MediaStreamConstraints) {
      requests.push(options);
      if (permission) { const next = permission; permission = undefined; return next.promise; }
      const source = testStream(); streams.push(source); return Promise.resolve(source.stream);
    },
    enumerateDevices: async () => [{ kind: "audioinput", deviceId: "test-a", label: "Microphone A" }, { kind: "audioinput", deviceId: "test-b", label: "Microphone B" }],
    addEventListener() {}, removeEventListener() {},
  };
  const measuredClock = new Proxy(globalThis.performance, { get(target, key) {
    if (key === "now") return () => clock;
    const value: unknown = Reflect.get(target, key, target);
    return typeof value === "function" ? value.bind(target) : value;
  } });
  for (const [name, value] of Object.entries({ window: dom.window, document: dom.window.document, navigator: { mediaDevices }, HTMLElement: dom.window.HTMLElement,
    HTMLMediaElement: dom.window.HTMLMediaElement, AudioContext: TestContext, isSecureContext: true, performance: measuredClock, IS_REACT_ACT_ENVIRONMENT: true })) {
    saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name)); Object.defineProperty(globalThis, name, { configurable: true, value });
  }
  let latest: DemoAudio | undefined;
  const playback: number[] = [], microphone: number[] = [];
  function Harness({ version, audioKey }: { version: number; audioKey: number }) {
    latest = useDemoAudio({ sampleUrl: "/sample.wav", onPlaybackStart: () => { playback.push(version); }, onMicStart: () => { microphone.push(version); } });
    return createElement("audio", { ref: latest.audioRef, key: audioKey });
  }
  const root = createRoot(dom.window.document.getElementById("root")!);
  return {
    contexts, requests, streams, playback, microphone,
    api() { assert(latest); return latest; },
    async render(version = 1, audioKey = 1) { await act(async () => { root.render(createElement(StrictMode, null, createElement(Harness, { version, audioKey }))); }); },
    pending() { const pending = deferred<MediaStream>(); permission = pending; return pending; },
    failResume() { failResume = true; },
    advance(ms: number) { clock += ms; },
    async unmount() { await act(async () => { root.unmount(); await Promise.resolve(); }); },
    restore() { for (const [name, descriptor] of saved) { if (descriptor) Object.defineProperty(globalThis, name, descriptor); else Reflect.deleteProperty(globalThis, name); } dom.window.close(); },
  };
}

test("demo audio owns StrictMode-safe graphs, raw PCM meters, device switching and latest callbacks", async () => {
  const f = fixture();
  try {
    await f.render(); assert.equal(f.requests.length, 0); assert.equal(f.contexts.length, 0);
    await act(async () => { await f.api().toggleMicrophone(); });
    assert.equal(f.requests.length, 1); assert.equal(f.api().input.active, true); assert.deepEqual(f.microphone, [1]);
    assert.equal(f.contexts[0].nodes.find(node => node.kind === "gain")?.gain.value, 0);
    assert.equal(f.api().input.devices.length, 3);
    let frames = { input: { active: false, rms: 0, low: 0, mid: 0, high: 0 }, output: { active: false, rms: 0, low: 0, mid: 0, high: 0 } };
    await act(async () => { frames = f.api().sample(); }); assert(frames.input.rms > 0); assert.equal(frames.output.rms, 0);
    const meterChanges: number[] = [];
    for (let i = 0; i < 40; i++) {
      f.advance(5); f.contexts[0].amplitude = .05 + i * .002;
      await act(async () => { f.api().sample(); });
      if (meterChanges.at(-1) !== f.api().input.level) meterChanges.push(f.api().input.level);
    }
    assert(meterChanges.length <= 4, "Meter state exceeded 10 Hz");
    await act(async () => { await f.api().setInputDevice("test-b"); });
    assert(f.streams[0].track.stopped > 0); assert.equal(f.contexts[0].state, "closed");
    assert.deepEqual(f.requests.at(-1)?.audio, { echoCancellation: true, noiseSuppression: true, autoGainControl: false, deviceId: { exact: "test-b" } });
    await act(async () => { await f.api().playSample(); });
    assert.equal(f.api().input.active, true, "Output must mix with the microphone"); assert.equal(f.api().output.playing, true);
    const output = f.contexts.find(context => context.media); assert(output); assert.equal(output.nodes.find(node => node.kind === "gain")?.gain.value, 1);
    f.advance(30); await act(async () => { frames = f.api().sample(); }); assert(frames.output.rms > 0);
    const contextCount = f.contexts.length;
    await f.render(2); await act(async () => { await f.api().playSample(); });
    assert.equal(f.contexts.length, contextCount); assert.equal(f.playback.at(-1), 2);
    await f.render(2, 2); assert.equal(output.state, "closed");
    await act(async () => { await f.api().playSample(); }); assert.equal(f.contexts.length, contextCount + 1);
    await act(async () => { f.api().pauseOutput(); f.api().stopMicrophone(); });
    assert.equal(f.api().sample().input.rms, 0); assert.equal(f.api().sample().output.rms, 0); assert.equal(f.api().input.active, false);
    await f.unmount(); assert(f.contexts.every(context => context.state === "closed"));
  } finally { f.restore(); }
});

test("demo microphone releases permission results after Stop and unmount, and survives failed contexts", async () => {
  const f = fixture();
  try {
    await f.render(); const pending = f.pending(); let starting!: Promise<void>;
    await act(async () => { starting = f.api().toggleMicrophone(); await Promise.resolve(); });
    assert.equal(f.api().input.starting, true);
    await act(async () => { await f.api().toggleMicrophone(); });
    const late = testStream(); await act(async () => { pending.resolve(late.stream); await starting; });
    assert(late.track.stopped > 0); assert.equal(f.api().input.active, false);
    f.failResume(); await act(async () => { await f.api().toggleMicrophone(); }); assert.equal(f.api().input.active, false); assert.equal(f.contexts.at(-1)?.state, "closed");
    await act(async () => { await f.api().toggleMicrophone(); });
    await act(async () => { f.streams.at(-1)!.track.end(); }); assert.equal(f.api().input.active, false); assert.match(f.api().input.status, /disconnected/);
    const afterUnmount = f.pending(); await act(async () => { starting = f.api().toggleMicrophone(); await Promise.resolve(); });
    await f.unmount(); const released = testStream(); afterUnmount.resolve(released.stream); await starting;
    assert(released.track.stopped > 0); assert(f.contexts.every(context => context.state === "closed"));
  } finally { f.restore(); }
});
