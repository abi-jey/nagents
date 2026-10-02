import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { browserMedia, liveSupport } from "./browser.js";
import type { AudioDeviceSelection } from "./types.js";

test("late microphone permission after cancellation stops tracks and releases the context", async () => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const savedAudio = Object.getOwnPropertyDescriptor(globalThis, "AudioContext");
  let grant!: (stream: MediaStream) => void;
  let stopped = 0, closed = 0;
  Object.defineProperty(globalThis, "AudioContext", { configurable: true, value: class {
    async resume() {}
    async close() { closed++; }
  } });
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia: () => new Promise<MediaStream>((resolve) => { grant = resolve; }),
  } } });
  try {
    const media = browserMedia({ connected: () => assert.fail("No connection expected"), ended: () => {}, failed: () => {}, playbackBlocked: () => {} });
    const abort = new AbortController();
    const prepare = media.prepare(abort.signal);
    abort.abort(); media.close();
    grant({ getTracks: () => [{ stop: () => { stopped++; } }] } as unknown as MediaStream);
    await assert.rejects(prepare, { name: "AbortError" });
    assert.equal(stopped, 1);
    assert.equal(closed, 1);
  } finally {
    if (saved) Object.defineProperty(globalThis, "navigator", saved); else Reflect.deleteProperty(globalThis, "navigator");
    if (savedAudio) Object.defineProperty(globalThis, "AudioContext", savedAudio); else Reflect.deleteProperty(globalThis, "AudioContext");
  }
});

test("insecure contexts explain why a connection cannot start", () => {
  assert.match(liveSupport(), /localhost or HTTPS/);
});

function browserFixture(devices: AudioDeviceSelection = { inputId: "", outputId: "" }) {
  const names = ["navigator", "AudioContext", "AudioWorkletNode", "WebSocket", "location", "RTCPeerConnection"] as const;
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  const microphone = Object.assign(new EventTarget(), { enabled: true, stops: 0, stop() { this.stops++; } });
  const stream = { getTracks: () => [microphone], getAudioTracks: () => [microphone] };
  const sent: ArrayBuffer[] = [], playback: boolean[] = [];
  const failures: string[] = [], sources: { stops: number }[] = [];
  const constraints: MediaStreamConstraints[] = [], sinks: string[] = [];
  let inputError: Error | undefined, outputError: Error | undefined, sinkResult = Promise.resolve();
  let connected = false, ended = false, socket!: Socket, node!: WorkletNode, context!: Context;
  class Node {
    port = { onmessage: (_event: MessageEvent<ArrayBuffer>) => {}, close: () => {} };
    connect(other: object) { return other as this; }
    disconnect() {}
  }
  class WorkletNode extends Node {
    onprocessorerror = () => {};
    constructor() { super(); node = this; }
  }
  class Context {
    state = "suspended";
    currentTime = 0;
    destination = {};
    closes = 0;
    onstatechange: (() => void) | null = null;
    constructor() { context = this; }
    audioWorklet = { addModule: async (path: string) => { assert.equal(path, "/assets/live-capture.js"); } };
    createMediaStreamSource(value: object) { assert.equal(value, stream); return new Node(); }
    createGain() { return Object.assign(new Node(), { gain: { value: 1 } }); }
    createBuffer(_channels: number, size: number, rate: number) {
      assert.equal(rate, 24_000);
      return { duration: size / rate, getChannelData: () => new Float32Array(size) };
    }
    createBufferSource() {
      const source = Object.assign(new Node(), { buffer: null, onended: () => {}, stops: 0, start: () => {}, stop() { this.stops++; } });
      sources.push(source); return source;
    }
    async resume() { this.state = "running"; }
    async setSinkId(id: string) { sinks.push(id); if (outputError) throw outputError; return sinkResult; }
    async close() { this.closes++; }
  }
  class Socket {
    static OPEN = 1;
    readyState = Socket.OPEN;
    bufferedAmount = 0;
    binaryType = "blob";
    onopen = () => {};
    onerror = () => {};
    onclose = (_event: { code: number }) => {};
    onmessage = (_event: MessageEvent<ArrayBuffer>) => {};
    constructor(readonly url: string, readonly protocols: string[]) { socket = this; }
    send(frame: ArrayBuffer) { sent.push(frame); }
    close() { this.readyState = 3; }
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: async (value: MediaStreamConstraints) => { constraints.push(value); if (inputError) throw inputError; return stream; } } } });
  Object.defineProperty(globalThis, "AudioContext", { configurable: true, value: Context });
  Object.defineProperty(globalThis, "AudioWorkletNode", { configurable: true, value: WorkletNode });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: Socket });
  Object.defineProperty(globalThis, "location", { configurable: true, value: { protocol: "http:", host: "127.0.0.1:8765" } });
  Object.defineProperty(globalThis, "RTCPeerConnection", { configurable: true, value: class { constructor() { assert.fail("WebRTC must not be used"); } } });
  const media = browserMedia({ connected: () => { connected = true; }, ended: () => { ended = true; }, failed: (message) => failures.push(message), playbackBlocked: (blocked) => playback.push(blocked) }, "websocket", devices);
  return { media, microphone, sent, playback, failures, sources, constraints, sinks,
    inputError: (cause: Error) => { inputError = cause; }, outputError: (cause: Error) => { outputError = cause; },
    sinkResult: (result: Promise<void>) => { sinkResult = result; },
    withoutOutputRouting: () => { Object.defineProperty(Context.prototype, "setSinkId", { value: undefined }); },
    context: () => context, socket: () => socket, node: () => node, connected: () => connected, ended: () => ended,
    open: async () => { await media.prepare(new AbortController().signal); const opening = media.connect("voice-id", "web-token"); socket.onopen(); await opening; },
    restore: () => {
    media.close();
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else Reflect.deleteProperty(globalThis, name);
    }
    },
  };
}

