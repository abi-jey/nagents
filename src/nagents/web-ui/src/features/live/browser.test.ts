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
    const tracks = [{ readyState: "live", stop: () => { stopped++; } }];
    grant({ getTracks: () => tracks, getAudioTracks: () => tracks } as unknown as MediaStream);
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

function browserFixture(devices: AudioDeviceSelection = { inputId: "", outputId: "" }, metering = false) {
  const names = ["navigator", "AudioContext", "AudioWorkletNode", "WebSocket", "location", "RTCPeerConnection"] as const;
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  const makeTrack = () => Object.assign(new EventTarget(), { enabled: true, readyState: "live", stops: 0, stop() { this.stops++; this.readyState = "ended"; } });
  const microphone = makeTrack();
  const stream = { getTracks: () => [microphone], getAudioTracks: () => [microphone] };
  const sent: ArrayBuffer[] = [], playback: boolean[] = [];
  const failures: string[] = [], sources: { stops: number; starts: number[]; onended(): void }[] = [];
  const constraints: MediaStreamConstraints[] = [], sinks: string[] = [];
  const levels: [number, number][] = [];
  const playbackCommands: { type: string; epoch: number; buffer?: ArrayBuffer }[] = [];
  let inputError: Error | undefined, outputError: Error | undefined, sinkResult = Promise.resolve();
  let inputResult = Promise.resolve(stream), graphError = false, resumeAllowed = true;
  const captureSources: { node: Node; stream: object }[] = [];
  let connected = false, ended = false, socket!: Socket, node!: WorkletNode, speaker!: WorkletNode, context!: Context;
  class Node {
    port = { onmessage: (_event: MessageEvent<ArrayBuffer>) => {}, close: () => {},
      postMessage: (message: { type: string; epoch: number; buffer?: ArrayBuffer }) => { playbackCommands.push(message); } };
    connections: object[] = [];
    disconnects = 0;
    connect(other: object) { this.connections.push(other); return other as this; }
    disconnect(target?: object) { this.disconnects++; this.connections = target ? this.connections.filter((value) => value !== target) : []; }
  }
  class Analyser extends Node {
    fftSize = 1024;
    amplitude = 0;
    getFloatTimeDomainData(samples: Float32Array) { samples.fill(this.amplitude); }
  }
  class WorkletNode extends Node {
    onprocessorerror = () => {};
    constructor(_context: object, name: string) { super(); if (name === "ngn-live-playback") speaker = this; else node = this; }
  }
  class Context {
    state = "suspended";
    currentTime = 0;
    sampleRate = 48000;
    destination = {};
    closes = 0;
    sinkId = "";
    onstatechange: (() => void) | null = null;
    analysers: Analyser[] = [];
    constructor() { context = this; }
    audioWorklet = { addModule: async (path: string) => { assert.ok(["/assets/live-capture.js", "/assets/live-playback.js"].includes(path)); } };
    createMediaStreamSource(value: object) { if (graphError) throw new Error("Graph unavailable"); const source = new Node(); captureSources.push({ node: source, stream: value }); return source; }
    createGain() { return Object.assign(new Node(), { gain: { value: 1 } }); }
    createAnalyser() { const analyser = new Analyser(); this.analysers.push(analyser); return analyser; }
    createBuffer(_channels: number, size: number, rate: number) {
      assert.equal(rate, 24_000);
      return { duration: size / rate, getChannelData: () => new Float32Array(size) };
    }
    createBufferSource() {
      const starts: number[] = [];
      const source = Object.assign(new Node(), { buffer: null, onended: () => {}, starts, stops: 0, start: (at: number) => { starts.push(at); }, stop() { this.stops++; } });
      sources.push(source); return source;
    }
    async resume() { if (resumeAllowed) this.state = "running"; }
    async setSinkId(id: string) { sinks.push(id); if (outputError) throw outputError; await sinkResult; this.sinkId = id; }
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
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: async (value: MediaStreamConstraints) => { constraints.push(value); if (inputError) throw inputError; return inputResult; } } } });
  Object.defineProperty(globalThis, "AudioContext", { configurable: true, value: Context });
  Object.defineProperty(globalThis, "AudioWorkletNode", { configurable: true, value: WorkletNode });
  Object.defineProperty(globalThis, "WebSocket", { configurable: true, value: Socket });
  Object.defineProperty(globalThis, "location", { configurable: true, value: { protocol: "http:", host: "127.0.0.1:8765" } });
  Object.defineProperty(globalThis, "RTCPeerConnection", { configurable: true, value: class { constructor() { assert.fail("WebRTC must not be used"); } } });
  const media = browserMedia({ connected: () => { connected = true; }, ended: () => { ended = true; }, failed: (message) => failures.push(message), playbackBlocked: (blocked) => playback.push(blocked),
    ...(metering ? { levels: (input: number, output: number) => { levels.push([input, output]); } } : {}),
  }, "websocket", devices);
  const epoch = () => playbackCommands.filter(command => ["reset", "barrier"].includes(command.type)).at(-1)?.epoch || 0;
  const heard = (buffer: ArrayBuffer, at = context.currentTime - .02, version = epoch()) => {
    speaker.port.onmessage?.({ data: { type: "played", epoch: version, buffer, at } } as unknown as MessageEvent<ArrayBuffer>);
  };
  return { media, microphone, sent, playback, failures, sources, constraints, sinks, captureSources, levels, playbackCommands, heard,
    speaker: () => speaker, epoch,
    packets: () => playbackCommands.filter(command => command.type === "pcm"),
    inputError: (cause: Error | undefined) => { inputError = cause; }, outputError: (cause: Error | undefined) => { outputError = cause; },
    sinkResult: (result: Promise<void>) => { sinkResult = result; },
    inputResult: (result: Promise<typeof stream>) => { inputResult = result; },
    graphError: (value: boolean) => { graphError = value; },
    resumeAllowed: (value: boolean) => { resumeAllowed = value; },
    newMicrophone: () => { const track = makeTrack(); return { track, stream: { getTracks: () => [track], getAudioTracks: () => [track] } }; },
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

function pcmFrame(value: number, samples = 480): ArrayBuffer {
  const bytes = new ArrayBuffer(samples * 2), view = new DataView(bytes);
  for (let i = 0; i < samples; i++) view.setInt16(i * 2, value, true);
  return bytes;
}

test("sphere PCM samples only sent capture and actual worklet playback, copying each source buffer", async (t) => {
  const f = browserFixture(); t.after(f.restore);
  assert(f.media.sampleAudio);
  await f.media.prepare(new AbortController().signal);
  const context = f.context(); context.currentTime = 1;
  const input = pcmFrame(8192);
  f.node().port.onmessage({ data: input } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sent.length, 0); assert.equal(f.media.sampleAudio().input.active, false);
  const opening = f.media.connect("voice-id", "web-token");
  f.socket().readyState = 0;
  f.node().port.onmessage({ data: input } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sent.length, 0);
  f.socket().readyState = 1; f.socket().onopen(); await opening;
  f.node().port.onmessage({ data: input } as MessageEvent<ArrayBuffer>);
  const captured = f.media.sampleAudio().input;
  assert(captured.active && captured.rms > .18 && captured.rms < .21);
  new Uint8Array(input).fill(0);
  assert.equal(f.media.sampleAudio().input.rms, captured.rms, "queued capture must not alias the worklet buffer");

  const first = pcmFrame(8192), second = pcmFrame(-16384);
  f.socket().onmessage({ data: first } as MessageEvent<ArrayBuffer>);
  f.socket().onmessage({ data: second } as MessageEvent<ArrayBuffer>);
  assert.equal(f.packets().length, 2);
  assert.equal(f.sources.length, 0, "network frames must not create UI-thread AudioBufferSource schedules");
  assert.equal(f.media.sampleAudio().output.active, false, "received speech must not animate before actual worklet playback");
  context.currentTime = 1.04;
  f.heard(first, 1.02);
  const audible = f.media.sampleAudio().output.rms;
  assert(audible > .15);
  new Uint8Array(first).fill(0);
  assert.equal(f.media.sampleAudio().output.rms, audible, "played PCM must be copied before buffers can be reused");
  context.currentTime = 1.06; f.heard(second, 1.04);
  assert(f.media.sampleAudio().output.rms > audible);
  context.currentTime = 1.5;
  assert.equal(f.media.sampleAudio().output.active, false, "silence following the last played PCM clears the sphere");
  assert.equal(f.constraints.length, 1, "PCM analysis does not acquire another microphone");
  assert.deepEqual(f.failures, []);
});

test("sphere PCM ownership resets on mute, context suspension, device changes, reconnect and close", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  assert(f.media.sampleAudio); const context = f.context(); context.currentTime = 1;
  const feed = () => {
    f.node().port.onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
    f.socket().onmessage({ data: pcmFrame(16384, 4800) } as MessageEvent<ArrayBuffer>);
    f.heard(pcmFrame(16384));
  };
  feed(); assert(f.media.sampleAudio().input.rms > 0); assert(f.media.sampleAudio().output.rms > 0);
  f.media.muteInput(true); f.media.muteOutput(true);
  assert.equal(f.media.sampleAudio().input.rms, 0); assert.equal(f.media.sampleAudio().output.rms, 0);
  f.node().port.onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
  assert(new Uint8Array(f.sent.at(-1)!).every(value => value === 0));
  f.media.muteInput(false); f.media.muteOutput(false);
  assert.equal(f.media.sampleAudio().input.active, false); assert.equal(f.media.sampleAudio().output.active, false);
  feed(); context.currentTime += .025;
  context.state = "suspended"; context.onstatechange?.();
  assert.equal(f.media.sampleAudio().input.rms, 0); assert.equal(f.media.sampleAudio().output.rms, 0);
  await f.media.play(); assert.equal(f.media.sampleAudio().input.active, false); assert.equal(f.media.sampleAudio().output.active, false);
  feed(); assert(f.media.sampleAudio().input.active);
  const replacement = f.newMicrophone(); f.inputResult(Promise.resolve(replacement.stream));
  await f.media.setInputDevice("new-input"); assert.equal(f.media.sampleAudio().input.rms, 0);
  assert(f.media.sampleAudio().output.rms > 0);
  await f.media.setOutputDevice("new-output"); assert.equal(f.media.sampleAudio().output.rms, 0);
  feed(); const next = f.media.connect("second-session", "web-token"); f.socket().onopen(); await next;
  assert.equal(f.media.sampleAudio().input.active, false); assert.equal(f.media.sampleAudio().output.active, false);
  feed(); f.media.close(); assert.equal(f.media.sampleAudio().input.rms, 0); assert.equal(f.media.sampleAudio().output.rms, 0);
  assert.deepEqual(f.failures, []);
});

