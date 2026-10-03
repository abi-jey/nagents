import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { browserMedia, liveSupport } from "./browser.js";
import type { AudioDeviceSelection } from "./types.js";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}

function fixture(devices: AudioDeviceSelection = { inputId: "", outputId: "" }, metering = false) {
  const names = ["navigator", "Audio", "AudioContext", "RTCPeerConnection", "MediaStream", "isSecureContext"] as const;
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  const makeTrack = () => Object.assign(new EventTarget(), { enabled: true, readyState: "live", stops: 0, stop() { this.stops++; this.readyState = "ended"; } });
  const microphone = makeTrack();
  class Stream {
    constructor(readonly tracks = [microphone]) {}
    getTracks() { return this.tracks; }
    getAudioTracks() { return this.tracks; }
  }
  const stream = new Stream(), steps: string[] = [], failures: string[] = [], playback: boolean[] = [];
  const constraints: MediaStreamConstraints[] = [], sinks: string[] = [];
  const levels: [number, number][] = [];
  let context: Context | undefined;
  class Node {
    connections = new Set<object>();
    connect(target: object) { this.connections.add(target); return target; }
    disconnect(target?: object) { if (target) this.connections.delete(target); else this.connections.clear(); }
  }
  class Analyser extends Node {
    fftSize = 1024;
    amplitude = 0;
    getFloatTimeDomainData(samples: Float32Array) { samples.fill(this.amplitude); }
  }
  class Context {
    state = "suspended";
    closes = 0;
    destination = {};
    analysers: Analyser[] = [];
    sources: { source: Stream; node: Node }[] = [];
    constructor() { context = this; }
    createAnalyser() { const node = new Analyser(); this.analysers.push(node); return node; }
    createGain() { return Object.assign(new Node(), { gain: { value: 1 } }); }
    createMediaStreamSource(source: Stream) { const node = new Node(); this.sources.push({ source, node }); return node; }
    async resume() { this.state = "running"; }
    async close() { this.closes++; this.state = "closed"; }
  }
  let inputError: Error | undefined, outputError: Error | undefined, sinkResult = Promise.resolve();
  let permission = Promise.resolve(stream), offer = Promise.resolve({ type: "offer" as const, sdp: "offer-sdp" });
  let answer = Promise.resolve(), audio!: AudioFixture, peer!: Peer;
  let replacementResult = Promise.resolve(), replacementError = false;
  const replacements: (typeof microphone)[] = [];
  const sender = { track: microphone, async replaceTrack(track: typeof microphone) {
    replacements.push(track); if (replacementError) throw new Error("Replacement failed");
    await replacementResult; this.track = track;
  } };
  let connections = 0, ends = 0, blockPlayback = false;
  const channel = { onclose: () => {}, onerror: () => {}, closes: 0,
    close() { this.closes++; this.onclose(); }, send: () => assert.fail("Only the server sideband may send provider commands") };
  class AudioFixture {
    srcObject: Stream | null = null;
    autoplay = false;
    muted = false;
    pauses = 0;
    sinkId = "";
    onplaying = () => {};
    onpause = () => {};
    onerror = () => {};
    constructor() { audio = this; }
    async play() { if (blockPlayback) throw new DOMException("Blocked", "NotAllowedError"); this.onplaying(); }
    async setSinkId(id: string) { sinks.push(id); if (outputError) throw outputError; await sinkResult; this.sinkId = id; }
    pause() { this.pauses++; this.onpause(); }
  }
  class Peer {
    connectionState = "new";
    closes = 0;
    onconnectionstatechange = () => {};
    ontrack = (_event: { streams: Stream[]; track: typeof microphone }) => {};
    local?: RTCSessionDescriptionInit;
    remote?: RTCSessionDescriptionInit;
    constructor() { peer = this; }
    createDataChannel(name: string) { assert.equal(name, "oai-events"); steps.push("channel"); return channel; }
    addTrack(track: typeof microphone, source: Stream) { assert.equal(source, stream); assert.equal(track.enabled, false); steps.push("track"); return sender; }
    async createOffer() { steps.push("offer"); return offer; }
    async setLocalDescription(value: RTCSessionDescriptionInit) { this.local = value; }
    async setRemoteDescription(value: RTCSessionDescriptionInit) { this.remote = value; return answer; }
    close() { this.closes++; this.connectionState = "closed"; this.onconnectionstatechange(); }
    state(value: string) { this.connectionState = value; this.onconnectionstatechange(); }
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: async (value: MediaStreamConstraints) => { constraints.push(value); if (inputError) throw inputError; return permission; } } } });
  Object.defineProperty(globalThis, "Audio", { configurable: true, value: AudioFixture });
  Object.defineProperty(globalThis, "RTCPeerConnection", { configurable: true, value: Peer });
  Object.defineProperty(globalThis, "MediaStream", { configurable: true, value: Stream });
  Object.defineProperty(globalThis, "isSecureContext", { configurable: true, value: true });
  if (metering) Object.defineProperty(globalThis, "AudioContext", { configurable: true, value: Context });
  const media = browserMedia({ connected: () => { connections++; }, ended: () => { ends++; }, failed: (message) => failures.push(message), playbackBlocked: (blocked) => playback.push(blocked),
    ...(metering ? { levels: (input: number, output: number) => { levels.push([input, output]); } } : {}),
  }, "webrtc", devices);
  return { media, stream, microphone, steps, channel, failures, playback, constraints, sinks, sender, replacements, levels, context: () => context!,
    inputError: (cause: Error | undefined) => { inputError = cause; }, outputError: (cause: Error | undefined) => { outputError = cause; },
    sinkResult: (result: Promise<void>) => { sinkResult = result; },
    replacementResult: (result: Promise<void>) => { replacementResult = result; },
    replacementError: (value: boolean) => { replacementError = value; },
    newMicrophone: () => { const track = makeTrack(); return { track, stream: new Stream([track]) }; },
    withoutOutputRouting: () => { Object.defineProperty(AudioFixture.prototype, "setSinkId", { value: undefined }); },
    audio: () => audio, peer: () => peer, connections: () => connections, ends: () => ends,
    permission: (value: Promise<Stream>) => { permission = value; }, offer: (value: typeof offer) => { offer = value; },
    answer: (value: Promise<void>) => { answer = value; }, blockPlayback: (value: boolean) => { blockPlayback = value; },
    prepare: () => media.prepare(new AbortController().signal),
    restore: () => {
      media.close();
      for (const [name, descriptor] of saved) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor); else Reflect.deleteProperty(globalThis, name);
      }
    },
  };
}

