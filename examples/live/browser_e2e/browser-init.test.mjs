import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

function fixture({ honorSink = true, jsonResponses = [], maxSeconds = 660, autoCheckpoint = true } = {}) {
  const created = [];
  const sockets = [];
  let now = 100;
  class AudioNode {
    constructor(context) { this.context = context; }
    connect(destination) { return destination; }
    addEventListener() {}
  }
  class Context {
    constructor(options = {}) {
      this.options = options;
      this.sinkId = honorSink ? options.sinkId : "";
      this.sampleRate = options.sampleRate || 48000;
      this.currentTime = 0;
      this.state = "running";
      this.outputTimestampCalls = 0;
      this.destination = new AudioNode(this);
      this.audioWorklet = { addModule: async () => {} };
      created.push(this);
    }
    addEventListener() {}
    getOutputTimestamp() {
      this.outputTimestampCalls++;
      return { contextTime: this.currentTime, performanceTime: 10_000 + this.currentTime * 1000 };
    }
    async close() { this.state = "closed"; }
    async resume() {}
    async setSinkId(value) { this.sinkId = value; }
    async decodeAudioData() { return { duration: 1 }; }
    createMediaStreamDestination() { return { stream: { clone: () => ({ virtual: true }) } }; }
    createBufferSource() {
      const source = new AudioNode(this);
      return Object.assign(source, { buffer: null, start() {}, stop() {}, addEventListener() {} });
    }
    createConstantSource() {
      const source = new AudioNode(this);
      return Object.assign(source, { offset: { value: 0 }, start() {} });
    }
  }
  class Worklet extends AudioNode {
    constructor(context, name) {
      super(context);
      this.name = name;
      this.posted = [];
      this.acceptedSamples = 0;
      const listeners = [];
      this.port = {
        onmessage: null,
        postMessage: message => {
          this.posted.push(message);
          if (message?.type === "pcm") this.acceptedSamples += message.buffer.byteLength / 2;
          if (autoCheckpoint && message?.type === "trace-checkpoint") {
            if (this.acceptedSamples > 0) this.port.emit({ type: "trace", epoch: message.epoch, sequence: 0,
              omittedIntervals: 0, batchIntervals: 1,
              intervals: [{ sourceStart: 0, samples: this.acceptedSamples, at: 1,
                until: 1 + (this.acceptedSamples - 1) / 24000 }] });
            this.port.emit({ type: "trace-checkpoint", epoch: message.epoch, requestId: message.requestId,
              status: "completed", acceptedEnd: this.acceptedSamples, renderedEnd: this.acceptedSamples,
              receivedEnd: this.acceptedSamples, renderedAt: 1, omittedIntervals: 0 });
          }
          if (message === "arm") this.port.onmessage?.({ data: { type: "capture-start", schema: 2,
            frameOrigin: 4096, observedFrame: 4096, sampleOffset: 0, sequence: 0, sampleRate: 48000 } });
          if (message === "stop") this.port.onmessage?.({ data: { type: "capture-stop", schema: 2,
            finalFrame: 4096, observedFinalFrame: 4096, frameOrigin: 4096,
            samplesCaptured: 0, blocksCaptured: 0, sampleRate: 48000, armed: false } });
        },
        addEventListener: (type, callback) => { if (type === "message") listeners.push(callback); },
        emit: data => listeners.forEach(callback => callback({ data })), start() {},
      };
    }
  }
  class Socket {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; this.listeners = {}; sockets.push(this); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    send(value) { this.lastSent = value; }
    emit(name, data) { this.listeners[name]?.({ data }); }
  }
  const wav = new Uint8Array([1, 2, 3]);
  const globals = {
    __voiceE2EConfig: { mode: "offline", maxSeconds, wavPath: "/fixture.wav",
      fixtureSha256: createHash("sha256").update(wav).digest("hex") },
    AudioContext: Context, AudioWorkletNode: Worklet, AudioNode, WebSocket: Socket, DataView,
    fetch: async () => ({ ok: true, status: 200, arrayBuffer: async () => wav.buffer,
      clone: () => ({ json: async () => jsonResponses.shift() || {} }) }),
    navigator: { mediaDevices: {}, userAgent: "unit-test" }, ArrayBuffer, Uint8Array, btoa,
    location: { href: "http://localhost/" },
    performance: { now: () => now, timeOrigin: 0 }, crypto: webcrypto,
    document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {}, scripts: [],
      querySelectorAll: () => [] },
    MutationObserver: class { observe() {} disconnect() {} },
    localStorage: { removeItem() {} }, addEventListener() {},
    setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
    innerWidth: 1000, innerHeight: 800, devicePixelRatio: 1,
    URL, Uint8Array, Request,
  };
  runInNewContext(readFileSync(new URL("./browser-init.js", import.meta.url), "utf8"), globals);
  return { globals, created, sockets, setNow: value => { now = value; } };
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