test("unavailable optional PCM queues never interrupt the existing voice transport", async (t) => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, "WritableStream");
  Object.defineProperty(globalThis, "WritableStream", { configurable: true, value: undefined });
  t.after(() => { if (saved) Object.defineProperty(globalThis, "WritableStream", saved); else Reflect.deleteProperty(globalThis, "WritableStream"); });
  const f = browserFixture(); t.after(f.restore); await f.open();
  assert(f.media.sampleAudio);
  f.node().port.onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
  f.socket().onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sent.length, 1); assert.equal(f.packets().length, 1);
  assert.equal(f.media.sampleAudio().input.active, false); assert.equal(f.media.sampleAudio().output.active, false);
  assert.deepEqual(f.failures, []); assert.equal(f.connected(), true);
});

for (const invalid of ["ended", "missing"] as const) test(`relay refuses an initially ${invalid} microphone before connecting audio`, async (t) => {
  const f = browserFixture(); t.after(f.restore);
  const capture = f.newMicrophone();
  if (invalid === "ended") capture.track.readyState = "ended";
  else capture.stream.getAudioTracks = () => [];
  f.inputResult(Promise.resolve(capture.stream));
  await assert.rejects(f.media.prepare(new AbortController().signal), /microphone did not provide live audio.*System default/);
  assert.equal(capture.track.stops, 1);
  assert.equal(f.captureSources.length, 0);
  assert.equal(f.socket(), undefined);
  assert.equal(f.connected(), false);
  assert.equal(f.context().closes, 1);
});