for (const invalid of ["ended", "missing"] as const) test(`login refuses an initially ${invalid} microphone before preparing a provider offer`, async (t) => {
  const f = fixture(); t.after(f.restore);
  if (invalid === "ended") f.microphone.readyState = "ended";
  else f.stream.getAudioTracks = () => [];
  await assert.rejects(f.prepare(), /microphone did not provide live audio.*System default/);
  assert.equal(f.microphone.stops, 1);
  assert.deepEqual(f.steps, []);
  assert.equal(f.connections(), 0);
  assert.equal(f.media.offer?.(), "");
});

test("login level analysis reuses existing media streams and stops before RTC finalization", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const f = fixture(undefined, true); t.after(f.restore);
  await f.prepare();
  const context = f.context();
  context.analysers[0].amplitude = 0.125; context.analysers[1].amplitude = 0.25;
  t.mock.timers.tick(100); assert.deepEqual(f.levels, [], "permission and SDP preparation remain silent");
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 0]);
  const remote = f.newMicrophone();
  f.blockPlayback(true); f.peer().ontrack({ streams: [remote.stream], track: remote.track }); await setImmediate();
  t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 0], "blocked output has no audible speech level");
  f.blockPlayback(false); await f.media.play(); t.mock.timers.tick(100);
  assert.deepEqual(f.levels.at(-1), [0.5, 1]);
  assert.equal(f.audio().srcObject, remote.stream, "analysis does not replace browser playback or its sink");
  assert.deepEqual(context.sources.map((item) => item.source), [f.stream, remote.stream]);
  assert.equal(f.constraints.length, 1);
  f.media.muteInput(true); f.media.muteOutput(true); assert.deepEqual(f.levels.at(-1), [0, 0]);
  const replacement = f.newMicrophone(); f.permission(Promise.resolve(replacement.stream));
  await f.media.setInputDevice("other-mic");
  assert.equal(context.sources[0].node.connections.size, 0);
  assert.equal(context.sources.at(-1)?.source, replacement.stream);
  f.media.muteInput(false); f.media.muteOutput(false); await setImmediate();
  t.mock.timers.tick(100); assert.deepEqual(f.levels.at(-1), [0.5, 1]);
  f.media.stop?.(); assert.deepEqual(f.levels.at(-1), [0, 0]);
  assert.equal(context.closes, 1); assert.equal(f.peer().closes, 0, "quiet RTC remains available for server finalization");
  assert.ok(context.sources.every((item) => !item.node.connections.size));
  const count = f.levels.length; t.mock.timers.tick(1_000); assert.equal(f.levels.length, count);
  f.media.close(); assert.equal(context.closes, 1); assert.equal(f.peer().closes, 1);
});