test("rendered PCM records bounded source and active-sample timing without retaining audio", async () => {
  const { globals, created } = fixture();
  new globals.AudioContext();
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  const socket = new globals.WebSocket("ws://localhost/api/live/sessions/voice-owned/audio");
  const packet = new globals.ArrayBuffer(8);
  socket.emit("message", packet);
  const playback = new globals.AudioWorkletNode(created[0], "ngn-live-playback");
  playback.port.postMessage({ type: "pcm", epoch: 4, buffer: packet });
  const rendered = new globals.ArrayBuffer(8);
  const view = new DataView(rendered);
  view.setInt16(0, 0, true);
  view.setInt16(2, 1000, true);
  view.setInt16(4, -1200, true);
  view.setInt16(6, 0, true);
  playback.port.emit({ type: "played", epoch: 4, sourceStart: 10, at: 1, until: 1 + 3 / 24000, buffer: rendered });

  const events = globals.__voiceE2E.export().events;
  const submit = events.find(event => event.type === "playback.submit");
  const event = events.find(item => item.type === "playback.rendered");
  assert.equal(submit.submittedSamples, 4);
  assert.equal(event.epoch, 4);
  assert.equal(event.sourceStart, 10);
  assert.equal(event.samples, 4);
  assert.equal(event.firstActiveSample, 1);
  assert.equal(event.lastActiveSample, 2);
  assert.equal(event.activeSamples, 2);
  assert.deepEqual(Array.from(event.activeSpans, span => Array.from(span)), [[1, 3]]);
  assert.equal(event.until, 1 + 3 / 24000);
  assert.equal("buffer" in event, false);
  assert.equal(JSON.stringify(event).includes("1000"), false);
});

test("isolated playback enables numeric trace and ACKs each metadata batch without PCM", () => {
  const { globals, created } = fixture();
  const context = new globals.AudioContext();
  const playback = new globals.AudioWorkletNode(context, "ngn-live-playback");
  assert.equal(playback.posted[0].type, "trace");
  assert.equal(playback.posted[0].enabled, true);
  assert.equal(playback.posted[0].epoch, 0);
  playback.port.emit({ type: "trace", epoch: 0, sequence: 0, omittedIntervals: 0,
    intervals: [{ sourceStart: 0, samples: 480, at: 1, until: 1 + 479 / 24000 }] });
  assert.equal(created[0].outputTimestampCalls, 0);
  const event = globals.__voiceE2E.export().events.find(item => item.type === "playback.trace");
  assert.equal(event.contextId, 0);
  assert.equal(event.sequence, 0);
  assert.equal(event.sourceStart, 0);
  assert.equal(event.samples, 480);
  assert.equal("buffer" in event, false);
  assert.equal(playback.posted.at(-1).type, "trace-ack");
  assert.equal(playback.posted.at(-1).epoch, 0);
  assert.equal(playback.posted.at(-1).sequence, 0);
  assert.equal(created[0].outputTimestampCalls, 1); // Context export is a deliberate diagnostic sample.
});