test("relay meters existing capture and actual playback graphs, gates mute, follows device changes and releases analysis", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const f = browserFixture(undefined, true); t.after(f.restore);
  await f.open();
  const context = f.context();
  context.analysers[0].amplitude = 0.125; context.analysers[1].amplitude = 0.25;
  t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 0]);
  assert.equal(f.constraints.length, 1, "analysis reuses the already-authorized microphone");
  assert.equal(f.captureSources.length, 1, "capture and metering share the same source node");
  f.socket().onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
  t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 1]);
  f.media.muteInput(true); assert.deepEqual(f.levels.at(-1), [0, 1]);
  f.media.muteOutput(true); assert.deepEqual(f.levels.at(-1), [0, 0]);
  const replacement = f.newMicrophone(); f.inputResult(Promise.resolve(replacement.stream));
  await f.media.setInputDevice("other-mic");
  assert.equal(f.captureSources[0].node.connections.length, 0);
  assert.ok(f.captureSources[1].node.connections.includes(context.analysers[0]));
  f.media.muteInput(false); t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 0]);
  context.state = "suspended"; context.onstatechange?.(); assert.deepEqual(f.levels.at(-1), [0, 0]);
  await f.media.play(); t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 0]);
  f.media.close(); assert.deepEqual(f.levels.at(-1), [0, 0]);
  assert.ok(context.analysers.every((node) => !node.connections.length));
  const count = f.levels.length;
  t.mock.timers.tick(1_000); assert.equal(f.levels.length, count);
  assert.equal(context.closes, 1);
});

