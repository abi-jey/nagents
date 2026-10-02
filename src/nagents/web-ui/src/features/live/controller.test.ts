import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { appendCaptions, LiveController, liveFailure } from "./controller.js";
import type { LiveCreated, LiveSnapshot, LiveTransport, MediaHandlers } from "./types.js";

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const created: LiveCreated = { session_id: "live-test", model: "gpt-live-1", voice: "marin" };
const snapshot: LiveSnapshot = { session_id: "live-test", status: "connected", model: "gpt-live-1", voice: "marin", events: [], cursor: 0 };
const revision = "a".repeat(64);

function fixture(pollMs = 60_000, offer?: string) {
  const prepare = deferred<void>(), create = deferred<LiveCreated>(), read = deferred<LiveSnapshot>();
  const calls: string[] = [];
  const transports: LiveTransport[] = [], offers: (string | undefined)[] = [], answers: (string | undefined)[] = [];
  let handlers!: MediaHandlers;
  let rejectClose = false;
  let rejectConnect = false;
  let closeSnapshot: LiveSnapshot | Promise<LiveSnapshot> = { ...snapshot, status: "closed" };
  let mediaSignal: AbortSignal | undefined;
  let nextRead = read.promise;
  let nextPlay = Promise.resolve();
  const controller = new LiveController({
    media: (callbacks, transport) => {
      transports.push(transport);
      handlers = callbacks;
      return {
        prepare: async (signal) => { mediaSignal = signal; calls.push("prepare"); return prepare.promise; },
        ...(offer ? { offer: () => offer, stop: () => { calls.push("media-stop"); } } : {}),
        connect: async (id, token, answer) => { answers.push(answer); calls.push(`connect:${id}:${token}`); if (rejectConnect) throw new Error("Audio relay rejected connection"); },
        muteInput: (muted) => { calls.push(`mic:${muted}`); },
        muteOutput: (muted) => { calls.push(`speaker:${muted}`); },
        play: async () => { calls.push("play"); handlers.playbackBlocked(false); return nextPlay; },
        close: () => { calls.push("media-close"); },
      };
    },
    create: async (_voice, _signal, current, session, sdp) => { offers.push(sdp); assert.equal(current, revision); calls.push(`create:${session}`); return create.promise; },
    token: "test-token",
    read: async (_id, after) => { calls.push(`read:${after}`); return nextRead; },
    close: async (id) => { calls.push(`close:${id}`); if (rejectClose) throw new Error("Offline"); return closeSnapshot; },
    pollMs,
  });
  return { controller, calls, transports, offers, answers, prepare, create, read, handlers: () => handlers,
    mediaSignal: () => mediaSignal,
    closeSnapshot: (reply: LiveSnapshot | Promise<LiveSnapshot>) => { closeSnapshot = reply; }, nextRead: (reply: Promise<LiveSnapshot>) => { nextRead = reply; },
    nextPlay: (reply: Promise<void>) => { nextPlay = reply; },
    failClose: () => { rejectClose = true; }, failConnect: () => { rejectConnect = true; } };
}

test("only an explicit start acquires the mic; double starts cannot create two sessions", async () => {
  const f = fixture();
  assert.deepEqual([...f.calls], []);
  const start = f.controller.start("marin", revision, "ngn-chat-root");
  await f.controller.start("marin", revision, "ngn-chat-root");
  assert.deepEqual([...f.calls], ["prepare"]);
  assert.equal(f.controller.getSnapshot().phase, "permission");
  f.prepare.resolve(); await setImmediate();
  assert.ok(f.calls.includes("create:ngn-chat-root"));
  assert.equal(f.controller.getSnapshot().phase, "connecting");
  f.create.resolve(created); await start;
  assert.equal(f.controller.getSnapshot().phase, "connecting", "upstream provisioning is not proof of browser audio");
  assert.ok(f.calls.includes("connect:live-test:test-token"));
  f.handlers().connected();
  assert.equal(f.controller.getSnapshot().phase, "connected");
  f.controller.muteInput(); f.controller.muteOutput();
  assert.ok(f.calls.includes("mic:true")); assert.ok(f.calls.includes("speaker:true"));
  f.handlers().playbackBlocked(true);
  assert.equal(f.controller.getSnapshot().playbackBlocked, true);
  await f.controller.play();
  assert.equal(f.controller.getSnapshot().playbackBlocked, false);
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, "ended");
  assert.ok(f.calls.indexOf("media-close") < f.calls.indexOf("close:live-test"));
  f.read.resolve(snapshot); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ended", "late polling cannot resurrect an ended session");
  f.controller.dispose();
});