test("rendered clock uses a fresh cached timestamp and falls back when it is older than one second", async () => {
  const { globals, created, setNow } = fixture();
  new globals.AudioContext();
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  const playback = new globals.AudioWorkletNode(created[0], "ngn-live-playback");
  playback.port.emit({ type: "health", epoch: 0, receivedSamples: 0, playedSamples: 0 });
  assert.equal(created[0].outputTimestampCalls, 1);

  const rendered = new globals.ArrayBuffer(4);
  new DataView(rendered).setInt16(0, 1000, true);
  setNow(200);
  playback.port.emit({ type: "played", epoch: 0, sourceStart: 0, at: 1, until: 1 + 1 / 24000, buffer: rendered });
  let event = globals.__voiceE2E.export().events.find(item => item.type === "playback.rendered");
  assert.equal(event.outputClockSource, "cached_output_timestamp");
  assert.equal(event.outputTimestampAgeMs, 100);
  assert.equal(created[0].outputTimestampCalls, 2); // One call for health; one for export, none for this played report.

  setNow(1200);
  playback.port.emit({ type: "played", epoch: 0, sourceStart: 1, at: 1, until: 1 + 1 / 24000, buffer: rendered });
  event = globals.__voiceE2E.export().events.filter(item => item.type === "playback.rendered")[1];
  assert.equal(event.outputClockSource, "context_time_fallback");
  assert.equal(event.outputTimestampAgeMs, 1100);
  assert.equal(event.outputTimestamp, undefined);
  assert.equal(created[0].outputTimestampCalls, 3); // Only the second export sampled the graph clock.
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


test("received live PCM preserves packet bytes/order and playback-clock metadata without copying socket protocols", async () => {
  const { globals, created } = fixture();
  const speaker = new globals.AudioContext();
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  const socket = new globals.WebSocket("ws://localhost/api/live/sessions/voice-owned/audio",
    ["ngn.live.audio.v1", "ngn.token.never-capture"]);
  const beforeCapture = new globals.ArrayBuffer(2);
  new globals.Uint8Array(beforeCapture).set([9, 9]);
  socket.emit("message", beforeCapture);
  const playback = new globals.AudioWorkletNode(created[0], "ngn-live-playback");
  playback.port.postMessage({ type: "pcm", epoch: 0, buffer: beforeCapture }); // Warm-up submission is transport-global only.
  created[0].currentTime = 12;
  await globals.__voiceE2E.play(0, 1);
  playback.port.postMessage({ type: "reset", epoch: 3 });
  playback.port.postMessage({ type: "mode", epoch: 3, realtime: true });
  created[0].currentTime = 12.02;
  const first = new globals.ArrayBuffer(4);
  new globals.Uint8Array(first).set([1, 0, 2, 0]);
  const second = new globals.ArrayBuffer(2);
  new globals.Uint8Array(second).set([3, 0]);
  socket.emit("message", first);
  playback.port.postMessage({ type: "pcm", epoch: 3, buffer: first });
  structuredClone(first, { transfer: [first] }); // The application transfers its original view to AudioWorklet.
  assert.equal(first.byteLength, 0);
  created[0].currentTime = 12.04;
  socket.emit("message", second);
  playback.port.postMessage({ type: "pcm", epoch: 3, buffer: second });
  structuredClone(second, { transfer: [second] });
  assert.equal(second.byteLength, 0);

  const capture = globals.__voiceE2E.export();
  assert.equal(capture.schema, "ngn-browser-voice-e2e/v2");
  assert.deepEqual(Array.from(capture.received, packet => [packet.sequence, packet.transportSequence, packet.sampleOffset,
    packet.receivedSamples, packet.measurementSampleOffset, packet.measurementReceivedSamples,
    packet.transportReceivedSamples, packet.samples, packet.sampleRate, packet.contextId, packet.epoch, packet.realtime]), [
    [0, 0, 0, 1, null, 0, 1, 1, 24000, 0, 0, false],
    [1, 1, 1, 3, 0, 2, 3, 2, 24000, 0, 3, true],
    [2, 2, 3, 4, 2, 3, 4, 1, 24000, 0, 3, true],
  ]);
  assert.deepEqual(Array.from(capture.received, packet => Buffer.from(packet.pcm16, "base64").toString("hex")), [
    "0909", "01000200", "0300",
  ]);
  assert.equal(capture.received[0].audioTime, 0);
  assert.equal(capture.received[1].audioTime, 12.02);
  assert.equal(capture.received[2].audioTime, 12.04);
  assert.equal(typeof capture.received[0].monotonicMs, "number");
  assert.equal(typeof capture.received[0].wallMs, "number");
  assert.equal(capture.receivedPcm.capturedBytes, 8);
  assert.equal(capture.receivedPcm.omittedPackets, 0);
  assert.equal(capture.clock.measurementStartedAudioTime, 12);
  const submissions = capture.events.filter(event => event.type === "playback.submit");
  assert.deepEqual(Array.from(submissions, event => [event.receivedSequence, event.submittedSamples,
    event.transportReceivedSamples, event.receiveMonotonicMs !== undefined]), [
    [0, 1, 1, true], [1, 3, 3, true], [2, 4, 4, true],
  ]);
  assert.equal(submissions[1].receivedSamples, 2); // This count is measurement-relative, unlike transport totals.
  assert.equal(submissions[1].receivedSequence, capture.received[1].transportSequence);
  assert.equal(submissions[1].audioTime, 12.02);
  const controls = capture.events.filter(event => event.type === "playback.control");
  assert.deepEqual(Array.from(controls, event => [event.controlType, event.epoch, event.realtime]), [
    ["reset", 3, false], ["mode", 3, true],
  ]);
  assert.equal(JSON.stringify(capture).includes("ngn.token.never-capture"), false);
  assert.equal("pcm" in capture.received[0], false);
});

test("received PCM is not falsely counted as submitted when a packet never reaches the playback port", async () => {
  const { globals } = fixture();
  const speaker = new globals.AudioContext();
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  new globals.AudioWorkletNode(speaker, "ngn-live-playback");
  const socket = new globals.WebSocket("ws://localhost/api/live/sessions/voice-owned/audio");
  await globals.__voiceE2E.play(0, 1);
  socket.emit("message", new globals.ArrayBuffer(8));
  const capture = globals.__voiceE2E.export();
  assert.equal(capture.receivedPcm.capturedPackets, 1);
  assert.equal(capture.events.filter(event => event.type === "playback.submit").length, 0);
});

test("measurement ends only after a matching checkpoint and complete trace prefix", async () => {
  const { globals, created } = fixture({ autoCheckpoint: false });
  new globals.AudioContext();
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  const socket = new globals.WebSocket("ws://localhost/api/live/sessions/voice-owned/audio");
  const playback = new globals.AudioWorkletNode(created[0], "ngn-live-playback");
  const playing = globals.__voiceE2E.play(0, 1);
  await new Promise(resolve => setImmediate(resolve));
  const begin = playback.posted.at(-1);
  playback.port.emit({ type: "trace-checkpoint", epoch: 0, requestId: begin.requestId, status: "completed",
    acceptedEnd: 0, renderedEnd: 0, receivedEnd: 0, renderedAt: 1, omittedIntervals: 0 });
  await playing;
  const packet = new globals.ArrayBuffer(4);
  socket.emit("message", packet);
  playback.port.postMessage({ type: "pcm", epoch: 0, buffer: packet });
  const finish = globals.__voiceE2E.finishMeasurement();
  const request = playback.posted.at(-1);
  assert.equal(request.type, "trace-checkpoint");
  const continuousTail = new globals.ArrayBuffer(4);
  socket.emit("message", continuousTail);
  playback.port.postMessage({ type: "pcm", epoch: 0, buffer: continuousTail });
  playback.port.emit({ type: "trace-checkpoint", epoch: 0, requestId: request.requestId, status: "completed",
    acceptedEnd: 2, renderedEnd: 2, receivedEnd: 4, renderedAt: 1 + 1 / 24000, omittedIntervals: 0 });
  let finished = false;
  void finish.then(() => { finished = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, false); // Response alone is not proof that the ACKed trace prefix arrived.
  playback.port.emit({ type: "trace", epoch: 0, sequence: 0, omittedIntervals: 0, batchIntervals: 1,
    intervals: [{ sourceStart: 0, samples: 2, at: 1, until: 1 + 1 / 24000 }] });
  const result = await finish;
  assert.equal(result.verified, true);
  assert.equal(result.acceptedEnd, 2); // Continuous RTP after the snapshot is a separate tail.
  assert.equal(result.receivedEnd, 4);
  const events = globals.__voiceE2E.export().events;
  assert.equal(events.filter(event => event.type === "measurement.end").length, 1);
  const checkpoint = events.find(event => event.type === "playback.trace-checkpoint");
  assert.equal(checkpoint.type, "playback.trace-checkpoint");
  assert.equal(checkpoint.status, "completed");
  assert.equal(events.find(event => event.type === "measurement.end").renderedEnd, 2);
  assert.equal(events.find(event => event.type === "playback.trace").samples, 2);
});

test("received PCM capture is byte-bounded, reports omissions, and never stores excess packets", async () => {
  const { globals } = fixture({ maxSeconds: 30 });
  const speaker = new globals.AudioContext();
  await globals.navigator.mediaDevices.getUserMedia({ audio: true });
  new globals.AudioWorkletNode(speaker, "ngn-live-playback");
  const socket = new globals.WebSocket("ws://localhost/api/live/sessions/voice-owned/audio");
  await globals.__voiceE2E.play(0, 1);
  const packet = new globals.ArrayBuffer(48000); // One second of accepted mono s16le at 24 kHz.
  for (let index = 0; index < 31; index++) socket.emit("message", packet);
  const capture = globals.__voiceE2E.export();
  assert.equal(capture.receivedPcm.byteLimit, 1_440_000);
  assert.equal(capture.receivedPcm.capturedPackets, 30);
  assert.equal(capture.receivedPcm.capturedBytes, 1_440_000);
  assert.equal(capture.receivedPcm.omittedPackets, 1);
  assert.equal(capture.receivedPcm.omittedBytes, 48_000);
  assert.equal(capture.received.length, 30);
  assert.equal(capture.received[29].sequence, 29);
  assert.equal(capture.received[29].sampleOffset, 29 * 24_000);
  assert.equal(capture.received[29].receivedSamples, 30 * 24_000);
  assert.equal(capture.receivedPcm.packetCount, 31);
  assert.equal(capture.receivedPcm.samples, 31 * 24_000);
  assert.equal(capture.omitted, 1);
  assert.equal(capture.events.filter(event => event.type === "harness.received-pcm-omitted").length, 1);
});