test("microphone and playback use only the ngn serve WebSocket, release devices and mute pending frames", async (t) => {
  const f = browserFixture(); t.after(f.restore);
  await f.open();
  assert.deepEqual(f.constraints, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }]);
  assert.deepEqual(f.sinks, [], "fresh playback follows the system default without pinning a sink");
  assert.equal(f.connected(), true);
  assert.equal(f.socket().url, "ws://127.0.0.1:8765/api/live/sessions/voice-id/audio");
  assert.deepEqual(f.socket().protocols, ["ngn.live.v2", "ngn.token.web-token"]);
  assert.equal(f.socket().binaryType, "arraybuffer");
  const frame = new Int16Array(480).fill(1234).buffer;
  f.node().port.onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sent[0], frame);
  f.socket().onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  f.media.muteInput(true); f.media.muteOutput(true);
  f.node().port.onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.microphone.enabled, false);
  assert.ok(new Int16Array(f.sent[1]).every((sample) => sample === 0));
  assert(f.epoch() > 1, "mute clears the playback queue with a fresh epoch");
  f.socket().onmessage({ data: frame } as MessageEvent<ArrayBuffer>);
  assert.equal(f.packets().length, 1, "muted output is discarded");
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
  assert.equal(f.playback.at(-1), true); assert(f.epoch() > 1);
  await f.media.play();
  assert.equal(f.playback.at(-1), false);
  f.node().onprocessorerror();
  assert.match(f.failures[0], /processing stopped/);
});

test("playback backlog and invalid or stalled relay frames stay bounded", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  for (let index = 0; index < 40; index++) f.socket().onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>);
  assert.equal(f.sources.length, 0, "only the audio-thread bounded queue owns playout");
  assert.equal(f.packets().length, 40);
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

test("relay microphone switching keeps its socket and worklet, preserving the latest mute state", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  const replacement = f.newMicrophone(), socket = f.socket(), worklet = f.node();
  let grant!: (stream: typeof replacement.stream) => void;
  f.inputResult(new Promise((resolve) => { grant = resolve; }));
  const changing = f.media.setInputDevice("other-mic"); await setImmediate();
  assert.equal(f.microphone.stops, 0); assert.equal(f.captureSources.length, 1);
  f.media.muteInput(true); grant(replacement.stream); await changing;
  assert.equal(replacement.track.enabled, false); assert.equal(f.microphone.stops, 1);
  assert.equal(f.captureSources[0].node.disconnects, 1); assert.equal(f.captureSources[1].stream, replacement.stream);
  assert.equal(f.captureSources[1].node.connections[0], worklet);
  assert.equal(f.socket(), socket); assert.equal(f.node(), worklet); assert.equal(socket.readyState, 1);
  f.microphone.dispatchEvent(new Event("ended")); assert.deepEqual(f.failures, []);
  f.media.muteInput(false); assert.equal(replacement.track.enabled, true);
});