test("cancelling a pending permission request prevents provisioning and ignores stale callbacks", async () => {
  const f = fixture(); const start = f.controller.start("marin", revision);
  await f.controller.end();
  f.prepare.resolve(); await start;
  f.handlers().connected(); f.handlers().failed("late failure");
  assert.deepEqual([...f.calls], ["prepare", "media-close"]);
  assert.equal(f.controller.getSnapshot().phase, "ended");
  f.controller.dispose();
});

test("cancelling while the server creates a session closes the late provisioned session", async () => {
  const f = fixture(); const start = f.controller.start("marin", revision);
  f.prepare.resolve(); await setImmediate();
  await f.controller.end();
  f.create.resolve(created); await start;
  assert.ok(f.calls.includes("close:live-test"));
  assert.ok(!f.calls.some((call) => call.startsWith("connect:")));
  assert.equal(f.controller.getSnapshot().phase, "ended");
  f.controller.dispose();
});

test("rejected relay connection cleans up browser and upstream resources", async () => {
  const f = fixture(); f.failConnect();
  const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start; await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.ok(f.calls.includes("media-close")); assert.ok(f.calls.includes("close:live-test"));
  f.controller.dispose();
});

test("lost server heartbeat releases audio immediately and reports unconfirmed closure", async () => {
  const f = fixture(); f.failClose();
  const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start;
  f.handlers().connected(); f.read.reject(new Error("Server unavailable")); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.match(f.controller.getSnapshot().error, /Lost contact/);
  assert.match(f.controller.getSnapshot().notice, /could not be confirmed/);
  assert.ok(f.calls.includes("media-close"));
  f.controller.dispose();
});

test("final server snapshot preserves captions while ending microphone capture", async () => {
  const f = fixture(); const start = f.controller.start("marin", revision);
  f.prepare.resolve(); f.create.resolve(created); await start;
  f.read.resolve({ ...snapshot, status: "closed", cursor: 2, events: [
    { seq: 1, type: "transcript", speaker: "user", text: "Hello", start_ms: 100, end_ms: 500 },
    { seq: 2, type: "transcript", speaker: "assistant", text: "Hi there", start_ms: 600, end_ms: 900 },
  ] });
  await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ended");
  assert.deepEqual(f.controller.getSnapshot().captions.map((caption) => caption.text), ["Hello", "Hi there"]);
  assert.ok(f.calls.includes("media-close"));
  f.controller.dispose();
});

test("unmount during startup closes late-created resources without publishing stale state", async () => {
  const f = fixture(); const start = f.controller.start("marin", revision);
  f.prepare.resolve(); await setImmediate();
  f.controller.dispose(); let changes = 0;
  f.controller.subscribe(() => { changes++; });
  f.create.resolve(created); await start;
  assert.ok(f.calls.includes("close:live-test")); assert.equal(changes, 0);
});

test("permission errors are actionable and never create a remote session", async () => {
  const f = fixture(); const start = f.controller.start("marin", revision);
  f.prepare.reject(new DOMException("Denied", "NotAllowedError")); await start;
  assert.match(f.controller.getSnapshot().error, /site settings/);
  assert.ok(!f.calls.some((call) => call.startsWith("create:")));
  assert.match(liveFailure(new DOMException("No input", "NotFoundError")), /No microphone/);
  f.controller.dispose();
});

test("caption grouping preserves fragments, speaker overlap, late arrivals, and bounded history", () => {
  const captions = appendCaptions([], [
    { seq: 1, type: "transcript", speaker: "user", text: "An", start_ms: 100, end_ms: 200 },
    { seq: 2, type: "transcript", speaker: "user", text: " idea.", start_ms: 201, end_ms: 400 },
    { seq: 3, type: "transcript", speaker: "assistant", text: "Yes?", start_ms: 300, end_ms: 500 },
    { seq: 4, type: "transcript", speaker: "user", text: "Late", start_ms: 150, end_ms: 190 },
  ]);
  assert.deepEqual(captions.map((caption) => caption.text), ["An idea.", "Yes?", "Late"]);
  assert.equal(captions[0].end, 400);
  const bounded = appendCaptions([], Array.from({ length: 300 }, (_, i) => ({ seq: i, type: "transcript", speaker: i % 2 ? "user" : "assistant", text: `${i}` })));
  assert.equal(bounded.length, 200); assert.equal(bounded[0].text, "100");
});

