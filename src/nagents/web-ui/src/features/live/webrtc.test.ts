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

function fixture(devices: AudioDeviceSelection = { inputId: "", outputId: "" }) {
  const names = ["navigator", "Audio", "RTCPeerConnection", "MediaStream", "isSecureContext"] as const;
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  const microphone = Object.assign(new EventTarget(), { enabled: true, stops: 0, stop() { this.stops++; } });
  class Stream {
    constructor(readonly tracks = [microphone]) {}
    getTracks() { return this.tracks; }
    getAudioTracks() { return this.tracks; }
  }
  const stream = new Stream(), steps: string[] = [], failures: string[] = [], playback: boolean[] = [];
  const constraints: MediaStreamConstraints[] = [], sinks: string[] = [];
  let inputError: Error | undefined, outputError: Error | undefined, sinkResult = Promise.resolve();
  let permission = Promise.resolve(stream), offer = Promise.resolve({ type: "offer" as const, sdp: "offer-sdp" });
  let answer = Promise.resolve(), audio!: AudioFixture, peer!: Peer;
  let connections = 0, ends = 0, blockPlayback = false;
  const channel = { onclose: () => {}, onerror: () => {}, closes: 0,
    close() { this.closes++; this.onclose(); }, send: () => assert.fail("Only the server sideband may send provider commands") };
  class AudioFixture {
    srcObject: Stream | null = null;
    autoplay = false;
    muted = false;
    pauses = 0;
    onplaying = () => {};
    onpause = () => {};
    onerror = () => {};
    constructor() { audio = this; }
    async play() { if (blockPlayback) throw new DOMException("Blocked", "NotAllowedError"); this.onplaying(); }
    async setSinkId(id: string) { sinks.push(id); if (outputError) throw outputError; return sinkResult; }
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
    addTrack(track: typeof microphone, source: Stream) { assert.equal(source, stream); assert.equal(track.enabled, false); steps.push("track"); }
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
  const media = browserMedia({ connected: () => { connections++; }, ended: () => { ends++; }, failed: (message) => failures.push(message), playbackBlocked: (blocked) => playback.push(blocked) }, "webrtc", devices);
  return { media, stream, microphone, steps, channel, failures, playback, constraints, sinks,
    inputError: (cause: Error) => { inputError = cause; }, outputError: (cause: Error) => { outputError = cause; },
    sinkResult: (result: Promise<void>) => { sinkResult = result; },
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