test("failed relay input swaps keep the previous microphone and allow a later retry", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  f.inputError(new DOMException("Denied", "NotAllowedError"));
  await assert.rejects(f.media.setInputDevice("denied-mic"), /selected microphone/);
  assert.equal(f.microphone.stops, 0); assert.equal(f.microphone.enabled, true);
  f.inputError(undefined); f.graphError(true);
  const rejected = f.newMicrophone(); f.inputResult(Promise.resolve(rejected.stream));
  await assert.rejects(f.media.setInputDevice("graph-fails"), /previous microphone is still selected/);
  assert.equal(rejected.track.stops, 1); assert.equal(f.microphone.stops, 0); assert.equal(f.captureSources[0].node.disconnects, 0);
  f.graphError(false); const good = f.newMicrophone(); f.inputResult(Promise.resolve(good.stream));
  await f.media.setInputDevice("");
  assert.equal(f.microphone.stops, 1); assert.equal(good.track.enabled, true);
  assert.deepEqual(f.constraints.at(-1), { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  assert.deepEqual(f.failures, []);
});

test("relay input replacements serialize and close rejects queued swaps while disposing late permissions", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  const late = f.newMicrophone(); let grant!: (stream: typeof late.stream) => void;
  f.inputResult(new Promise((resolve) => { grant = resolve; }));
  const first = f.media.setInputDevice("first"), second = f.media.setInputDevice("second");
  const rejected = Promise.all([assert.rejects(first, { name: "AbortError" }), assert.rejects(second, { name: "AbortError" })]);
  await setImmediate(); assert.equal(f.constraints.length, 2, "the second permission request waits for the first swap");
  f.media.close(); await rejected;
  grant(late.stream); await setImmediate();
  assert.equal(late.track.stops, 1); assert.equal(late.track.enabled, false);
  assert.equal(f.microphone.stops, 1); assert.equal(f.captureSources.length, 1); assert.equal(f.constraints.length, 2);
});

test("unplugging the old relay microphone during a successful swap does not terminate the call", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  const replacement = f.newMicrophone(); let grant!: (stream: typeof replacement.stream) => void;
  f.inputResult(new Promise((resolve) => { grant = resolve; }));
  const changing = f.media.setInputDevice("replacement"); await setImmediate();
  f.microphone.readyState = "ended"; f.microphone.dispatchEvent(new Event("ended"));
  assert.deepEqual(f.failures, []);
  grant(replacement.stream); await changing; assert.deepEqual(f.failures, []);
  replacement.track.readyState = "ended"; replacement.track.dispatchEvent(new Event("ended"));
  assert.match(f.failures[0], /microphone was disconnected/);
});

test("relay speaker changes preserve mute, reset to system default, and retain the route on failure", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  const socket = f.socket(), context = f.context(); f.media.muteOutput(true);
  await f.media.setOutputDevice("headphones"); assert.equal(context.sinkId, "headphones");
  f.socket().onmessage({ data: new ArrayBuffer(960) } as MessageEvent<ArrayBuffer>); assert.equal(f.packets().length, 0);
  await f.media.setOutputDevice(""); assert.deepEqual(f.sinks, ["headphones", ""]); assert.equal(context.sinkId, "");
  f.outputError(new DOMException("Missing", "NotFoundError"));
  await assert.rejects(f.media.setOutputDevice("missing"), /selected speaker/);
  assert.equal(context.sinkId, ""); assert.equal(f.socket(), socket); assert.equal(context.closes, 0);
  f.outputError(undefined); await f.media.setOutputDevice("speakers"); assert.equal(context.sinkId, "speakers");
});

test("relay output swaps serialize and pending sink selection aborts immediately on close", async (t) => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  let finish!: () => void; f.sinkResult(new Promise<void>((resolve) => { finish = resolve; }));
  const first = f.media.setOutputDevice("first"), second = f.media.setOutputDevice("second");
  const rejected = Promise.all([assert.rejects(first, { name: "AbortError" }), assert.rejects(second, { name: "AbortError" })]);
  await setImmediate(); assert.deepEqual(f.sinks, ["first"]);
  f.media.close(); await rejected; finish(); await setImmediate();
  assert.deepEqual(f.sinks, ["first"]); assert.equal(f.context().closes, 1); assert.equal(f.socket().readyState, 3);
});

