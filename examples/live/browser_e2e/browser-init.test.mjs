import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

function fixture({ honorSink = true, jsonResponses = [] } = {}) {
  const created = [];
  const sockets = [];
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
  class Socket {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; this.listeners = {}; sockets.push(this); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    send(value) { this.lastSent = value; }
    emit(name, data) { this.listeners[name]?.({ data }); }
  }
  const wav = new Uint8Array([1, 2, 3]);
  const globals = {
    __voiceE2EConfig: { mode: "offline", maxSeconds: 660, wavPath: "/fixture.wav",
      fixtureSha256: createHash("sha256").update(wav).digest("hex") },
    AudioContext: Context, AudioWorkletNode: Worklet, AudioNode, WebSocket: Socket,
    fetch: async () => ({ ok: true, status: 200, arrayBuffer: async () => wav.buffer,
      clone: () => ({ json: async () => jsonResponses.shift() || {} }) }),
    navigator: { mediaDevices: {}, userAgent: "unit-test" },
    location: { href: "http://localhost/" },
    performance: { now: () => 100, timeOrigin: 0 }, crypto: webcrypto,
    document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {}, scripts: [],
      querySelectorAll: () => [] },
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { removeItem() {} }, addEventListener() {},
    setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
    innerWidth: 1000, innerHeight: 800, devicePixelRatio: 1,
    URL, Uint8Array, Request,
  };
  runInNewContext(readFileSync(new URL("./browser-init.js", import.meta.url), "utf8"), globals);
  return { globals, created, sockets };
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

test("closing snapshots never replay earlier transcripts into the last round", async () => {
  const first = { seq: 1, type: "transcript", speaker: "assistant", text: "A completed earlier answer." };
  const { globals } = fixture({ jsonResponses: [
    { session_id: "voice-owned", events: [first] },
    { session_id: "voice-owned", events: [first, { seq: 2, type: "status", text: "Finalization confirmed." }] },
  ] });
  await globals.fetch("/api/live/sessions/voice-owned");
  await globals.fetch("/api/live/sessions/voice-owned/close");
  await Promise.resolve();
  const events = globals.__voiceE2E.export().events.filter(event => event.type === "live.event");
  assert.deepEqual(Array.from(events, event => event.seq), [1, 2]);
  assert.equal(globals.__voiceE2E.status().transcripts.length, 1);
});

test("main event socket captures only owned tool/task lifecycle metadata", () => {
  const { globals } = fixture();
  const socket = new globals.WebSocket("ws://localhost/api/events", ["ngn.events.v1", "ngn.token.never-capture"]);
  socket.send(JSON.stringify({ type: "subscribe", session_id: "owned-session", after: 0 }));
  socket.emit("message", JSON.stringify({
    type: "event", session_id: "other-session", cursor: 1,
    record: { event: "tool_call", run_id: "wrong-run", call_id: "ignored", name: "shell", arguments: { command: "private" } },
  }));
  socket.emit("message", JSON.stringify({
    type: "event", session_id: "owned-session", cursor: 2,
    record: { event: "tool_call", run_id: "run-owned", call_id: "call-owned", name: "delegate", arguments: { prompt: "private prompt" } },
  }));
  socket.emit("message", JSON.stringify({
    type: "event", session_id: "owned-session", cursor: 3,
    record: { event: "task_started", run_id: "run-owned", task_id: "task-owned", name: "Helper" },
  }));
  socket.emit("message", JSON.stringify({
    type: "event", session_id: "owned-session", cursor: 4,
    record: { event: "task_completed", run_id: "run-owned", task_id: "task-owned", status: "completed", result: "private result" },
  }));
  socket.emit("message", JSON.stringify({
    type: "event", session_id: "owned-session", cursor: 5,
    record: { event: "task_activity", run_id: "run-owned", task_id: "task-owned", activation: 1,
      payload: { type: "tool_result", id: "read-owned", name: "read_file", result: { content: "private file" } } },
  }));

  const state = globals.__voiceE2E.status();
  assert.equal(state.subscribedSessionId, "owned-session");
  assert.deepEqual(Array.from(state.toolCalls, event => [event.eventType, event.callId, event.runId]), [
    ["tool_call", "call-owned", "run-owned"], ["tool_result", "read-owned", "run-owned"],
  ]);
  assert.deepEqual(Array.from(state.delegations, event => [event.eventType, event.taskId, event.runId]), [
    ["task_started", "task-owned", "run-owned"], ["task_completed", "task-owned", "run-owned"],
  ]);
  const exportText = JSON.stringify(globals.__voiceE2E.export());
  for (const secret of ["ngn.token.never-capture", "private", "wrong-run", "other-session"]) {
    assert.equal(exportText.includes(secret), false, secret);
  }
});