for (const status of ["closed", "error"] as const) test(`explicit end preserves final captions and ${status} finalization evidence from its response`, async () => {
  const f = fixture();
  const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start;
  f.handlers().connected();
  const first = { seq: 1, type: "transcript" as const, speaker: "assistant" as const, text: "A final", start_ms: 0, end_ms: 100 };
  f.read.resolve({ ...snapshot, cursor: 1, events: [first] }); await setImmediate();
  f.closeSnapshot({ ...snapshot, status, message: "Provider finalization is unconfirmed.", cursor: 2, events: [first,
    { ...first, seq: 2, text: " thought.", start_ms: 100, end_ms: 200 },
  ] });
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, status === "error" ? "error" : "ended");
  assert.equal(f.controller.getSnapshot().captions[0].text, "A final thought.");
  assert.match(status === "error" ? f.controller.getSnapshot().error : f.controller.getSnapshot().notice, /unconfirmed/);
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1);
  f.controller.dispose();
});

test("closing stops audio but keeps polling for the final status and captions", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture(100);
  const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start;
  f.handlers().connected();
  f.read.resolve({ ...snapshot, status: "closing", message: "Closing Live session." }); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ending");
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1);
  f.nextRead(Promise.resolve({ ...snapshot, status: "error", message: "Finalization is unconfirmed.", cursor: 1,
    events: [{ seq: 1, type: "transcript", speaker: "assistant", text: "Last words" }],
  }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.match(f.controller.getSnapshot().error, /unconfirmed/);
  assert.equal(f.controller.getSnapshot().captions[0].text, "Last words");
  assert.equal(f.calls.filter((call) => call.startsWith("read:")).length, 2);
  f.controller.dispose();
});

for (const status of ["closed", "error"] as const) test(`clean relay closure reconciles ${status} status and final captions`, async () => {
  const f = fixture();
  const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start;
  f.handlers().connected(); f.handlers().playbackBlocked(true);
  f.closeSnapshot({ ...snapshot, status, message: status === "closed" ? "Finalization confirmed." : "Provider failed.", cursor: 1,
    events: [{ seq: 1, type: "transcript", speaker: "assistant", text: "Goodbye" }],
  });
  f.handlers().ended(); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, status === "closed" ? "ended" : "error");
  assert.equal(f.controller.getSnapshot().captions[0].text, "Goodbye");
  assert.equal(f.controller.getSnapshot().playbackBlocked, false);
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1);
  f.controller.dispose();
});

test("late playback rejection cannot display recovery controls after ending", async () => {
  const f = fixture(), play = deferred<void>(); f.nextPlay(play.promise);
  const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start;
  f.handlers().connected();
  const playing = f.controller.play();
  await f.controller.end();
  play.reject(new Error("Context closed")); await playing;
  assert.equal(f.controller.getSnapshot().phase, "ended");
  assert.equal(f.controller.getSnapshot().playbackBlocked, false);
  f.controller.dispose();
});

test("login startup provisions the SDP and applies the answer without treating HTTP success as audio connection", async () => {
  const f = fixture(60_000, "offer-sdp");
  const start = f.controller.start("marin", revision, "ngn-chat-root", "webrtc");
  f.prepare.resolve(); f.create.resolve({ ...created, sdp: "answer-sdp" }); await start;
  assert.deepEqual(f.transports, ["webrtc"]); assert.deepEqual(f.offers, ["offer-sdp"]); assert.deepEqual(f.answers, ["answer-sdp"]);
  assert.equal(f.controller.getSnapshot().phase, "connecting");
  f.handlers().connected(); assert.equal(f.controller.getSnapshot().phase, "connected");
  await f.controller.end(); f.controller.dispose();
});