test("obsolete direct provider transport is rejected before acquiring browser media", (t) => {
  const f = browserFixture(); t.after(f.restore);
  const transport = "webrtc" as import("./types.js").LiveTransport;
  assert.match(liveSupport(transport), /server voice relay/);
  assert.throws(() => browserMedia({ connected: () => {}, ended: () => {}, failed: () => {}, playbackBlocked: () => {} }, transport), /ngn server relay/);
  assert.deepEqual(f.constraints, []);
  assert.equal(f.socket(), undefined);
});

test("a server session identifier cannot change the audio socket origin", async (t) => {
  const f = browserFixture(); t.after(f.restore);
  await f.media.prepare(new AbortController().signal);
  const id = "https://provider.example/voice?token=secret";
  const opening = f.media.connect(id, "ngn-token");
  assert.equal(f.socket().url, `ws://127.0.0.1:8765/api/live/sessions/${encodeURIComponent(id)}/audio`);
  f.socket().onopen(); await opening;
  assert.equal(f.connected(), true);
});

test("output worklet epochs fence delayed sphere messages through mute, suspension and close", async t => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  assert(f.media.sampleAudio); const context = f.context(); context.currentTime = 1;
  const old = f.epoch(); f.heard(pcmFrame(8192)); assert(f.media.sampleAudio().output.rms > 0);
  f.media.muteOutput(true); f.media.muteOutput(false);
  f.heard(pcmFrame(8192), .98, old); assert.equal(f.media.sampleAudio().output.rms, 0);
  f.heard(pcmFrame(8192)); assert(f.media.sampleAudio().output.rms > 0);
  context.state = "suspended"; context.onstatechange?.();
  const suspended = f.epoch();
  f.socket().onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
  assert.equal(f.packets().length, 0, "suspended playback must not build a stale speech queue");
  await f.media.play(); f.heard(pcmFrame(8192), .98, suspended - 1);
  assert.equal(f.media.sampleAudio().output.rms, 0);
  const node = f.speaker(); f.media.close();
  assert.equal(node.port.onmessage, null); assert(node.disconnects > 0);
  assert.equal(f.sources.length, 0);
});

test("speaker switching retains the continuous playout node and clock", async t => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  const node = f.speaker(), epoch = f.epoch(), socket = f.socket();
  await f.media.setOutputDevice("headphones");
  assert.equal(f.speaker(), node); assert.equal(f.epoch(), epoch + 1); assert.equal(f.socket(), socket);
  assert.equal(f.playbackCommands.at(-1)?.type, "barrier", "device change only fences telemetry, preserving queued audio");
  f.context().currentTime = 2; f.heard(pcmFrame(8192), 1.98, epoch);
  assert.equal(f.media.sampleAudio!().output.rms, 0, "pre-switch reports cannot repopulate the sphere");
  f.context().currentTime = 2; f.heard(pcmFrame(8192), 1.98);
  assert(f.media.sampleAudio!().output.rms > 0);
  assert.equal(f.sources.length, 0);
});

test("a server interrupt clears queued speech and late sphere reports without reconnecting", async t => {
  const f = browserFixture(); t.after(f.restore); await f.open();
  f.context().currentTime = 1; f.heard(pcmFrame(8192));
  const epoch = f.epoch(), socket = f.socket();
  assert(f.media.sampleAudio!().output.rms > 0);
  socket.onmessage({ data: '{"type":"interrupt"}' } as unknown as MessageEvent<ArrayBuffer>);
  assert.equal(f.epoch(), epoch + 1); assert.equal(f.media.sampleAudio!().output.rms, 0);
  f.heard(pcmFrame(8192), .98, epoch); assert.equal(f.media.sampleAudio!().output.rms, 0);
  assert.equal(f.socket(), socket); assert.equal(f.socket().readyState, 1); assert.deepEqual(f.failures, []);
  socket.onmessage({ data: '{"type":"execute","prompt":"untrusted"}' } as unknown as MessageEvent<ArrayBuffer>);
  assert.match(f.failures.at(-1)!, /invalid frame/);
});