test("login media produces an offer with muted input and connects only after SDP plus actual peer connection", async (t) => {
  const f = fixture(); t.after(f.restore);
  assert.equal(liveSupport("webrtc"), "");
  await f.prepare();
  assert.deepEqual(f.constraints, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }]);
  assert.deepEqual(f.sinks, [], "fresh playback follows the system default without pinning a sink");
  assert.deepEqual(f.steps, ["channel", "track", "offer"]);
  assert.equal(f.media.offer?.(), "offer-sdp"); assert.equal(f.microphone.enabled, false);
  f.peer().state("connected");
  assert.equal(f.connections(), 0); assert.equal(f.microphone.enabled, false);
  await f.media.connect("local-session", "local-token", "answer-sdp");
  assert.deepEqual(f.peer().remote, { type: "answer", sdp: "answer-sdp" });
  assert.equal(f.connections(), 1); assert.equal(f.microphone.enabled, true);
  f.peer().state("connected"); assert.equal(f.connections(), 1);
  f.media.muteInput(true); assert.equal(f.microphone.enabled, false);
  f.media.muteInput(false); assert.equal(f.microphone.enabled, true);
  f.media.close(); f.media.close();
  assert.equal(f.microphone.stops, 1); assert.equal(f.peer().closes, 1); assert.equal(f.channel.closes, 1);
  assert.equal(f.ends(), 0); assert.deepEqual(f.failures, []);
});

test("cancelling microphone permission stops a late stream and never opens a peer", async (t) => {
  const f = fixture(); t.after(f.restore);
  const permission = deferred<typeof f.stream>(); f.permission(permission.promise);
  const abort = new AbortController(), preparing = f.media.prepare(abort.signal);
  abort.abort(); permission.resolve(f.stream);
  await assert.rejects(preparing, { name: "AbortError" });
  assert.equal(f.microphone.stops, 1); assert.equal(f.peer(), undefined);
});

test("late offer or answer cannot resurrect cancelled browser audio", async (t) => {
  const f = fixture(); t.after(f.restore);
  const offer = deferred<{ type: "offer"; sdp: string }>(); f.offer(offer.promise);
  const preparing = f.prepare(); await setImmediate(); f.media.close();
  offer.resolve({ type: "offer", sdp: "late-offer" });
  await assert.rejects(preparing, { name: "AbortError" });
  assert.equal(f.media.offer?.(), ""); assert.equal(f.peer().local, undefined); assert.equal(f.microphone.stops, 1);
});

test("cancelling while applying the answer never re-enables the microphone", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  const answer = deferred<void>(); f.answer(answer.promise);
  const connecting = f.media.connect("local-session", "local-token", "answer-sdp");
  f.media.close(); answer.resolve(); await assert.rejects(connecting, { name: "AbortError" });
  f.peer().state("connected");
  assert.equal(f.connections(), 0); assert.equal(f.microphone.enabled, false);
});

