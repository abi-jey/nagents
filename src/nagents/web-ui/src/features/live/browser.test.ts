import assert from "node:assert/strict";
import test from "node:test";
import { browserMedia, liveSupport } from "./browser.js";

test("late microphone permission after cancellation stops every track without constructing WebRTC", async () => {
  const savedAudio = Object.getOwnPropertyDescriptor(globalThis, "Audio");
  const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let grant!: (stream: MediaStream) => void;
  let stopped = 0;
  Object.defineProperty(globalThis, "Audio", { configurable: true, value: class {
    autoplay = false; srcObject = null; pause() {} play() { return Promise.resolve(); }
  } });
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia: () => new Promise<MediaStream>((resolve) => { grant = resolve; }),
  } } });
  try {
    const media = browserMedia({ connected: () => assert.fail("No connection expected"), failed: () => {}, playbackBlocked: () => {} });
    const abort = new AbortController();
    const offer = media.offer(abort.signal);
    abort.abort(); media.close();
    grant({ getTracks: () => [{ stop: () => { stopped++; } }] } as unknown as MediaStream);
    await assert.rejects(offer, { name: "AbortError" });
    assert.equal(stopped, 1);
  } finally {
    if (savedAudio) Object.defineProperty(globalThis, "Audio", savedAudio); else Reflect.deleteProperty(globalThis, "Audio");
    if (savedNavigator) Object.defineProperty(globalThis, "navigator", savedNavigator); else Reflect.deleteProperty(globalThis, "navigator");
  }
});

test("insecure contexts explain why a connection cannot start", () => {
  assert.match(liveSupport(), /localhost or HTTPS/);
});

test("WebRTC negotiates tracks without restarting Live, recovers blocked playback, and releases media", async () => {
  const names = ["Audio", "navigator", "RTCPeerConnection", "MediaStream"] as const;
  const saved = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  const microphone = Object.assign(new EventTarget(), { enabled: true, stops: 0, stop() { this.stops++; } });
  const stream = { getTracks: () => [microphone], getAudioTracks: () => [microphone] };
  let playbackAllowed = false, connected = false, channelClosed = false, peerClosed = false;
  const playback: boolean[] = [], sent: string[] = [];
  let player!: TestAudio;
  class TestAudio {
    autoplay = false; muted = false; srcObject: object | null = null; paused = false;
    constructor() { player = this; }
    pause() { this.paused = true; }
    async play() { if (!playbackAllowed) throw new DOMException("Autoplay blocked", "NotAllowedError"); }
  }
  class Peer extends EventTarget {
    iceGatheringState = "complete"; connectionState = "new";
    localDescription: RTCSessionDescriptionInit = { type: "offer", sdp: "offer" };
    onconnectionstatechange = () => {};
    ontrack = (_event: { track: object }) => {};
    addTrack(track: object, source: object) { assert.equal(track, microphone); assert.equal(source, stream); }
    createDataChannel(label: string) { assert.equal(label, "oai-events"); return { send: (data: string) => sent.push(data), close: () => { channelClosed = true; } }; }
    async createOffer() { return { type: "offer", sdp: "offer" }; }
    async setLocalDescription(offer: RTCSessionDescriptionInit) { this.localDescription = offer; }
    async setRemoteDescription(answer: RTCSessionDescriptionInit) {
      assert.deepEqual(answer, { type: "answer", sdp: "answer" });
      this.connectionState = "connected"; this.onconnectionstatechange(); this.ontrack({ track: microphone });
    }
    close() { peerClosed = true; this.connectionState = "closed"; this.onconnectionstatechange(); }
  }
  Object.defineProperty(globalThis, "Audio", { configurable: true, value: TestAudio });
  Object.defineProperty(globalThis, "MediaStream", { configurable: true, value: class {} });
  Object.defineProperty(globalThis, "RTCPeerConnection", { configurable: true, value: Peer });
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: async () => stream } } });
  try {
    const media = browserMedia({ connected: () => { connected = true; }, failed: () => assert.fail("No transport failure"), playbackBlocked: (blocked) => playback.push(blocked) });
    assert.equal(await media.offer(new AbortController().signal), "offer");
    await media.answer("answer");
    assert.equal(connected, true); assert.deepEqual([...sent], []);
    assert.equal(playback.at(-1), true);
    playbackAllowed = true; await media.play(); assert.equal(playback.at(-1), false);
    media.muteInput(true); media.muteOutput(true);
    assert.equal(microphone.enabled, false); assert.equal(player.muted, true);
    media.close(); media.close();
    assert.equal(microphone.stops, 1); assert.ok(channelClosed && peerClosed);
    assert.equal(player.srcObject, null); assert.equal(player.paused, true);
  } finally {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