test("playback health accepts numeric counters only and never logs worklet payload extras", async t => {
  const logs: unknown[][] = []; t.mock.method(console, "info", (...args: unknown[]) => { logs.push(args); });
  const f = browserFixture(); t.after(f.restore); await f.open();
  const health = { receivedSamples: 480, playedSamples: 300, underruns: 0, droppedSamples: 0, bufferedMs: 7.5,
    peakBufferedMs: 40, targetMs: 40, sampleRate: 48000, correctionPpm: 10 };
  f.speaker().port.onmessage({ data: { type: "health", epoch: f.epoch(), ...health, transcript: "never-log-this", provider: "private" } } as unknown as MessageEvent<ArrayBuffer>);
  assert.deepEqual(f.media.audioHealth!(), health);
  assert.equal(f.playbackCommands.at(-1)?.type, "health-ack");
  const snapshot = f.media.audioHealth!(); snapshot.receivedSamples = 999;
  assert.equal(f.media.audioHealth!().receivedSamples, 480);
  f.media.close();
  assert.equal(logs.length, 1); assert.equal(logs[0][0], "ngn voice playback health");
  assert.doesNotMatch(JSON.stringify(logs), /never-log-this|private|transcript|provider/);
});

test("an open audio socket stays connecting until the capture context is running", async t => {
  const f = browserFixture(); t.after(f.restore);
  f.resumeAllowed(false);
  await f.media.prepare(new AbortController().signal);
  assert.equal(f.context().state, "suspended");
  const opening = f.media.connect("voice-id", "web-token");
  f.socket().onopen(); await opening;
  assert.equal(f.connected(), false, "WebSocket attachment alone does not mean the mic is live");
  assert.equal(f.playback.at(-1), true, "the user can enable audio while still connecting");
  f.resumeAllowed(true); await f.media.play();
  assert.equal(f.connected(), true);
  assert.equal(f.playback.at(-1), false);
});

test("a source-muted microphone cannot report ready until its live track resumes", async t => {
  const f = browserFixture(); t.after(f.restore);
  Object.defineProperty(f.microphone, "muted", { value: true, configurable: true });
  await f.media.prepare(new AbortController().signal);
  const opening = f.media.connect("voice-id", "web-token");
  f.socket().onopen(); await opening;
  assert.equal(f.connected(), false);
  Object.defineProperty(f.microphone, "muted", { value: false });
  f.microphone.dispatchEvent(new Event("unmute"));
  assert.equal(f.connected(), true);
});

test("late audio resume cannot resurrect a cancelled connection", async t => {
  const f = browserFixture(); t.after(f.restore);
  f.resumeAllowed(false);
  await f.media.prepare(new AbortController().signal);
  const opening = f.media.connect("voice-id", "web-token"); f.socket().onopen(); await opening;
  f.media.close();
  f.resumeAllowed(true); await f.media.play(); f.microphone.dispatchEvent(new Event("unmute"));
  assert.equal(f.connected(), false);
});

test("an intentional mute before socket attachment permits a speaker-only connection and can be undone", async t => {
  const f = browserFixture(); t.after(f.restore);
  await f.media.prepare(new AbortController().signal);
  f.media.muteInput(true);
  const opening = f.media.connect("voice-id", "web-token");
  assert.equal(f.connected(), false);
  f.socket().onopen(); await opening;
  assert.equal(f.connected(), true, "an intentional input mute is not failed hardware or pending transport");
  assert.equal(f.microphone.enabled, false);
  f.node().port.onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
  assert(new Uint8Array(f.sent.at(-1)!).every(value => value === 0));
  assert.equal(f.media.sampleAudio!().input.active, false);
  f.media.muteInput(false);
  f.context().currentTime = 1;
  f.node().port.onmessage({ data: pcmFrame(8192) } as MessageEvent<ArrayBuffer>);
  assert.equal(f.microphone.enabled, true);
  assert.equal(f.connected(), true);
  assert(f.media.sampleAudio!().input.rms > 0);
});

test("enabling a pending microphone rechecks readiness without relying on a track unmute event", async t => {
  const f = browserFixture(); t.after(f.restore);
  await f.media.prepare(new AbortController().signal);
  f.microphone.enabled = false;
  const opening = f.media.connect("voice-id", "web-token"); f.socket().onopen(); await opening;
  assert.equal(f.connected(), false);
  f.media.muteInput(false);
  assert.equal(f.connected(), true);
});