test("remote tracks recover blocked playback, preserve speaker mute and release the audio element", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  f.blockPlayback(true);
  f.peer().ontrack({ streams: [], track: f.microphone }); await setImmediate();
  assert.ok(f.audio().srcObject); assert.equal(f.playback.at(-1), true);
  f.blockPlayback(false); await f.media.play(); assert.equal(f.playback.at(-1), false);
  f.media.muteOutput(true); assert.equal(f.audio().muted, true);
  f.audio().onpause(); assert.equal(f.playback.at(-1), false);
  f.media.muteOutput(false); await setImmediate(); assert.equal(f.audio().muted, false);
  f.audio().onpause(); assert.equal(f.playback.at(-1), true);
  f.audio().onerror(); assert.match(f.failures[0], /could not be played/);
  f.media.close(); assert.equal(f.audio().srcObject, null); assert.equal(f.audio().pauses, 1);
});

test("temporary ICE interruption recovers, persistent loss fails and clean channel close reconciles status", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  f.peer().state("disconnected"); t.mock.timers.tick(4_999); assert.deepEqual(f.failures, []);
  f.peer().state("connected"); t.mock.timers.tick(5_000); assert.deepEqual(f.failures, []);
  f.peer().state("disconnected"); t.mock.timers.tick(5_000); assert.match(f.failures[0], /interrupted/);
  f.channel.onclose(); assert.equal(f.ends(), 1);
  f.microphone.dispatchEvent(new Event("ended")); assert.match(f.failures[1], /microphone was disconnected/);
});

test("a missing answer cannot mark the session connected or release input", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await assert.rejects(f.media.connect("local-session", "local-token"), /did not return an audio connection/);
  assert.equal(f.microphone.enabled, false); assert.equal(f.connections(), 0);
});

test("stopping login devices preserves RTC for finalization and rejects late playback or input", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  f.peer().ontrack({ streams: [f.stream], track: f.microphone }); await setImmediate();
  f.media.stop?.(); f.media.stop?.();
  assert.equal(f.microphone.stops, 1); assert.equal(f.microphone.enabled, false);
  assert.equal(f.audio().srcObject, null); assert.equal(f.audio().muted, true);
  assert.equal(f.audio().pauses, 1); assert.equal(f.peer().closes, 0); assert.equal(f.channel.closes, 0);
  f.media.muteInput(false); f.media.muteOutput(false);
  f.peer().ontrack({ streams: [f.stream], track: f.microphone });
  f.microphone.dispatchEvent(new Event("ended")); f.channel.onerror(); f.channel.onclose(); f.peer().state("failed");
  await f.media.play();
  assert.equal(f.microphone.enabled, false); assert.equal(f.audio().muted, true); assert.equal(f.audio().srcObject, null);
  assert.deepEqual(f.failures, []); assert.equal(f.ends(), 0);
  f.media.close();
  assert.equal(f.peer().closes, 1); assert.equal(f.channel.closes, 1); assert.equal(f.microphone.stops, 1);
});

test("login voice routes exact microphone and speaker selections before generating an offer", async (t) => {
  const f = fixture({ inputId: "usb-mic", outputId: "headphones" }); t.after(f.restore);
  const sink = deferred<void>(); f.sinkResult(sink.promise);
  const preparing = f.prepare(); await setImmediate();
  assert.deepEqual(f.constraints, [{ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, deviceId: { exact: "usb-mic" } } }]);
  assert.deepEqual(f.sinks, ["headphones"]); assert.equal(f.peer(), undefined); assert.equal(f.microphone.enabled, false);
  sink.resolve(); await preparing;
  assert.equal(f.media.offer?.(), "offer-sdp"); assert.equal(f.connections(), 0);
});

test("cancelling login speaker selection stops input and never creates a late peer", async (t) => {
  const f = fixture({ inputId: "", outputId: "headphones" }); t.after(f.restore);
  const sink = deferred<void>(); f.sinkResult(sink.promise);
  const abort = new AbortController(), preparing = f.media.prepare(abort.signal); await setImmediate();
  abort.abort();
  assert.equal(f.microphone.stops, 1); assert.equal(f.audio().pauses, 1); assert.equal(f.audio().srcObject, null);
  sink.resolve(); await assert.rejects(preparing, { name: "AbortError" });
  assert.equal(f.peer(), undefined); assert.equal(f.connections(), 0);
});