test("microphone and playback use only the ngn serve WebSocket, release devices and mute pending frames", async (t) => {
  const f = browserFixture(); t.after(f.restore);
  await f.open();
  assert.deepEqual(f.constraints, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }]);
  assert.deepEqual(f.sinks, [], "fresh playback follows the system default without pinning a sink");
  assert.equal(f.connected(), true);
  assert.equal(f.socket().url, "ws://127.0.0.1:8765/api/live/sessions/voice-id/audio");
  assert.deepEqual(f.socket().protocols, ["ngn.live.v1", "ngn.token.web-token"]);
  assert.equal(f.socket().binaryType, "arraybuffer");
  const frame = new Int16Array(480).fill(1234).buffer;
  f.node().port.onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sent[0], frame);
  f.socket().onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  f.media.muteInput(true); f.media.muteOutput(true);
  f.node().port.onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.microphone.enabled, false);
  assert.ok(new Int16Array(f.sent[1]).every((sample) => sample === 0));
  assert.equal(f.sources[0].stops, 1);
  f.socket().onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sources.length, 1, "muted output is discarded");
  f.media.muteInput(false); f.node().port.onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.microphone.enabled, true); assert.equal(f.sent[2], frame);
  await f.media.play(); assert.equal(f.playback.at(-1), false);
  f.media.close(); f.media.close(); await setImmediate();
  assert.equal(f.microphone.stops, 1); assert.equal(f.socket().readyState, 3);
  assert.deepEqual(f.failures, []);
});

test("cancelling an unopened relay settles connection without waiting for a browser close event", async (t) => {
  const f = browserFixture(); t.after(f.restore);
  await f.media.prepare(new AbortController().signal);
  const opening = f.media.connect("voice-id", "web-token");
  f.media.close();
  await assert.rejects(opening, { name: "AbortError" });
  f.socket().onopen();
  assert.equal(f.connected(), false); assert.deepEqual(f.failures, []);
});

test("clean relay closure requests final status while abnormal closure reports failure", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  f.socket().onclose({ code: 1000 });
  assert.equal(f.ended(), true); assert.deepEqual(f.failures, []);
  f.socket().onclose({ code: 1006 });
  assert.match(f.failures[0], /disconnected/);
});