test("a cancelled login creation closes the returned session and never applies a late SDP", async () => {
  const f = fixture(60_000, "offer-sdp");
  const start = f.controller.start("marin", revision, "ngn-chat-root", "webrtc");
  f.prepare.resolve(); await setImmediate(); await f.controller.end();
  f.create.resolve({ ...created, sdp: "late-answer" }); await start;
  assert.deepEqual(f.answers, []); assert.ok(f.calls.includes("close:live-test"));
  assert.equal(f.controller.getSnapshot().phase, "ended"); f.controller.dispose();
});

for (const status of ["closed", "error"] as const) test(`ending login voice stops devices before HTTP close and retains the peer until ${status} confirmation`, async () => {
  const f = fixture(60_000, "offer-sdp"), closing = deferred<LiveSnapshot>();
  const start = f.controller.start("marin", revision, "ngn-chat-root", "webrtc");
  f.prepare.resolve(); f.create.resolve({ ...created, sdp: "answer-sdp" }); await start; f.handlers().connected();
  f.closeSnapshot(closing.promise);
  const ending = f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, "ending");
  assert.ok(f.calls.indexOf("media-stop") < f.calls.indexOf("close:live-test"));
  assert.ok(!f.calls.includes("media-close")); assert.equal(f.mediaSignal()?.aborted, false);
  f.read.resolve(snapshot); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ending", "the old poll cannot resurrect the call");
  closing.resolve({ ...snapshot, status, message: "Provider finalization result.", cursor: 1,
    events: [{ seq: 1, type: "transcript", speaker: "assistant", text: "Final caption" }],
  });
  await ending;
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1); assert.equal(f.mediaSignal()?.aborted, true);
  assert.equal(f.controller.getSnapshot().phase, status === "closed" ? "ended" : "error");
  assert.equal(f.controller.getSnapshot().captions[0].text, "Final caption");
  if (status === "error") assert.match(f.controller.getSnapshot().error, /Provider finalization/);
  f.controller.dispose();
});

test("login closing snapshots retain quiet RTC until final polling completes", async () => {
  const f = fixture(60_000, "offer-sdp"), final = deferred<LiveSnapshot>();
  const start = f.controller.start("marin", revision, "ngn-chat-root", "webrtc");
  f.prepare.resolve(); f.create.resolve({ ...created, sdp: "answer-sdp" }); await start; f.handlers().connected();
  f.closeSnapshot({ ...snapshot, status: "closing" }); f.nextRead(final.promise);
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, "ending");
  assert.ok(!f.calls.includes("media-close")); assert.equal(f.mediaSignal()?.aborted, false);
  final.resolve({ ...snapshot, status: "closed" }); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ended");
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1); f.controller.dispose();
});

for (const alreadyEnding of [false, true]) test(`unmount ${alreadyEnding ? "during" : "starts"} login finalization without closing RTC early`, async () => {
  const f = fixture(60_000, "offer-sdp"), closing = deferred<LiveSnapshot>();
  const start = f.controller.start("marin", revision, "ngn-chat-root", "webrtc");
  f.prepare.resolve(); f.create.resolve({ ...created, sdp: "answer-sdp" }); await start; f.handlers().connected();
  f.closeSnapshot(closing.promise);
  if (alreadyEnding) void f.controller.end();
  let changes = 0; f.controller.subscribe(() => { changes++; });
  f.controller.dispose(); f.controller.dispose();
  assert.ok(f.calls.includes("media-stop")); assert.ok(!f.calls.includes("media-close"));
  assert.equal(f.calls.filter((call) => call === "close:live-test").length, 1);
  assert.equal(f.mediaSignal()?.aborted, false);
  closing.resolve({ ...snapshot, status: "closed" }); await setImmediate();
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1);
  assert.equal(f.mediaSignal()?.aborted, true); assert.equal(changes, 0);
});

test("unconfirmed login closure still releases the quiet peer", async () => {
  const f = fixture(60_000, "offer-sdp");
  const start = f.controller.start("marin", revision, "ngn-chat-root", "webrtc");
  f.prepare.resolve(); f.create.resolve({ ...created, sdp: "answer-sdp" }); await start; f.handlers().connected();
  f.failClose(); await f.controller.end();
  assert.match(f.controller.getSnapshot().notice, /could not be confirmed/);
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1);
  assert.equal(f.mediaSignal()?.aborted, true); f.controller.dispose();
});