for (const name of ["NotAllowedError", "NotFoundError"]) test(`login selected speaker ${name} fails before provisioning and stops input`, async (t) => {
  const f = fixture({ inputId: "", outputId: "headphones" }); t.after(f.restore);
  f.outputError(new DOMException("Device unavailable", name));
  await assert.rejects(f.prepare(), /selected speaker.*System default/);
  assert.equal(f.microphone.stops, 1); assert.equal(f.peer(), undefined); assert.equal(f.connections(), 0);
});

test("unsupported login speaker routing never falls back to an unselected output", async (t) => {
  const f = fixture({ inputId: "", outputId: "headphones" }); t.after(f.restore); f.withoutOutputRouting();
  await assert.rejects(f.prepare(), /cannot use the selected speaker.*System default/);
  assert.equal(f.microphone.stops, 1); assert.equal(f.peer(), undefined); assert.equal(f.connections(), 0);
});

test("login system default still works when browser speaker selection is unsupported", async (t) => {
  const f = fixture(); t.after(f.restore); f.withoutOutputRouting();
  await f.prepare(); await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  assert.equal(f.connections(), 1); assert.deepEqual(f.sinks, []);
});

test("an unavailable selected login microphone asks for another input instead of retrying default", async (t) => {
  const f = fixture({ inputId: "removed-mic", outputId: "" }); t.after(f.restore);
  f.inputError(new DOMException("Unavailable", "NotFoundError"));
  await assert.rejects(f.prepare(), /selected microphone.*System default/);
  assert.equal(f.constraints.length, 1); assert.equal(f.peer(), undefined); assert.equal(f.connections(), 0);
});

test("login microphone swaps retain the peer, sender and mute while stopping old capture only after replacement", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  const peer = f.peer(), replacement = f.newMicrophone(), swapped = deferred<void>();
  f.permission(Promise.resolve(replacement.stream)); f.replacementResult(swapped.promise);
  const changing = f.media.setInputDevice("usb-mic"); await setImmediate();
  assert.equal(f.microphone.stops, 0); assert.equal(replacement.track.enabled, false); assert.equal(f.sender.track, f.microphone);
  f.media.muteInput(true); swapped.resolve(); await changing;
  assert.equal(f.sender.track, replacement.track); assert.equal(replacement.track.enabled, false); assert.equal(f.microphone.stops, 1);
  assert.equal(f.peer(), peer); assert.equal(f.connections(), 1); assert.equal(f.channel.closes, 0);
  f.microphone.dispatchEvent(new Event("ended")); assert.deepEqual(f.failures, []);
  f.media.muteInput(false); assert.equal(replacement.track.enabled, true);
});

test("failed login microphone replacement retains old sender and a retry can reset to default", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  const rejected = f.newMicrophone(); f.permission(Promise.resolve(rejected.stream)); f.replacementError(true);
  await assert.rejects(f.media.setInputDevice("incompatible-mic"), /previous microphone is still selected/);
  assert.equal(f.sender.track, f.microphone); assert.equal(f.microphone.stops, 0); assert.equal(rejected.track.stops, 1);
  f.replacementError(false); const good = f.newMicrophone(); f.permission(Promise.resolve(good.stream));
  await f.media.setInputDevice(""); assert.equal(f.sender.track, good.track); assert.equal(f.microphone.stops, 1);
  assert.deepEqual(f.constraints.at(-1), { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  assert.deepEqual(f.failures, []);
});

test("a replacement unplugged during RTC swap restores the previous sender without ending voice", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  const replacement = f.newMicrophone(), swapped = deferred<void>();
  f.permission(Promise.resolve(replacement.stream)); f.replacementResult(swapped.promise);
  const changing = f.media.setInputDevice("unplugged-mic"); await setImmediate();
  replacement.track.readyState = "ended"; replacement.track.dispatchEvent(new Event("ended"));
  swapped.resolve(); await assert.rejects(changing, /selected microphone disconnected/);
  assert.equal(f.sender.track, f.microphone); assert.equal(f.microphone.stops, 0);
  assert.equal(replacement.track.stops, 1); assert.deepEqual(f.failures, []);
});