test("audio context interruption stops queued speech and exposes playback recovery", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  f.socket().onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
  f.context().state = "interrupted"; f.context().onstatechange?.();
  assert.equal(f.playback.at(-1), true); assert.equal(f.sources[0].stops, 1);
  await f.media.play();
  assert.equal(f.playback.at(-1), false);
  f.node().onprocessorerror();
  assert.match(f.failures[0], /processing stopped/);
});

test("playback backlog and invalid or stalled relay frames stay bounded", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  for (let index = 0; index < 40; index++) f.socket().onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
  assert.ok(f.sources.some((source) => source.stops > 0));
  assert.ok(f.sources.filter((source) => source.stops === 0).length < 31);
  f.socket().onmessage({ data: new ArrayBuffer(3) } as MessageEvent<ArrayBuffer>);
  assert.match(f.failures.at(-1) || "", /invalid frame/);
  f.socket().bufferedAmount = 192_001;
  f.node().port.onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sent.length, 0); assert.match(f.failures.at(-1) || "", /falling behind/);
});

test("relay routes explicit microphone and speaker IDs before making a connection", async (t) => {
  const f = browserFixture({ inputId: "usb-mic", outputId: "headphones" }); t.after(f.restore);
  await f.media.prepare(new AbortController().signal);
  assert.deepEqual(f.constraints, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, deviceId: { exact: "usb-mic" } } }]);
  assert.deepEqual(f.sinks, ["headphones"]); assert.equal(f.connected(), false); assert.equal(f.socket(), undefined);
});

test("cancelling relay speaker selection releases the microphone and context before the sink settles", async (t) => {
  const f = browserFixture({ inputId: "", outputId: "headphones" }); t.after(f.restore);
  let finish!: () => void; f.sinkResult(new Promise<void>((resolve) => { finish = resolve; }));
  const abort = new AbortController(), preparing = f.media.prepare(abort.signal);
  await setImmediate();
  assert.deepEqual(f.sinks, ["headphones"]); assert.equal(f.node(), undefined);
  abort.abort();
  assert.equal(f.microphone.stops, 1); assert.equal(f.context().closes, 1);
  finish(); await assert.rejects(preparing, { name: "AbortError" });
  assert.equal(f.node(), undefined); assert.equal(f.socket(), undefined); assert.equal(f.connected(), false);
});

for (const name of ["NotAllowedError", "NotFoundError"]) test(`relay selected speaker ${name} fails clearly and releases capture`, async (t) => {
  const f = browserFixture({ inputId: "", outputId: "headphones" }); t.after(f.restore);
  f.outputError(new DOMException("Device unavailable", name));
  await assert.rejects(f.media.prepare(new AbortController().signal), /selected speaker.*System default/);
  assert.equal(f.microphone.stops, 1); assert.equal(f.context().closes, 1); assert.equal(f.connected(), false);
});

test("an unsupported relay speaker selection requires explicit system default instead of silently rerouting", async (t) => {
  const f = browserFixture({ inputId: "", outputId: "headphones" }); t.after(f.restore); f.withoutOutputRouting();
  await assert.rejects(f.media.prepare(new AbortController().signal), /cannot use the selected speaker.*System default/);
  assert.equal(f.microphone.stops, 1); assert.equal(f.connected(), false);
});

test("relay system default still works when browser speaker selection is unsupported", async (t) => {
  const f = browserFixture(); t.after(f.restore); f.withoutOutputRouting();
  await f.open();
  assert.equal(f.connected(), true); assert.deepEqual(f.sinks, []);
});

test("a missing selected relay microphone asks for another device without using the default", async (t) => {
  const f = browserFixture({ inputId: "removed-mic", outputId: "" }); t.after(f.restore);
  f.inputError(new DOMException("Unknown device", "OverconstrainedError"));
  await assert.rejects(f.media.prepare(new AbortController().signal), /selected microphone.*System default/);
  assert.equal(f.constraints.length, 1); assert.equal(f.context().closes, 1); assert.equal(f.connected(), false);
});
