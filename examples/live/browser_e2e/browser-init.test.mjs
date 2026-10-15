import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

function fixture({ honorSink = true } = {}) {
  const created = [];
  class AudioNode {
    constructor(context) { this.context = context; }
    connect(destination) { return destination; }
  }
  class Context {
    constructor(options = {}) {
      this.options = options;
      this.sinkId = honorSink ? options.sinkId : "";
      this.sampleRate = options.sampleRate || 48000;
      this.currentTime = 0;
      this.state = "running";
      this.destination = new AudioNode(this);
      this.audioWorklet = { addModule: async () => {} };
      created.push(this);
    }
    addEventListener() {}
    async close() { this.state = "closed"; }
    async resume() {}
    async setSinkId(value) { this.sinkId = value; }
    async decodeAudioData() { return { duration: 1 }; }
    createMediaStreamDestination() { return { stream: { clone: () => ({ virtual: true }) } }; }
    createConstantSource() {
      const source = new AudioNode(this);
      return Object.assign(source, { offset: { value: 0 }, start() {} });
    }
  }
  class Worklet extends AudioNode {
    port = { postMessage() {}, addEventListener() {}, start() {} };
  }
  const wav = new Uint8Array([1, 2, 3]);
  const globals = {
    __voiceE2EConfig: { mode: "offline", maxSeconds: 660, wavPath: "/fixture.wav",
      fixtureSha256: createHash("sha256").update(wav).digest("hex") },
    AudioContext: Context, AudioWorkletNode: Worklet, AudioNode, WebSocket: class {},
    fetch: async () => ({ ok: true, arrayBuffer: async () => wav.buffer }),
    navigator: { mediaDevices: {}, userAgent: "unit-test" },
    performance: { now: () => 100, timeOrigin: 0 }, crypto: webcrypto,
    document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {}, scripts: [],
      querySelectorAll: () => [] },
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { removeItem() {} }, addEventListener() {},
    setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
    innerWidth: 1000, innerHeight: 800, devicePixelRatio: 1,
    URL, Uint8Array,
  };
  runInNewContext(readFileSync(new URL("./browser-init.js", import.meta.url), "utf8"), globals);
  return { globals, created };
}

test("speaker and microphone use verified software clocks, never the system speaker", async () => {
  const { globals, created } = fixture();
  const speaker = new globals.AudioContext({ sampleRate: 44100, sinkId: "physical-device" });
  assert.equal(speaker.sampleRate, 44100);
  assert.equal(speaker.sinkId.type, "none");
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  assert.equal(created.length, 2);
  assert.equal(created[1].sampleRate, 24000);
  assert.equal(created[1].sinkId.type, "none");
  const events = globals.__voiceE2E.export().events.filter(event => event.type === "audio.virtual-sink");
  assert.deepEqual(Array.from(events, event => `${event.role}:${event.sinkType}`), ["speaker:none", "microphone:none"]);
  await assert.rejects(() => speaker.setSinkId(""), /software speaker sink/);
  assert.equal(speaker.sinkId.type, "none");
});

test("unsupported software sink fails and closes context without a physical fallback", async () => {
  const { globals, created } = fixture({ honorSink: false });
  assert.throws(() => new globals.AudioContext(), /no physical-device fallback/);
  assert.equal(created[0].state, "closed");
  await assert.rejects(() => globals.navigator.mediaDevices.getUserMedia({ audio: true }), /no physical-device fallback/);
  assert.equal(created[1].state, "closed");
  assert.equal(globals.__voiceE2E.export().events.filter(event => event.type === "harness.error").length, 2);
});