test("login input changes serialize, and stopping voice rejects queued changes and releases late capture", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  const late = f.newMicrophone(), permission = deferred<typeof late.stream>(); f.permission(permission.promise);
  const first = f.media.setInputDevice("first"), second = f.media.setInputDevice("second");
  const rejected = Promise.all([assert.rejects(first, { name: "AbortError" }), assert.rejects(second, { name: "AbortError" })]);
  await setImmediate(); assert.equal(f.constraints.length, 2);
  f.media.stop?.(); await rejected; permission.resolve(late.stream); await setImmediate();
  assert.equal(late.track.stops, 1); assert.equal(late.track.enabled, false); assert.equal(f.microphone.stops, 1);
  assert.equal(f.replacements.length, 0); assert.equal(f.constraints.length, 2);
  assert.equal(f.peer().closes, 0, "quiet RTC remains available for server finalization");
});

test("stopping during an RTC track replacement releases both captures without later unmuting", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  const replacement = f.newMicrophone(), swapped = deferred<void>();
  f.permission(Promise.resolve(replacement.stream)); f.replacementResult(swapped.promise);
  const changing = f.media.setInputDevice("pending-mic"), rejected = assert.rejects(changing, { name: "AbortError" });
  await setImmediate(); f.media.stop?.(); await rejected;
  assert.equal(replacement.track.stops, 1); assert.equal(f.microphone.stops, 1);
  swapped.resolve(); await setImmediate();
  assert.equal(replacement.track.enabled, false); assert.equal(f.peer().closes, 0); assert.deepEqual(f.failures, []);
});

test("retiring an unplugged login microphone during a successful swap does not fail the call", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  const replacement = f.newMicrophone(), permission = deferred<typeof replacement.stream>(); f.permission(permission.promise);
  const changing = f.media.setInputDevice("replacement"); await setImmediate();
  f.microphone.readyState = "ended"; f.microphone.dispatchEvent(new Event("ended")); assert.deepEqual(f.failures, []);
  permission.resolve(replacement.stream); await changing; assert.deepEqual(f.failures, []);
  replacement.track.dispatchEvent(new Event("ended")); assert.match(f.failures[0], /microphone was disconnected/);
});

test("login output routes change in place, preserve mute, reset default and retain selection on rejection", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  await f.media.connect("local-session", "local-token", "answer-sdp"); f.peer().state("connected");
  f.media.muteOutput(true); const audio = f.audio(), peer = f.peer();
  await f.media.setOutputDevice("headphones"); assert.equal(audio.sinkId, "headphones"); assert.equal(audio.muted, true);
  await f.media.setOutputDevice(""); assert.deepEqual(f.sinks, ["headphones", ""]); assert.equal(audio.sinkId, "");
  f.outputError(new DOMException("Denied", "NotAllowedError"));
  await assert.rejects(f.media.setOutputDevice("denied"), /selected speaker/);
  assert.equal(audio.sinkId, ""); assert.equal(audio.muted, true); assert.equal(f.peer(), peer); assert.equal(peer.closes, 0);
  f.outputError(undefined); await f.media.setOutputDevice("speakers"); assert.equal(audio.sinkId, "speakers");
});

test("queued login speaker swaps cannot run after voice stops", async (t) => {
  const f = fixture(); t.after(f.restore); await f.prepare();
  const sink = deferred<void>(); f.sinkResult(sink.promise);
  const first = f.media.setOutputDevice("first"), second = f.media.setOutputDevice("second");
  const rejected = Promise.all([assert.rejects(first, { name: "AbortError" }), assert.rejects(second, { name: "AbortError" })]);
  await setImmediate(); assert.deepEqual(f.sinks, ["first"]);
  f.media.stop?.(); await rejected; sink.resolve(); await setImmediate();
  assert.deepEqual(f.sinks, ["first"]); assert.equal(f.audio().srcObject, null); assert.equal(f.audio().muted, true);
});
