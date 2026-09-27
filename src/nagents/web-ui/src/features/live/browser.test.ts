import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { browserMedia, liveSupport } from "./browser.js";

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
    const media = browserMedia({ connected: () => assert.fail("No connection expected"), failed: () => {}, playbackBlocked: () => {} });
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

test("microphone and playback use only the ngn serve WebSocket, release devices and bound playback", async () => {
  const names = ["navigator", "AudioContext", "AudioWorkletNode", "WebSocket", "location", "RTCPeerConnection"] as const;
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  const microphone = Object.assign(new EventTarget(), { enabled: true, stops: 0, stop() { this.stops++; } });
  const stream = { getTracks: () => [microphone], getAudioTracks: () => [microphone] };
  const sent: ArrayBuffer[] = [], playback: boolean[] = [];
  let connected = false, socket!: Socket, node!: Node;
  class Node {
    port = { onmessage: (_event: MessageEvent<ArrayBuffer>) => {}, close: () => {} };
    connect(other: object) { return other as this; }
    disconnect() {}
  }
  class WorkletNode extends Node { constructor() { super(); node = this; } }
  class Context {
    state = "suspended";
    currentTime = 0;
    destination = {};
    audioWorklet = { addModule: async (path: string) => { assert.equal(path, "/assets/live-capture.js"); } };
    createMediaStreamSource(value: object) { assert.equal(value, stream); return new Node(); }
    createGain() { return Object.assign(new Node(), { gain: { value: 1 } }); }
    createBuffer(_channels: number, size: number, rate: number) {
      assert.equal(rate, 24_000);
      return { duration: size / rate, getChannelData: () => new Float32Array(size) };
    }
    createBufferSource() { return Object.assign(new Node(), { buffer: null, onended: () => {}, start: () => {}, stop: () => {} }); }
    async resume() { this.state = "running"; }
    async close() {}
  }
  class Socket {
    static OPEN = 1;
    readyState = Socket.OPEN;
    bufferedAmount = 0;
    binaryType = "blob";
    onopen = () => {};
    onerror = () => {};
    onclose = () => {};
    onmessage = (_event: MessageEvent<ArrayBuffer>) => {};
    constructor(readonly url: string, readonly protocols: string[]) { socket = this; }
    send(frame: ArrayBuffer) { sent.push(frame); }
    close() { this.readyState = 3; }
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: async () => stream } } });
  Object.defineProperty(globalThis, "AudioContext", { configurable: true, value: Context });
  Object.defineProperty(globalThis, "AudioWorkletNode", { configurable: true, value: WorkletNode });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: Socket });
  Object.defineProperty(globalThis, "location", { configurable: true, value: { protocol: "http:", host: "127.0.0.1:8765" } });
  Object.defineProperty(globalThis, "RTCPeerConnection", { configurable: true, value: class { constructor() { assert.fail("WebRTC must not be used"); } } });
  try {
    const media = browserMedia({ connected: () => { connected = true; }, failed: assert.fail, playbackBlocked: (blocked) => playback.push(blocked) });
    await media.prepare(new AbortController().signal);
    const opening = media.connect("voice-id", "web-token"); socket.onopen(); await opening;
    assert.equal(connected, true);
    assert.equal(socket.url, "ws://127.0.0.1:8765/api/live/sessions/voice-id/audio");
    assert.deepEqual(socket.protocols, ["ngn.live.v1", "ngn.token.web-token"]);
    assert.equal(socket.binaryType, "arraybuffer");
    node.port.onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
    assert.equal(sent.length, 1);
    socket.onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
    media.muteInput(true); media.muteOutput(true);
    assert.equal(microphone.enabled, false);
    await media.play(); assert.equal(playback.at(-1), false);
    media.close(); media.close(); await setImmediate();
    assert.equal(microphone.stops, 1); assert.equal(socket.readyState, 3);
  } finally {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
