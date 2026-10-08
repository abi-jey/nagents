import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { appendCaptions, currentLiveDelegation, LiveController, liveFailure } from "./controller.js";
import type { LiveCreated, LiveDelegationRecord, LiveDelegationStatus, LiveSnapshot, LiveTransport, MediaHandlers, VoiceContext } from "./types.js";
import type { AudioFrame } from "../../components/voiceSphere/types.js";

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const created: LiveCreated = { session_id: "live-test", model: "gpt-live-1", voice: "marin" };
const snapshot: LiveSnapshot = { session_id: "live-test", status: "connected", model: "gpt-live-1", voice: "marin", events: [], cursor: 0 };
const revision = "a".repeat(64);
const handoff = (id: string, seq: number, status: LiveDelegationStatus, extra: Partial<LiveDelegationRecord> = {}): LiveDelegationRecord => ({
  delegation_id: id, seq, status, voice_session_id: "live-test", chat_session_id: "ngn-chat-root",
  agent: "Workspace assistant", provider: "anthropic", model: "configured-model", run_id: status === "queued" ? "" : `run-${id}`,
  text: `Assistant ${status}`, ...extra,
});

function fixture(pollMs = 60_000, sampleAudio?: () => AudioFrame) {
  const prepare = deferred<void>(), create = deferred<LiveCreated>(), read = deferred<LiveSnapshot>();
  const calls: string[] = [];
  const transports: LiveTransport[] = [];
  let handlers!: MediaHandlers;
  let rejectClose = false;
  let rejectConnect = false;
  let closeSnapshot: LiveSnapshot | Promise<LiveSnapshot> = { ...snapshot, status: "closed" };
  let mediaSignal: AbortSignal | undefined;
  let nextRead = read.promise;
  let nextPlay = Promise.resolve();
  let nextDevice = Promise.resolve();
  let nextAudio = sampleAudio;
  const controller = new LiveController({
    media: (callbacks, transport) => {
      transports.push(transport);
      handlers = callbacks;
      return {
        ...(sampleAudio ? { sampleAudio: () => nextAudio!() } : {}),
        prepare: async (signal) => { mediaSignal = signal; calls.push("prepare"); return prepare.promise; },
        connect: async (id, token) => { calls.push(`connect:${id}:${token}`); if (rejectConnect) throw new Error("Audio relay rejected connection"); },
        muteInput: (muted) => { calls.push(`mic:${muted}`); },
        muteOutput: (muted) => { calls.push(`speaker:${muted}`); },
        setInputDevice: async (id) => { calls.push(`input-device:${id}`); await nextDevice; },
        setOutputDevice: async (id) => { calls.push(`output-device:${id}`); await nextDevice; },
        play: async () => { calls.push("play"); handlers.playbackBlocked(false); return nextPlay; },
        close: () => { calls.push("media-close"); },
      };
    },
    create: async (_voice, _signal, current, session) => { assert.equal(current, revision); calls.push(`create:${session}`); return create.promise; },
    token: "test-token",
    read: async (_id, after) => { calls.push(`read:${after}`); return nextRead; },
    close: async (id) => { calls.push(`close:${id}`); if (rejectClose) throw new Error("Offline"); return closeSnapshot; },
    pollMs,
  });
  return { controller, calls, transports, prepare, create, read, handlers: () => handlers,
    mediaSignal: () => mediaSignal,
    closeSnapshot: (reply: LiveSnapshot | Promise<LiveSnapshot>) => { closeSnapshot = reply; }, nextRead: (reply: Promise<LiveSnapshot>) => { nextRead = reply; },
    nextPlay: (reply: Promise<void>) => { nextPlay = reply; },
    nextDevice: (reply: Promise<void>) => { nextDevice = reply; },
    nextAudio: (sample: () => AudioFrame) => { nextAudio = sample; },
    failClose: () => { rejectClose = true; }, failConnect: () => { rejectConnect = true; } };
}

test("the stable sphere audio facade samples only the connected owner and never publishes frame state", async () => {
  const frame: AudioFrame = { input: { active: true, rms: .25, low: .2, mid: .1, high: .05 }, output: { active: true, rms: .5, low: .4, mid: .3, high: .2 } };
  let samples = 0;
  const f = fixture(60_000, () => { samples++; return frame; }), audio = f.controller.audio;
  let changes = 0; const unsubscribe = f.controller.subscribe(() => { changes++; });
  try {
    assert.equal(audio.sample().input.active, false); assert.equal(samples, 0);
    const start = f.controller.start("marin", revision, "ngn-chat-root");
    assert.equal(audio.sample().input.active, false); assert.equal(samples, 0);
    f.prepare.resolve(); f.create.resolve(created); await start;
    assert.equal(audio.sample().output.active, false); assert.equal(samples, 0);
    f.handlers().connected(); const beforeFrames = changes;
    const copy = audio.sample(); assert.deepEqual(copy, frame); copy.input.rms = 0;
    assert.equal(frame.input.rms, .25, "renderers must not mutate transport-owned frames");
    for (let i = 0; i < 30; i++) audio.sample();
    assert.equal(changes, beforeFrames, "per-frame audio sampling must not rerender the controller");
    f.controller.muteInput(); assert.equal(audio.sample().input.active, false); assert.equal(audio.sample().output.rms, .5);
    f.controller.muteOutput(); assert.equal(audio.sample().output.active, false);
    f.controller.muteInput(); f.controller.muteOutput(); f.handlers().playbackBlocked(true);
    assert.equal(audio.sample().input.rms, .25); assert.equal(audio.sample().output.rms, 0);
    f.handlers().playbackBlocked(false);
    f.nextAudio(() => ({ input: { active: true, rms: NaN, low: -1, mid: 2, high: Infinity }, output: frame.output }));
    assert.deepEqual(audio.sample().input, { active: true, rms: 0, low: 0, mid: 1, high: 0 });
    f.nextAudio(() => { throw new Error("Optional visualizer unavailable"); });
    assert.equal(audio.sample().input.active, false); assert.equal(f.controller.getSnapshot().phase, "connected");
    f.nextAudio(() => { void f.controller.end(); return frame; });
    assert.equal(audio.sample().output.active, false, "ownership is rechecked after a sampler changes the session epoch");
    await setImmediate(); assert.equal(f.controller.getSnapshot().phase, "ended");
    f.nextAudio(() => frame); await f.controller.start("marin", revision, "ngn-chat-root");
    assert.equal(f.controller.audio, audio); assert.equal(audio.sample().input.active, false);
    f.handlers().connected(); assert.equal(audio.sample().input.rms, .25);
    f.controller.dispose(); assert.equal(audio.sample().input.active, false); assert.equal(audio.sample().output.active, false);
  } finally { unsubscribe(); f.controller.dispose(); }
});

test("media adapters without optional PCM sampling remain compatible", async () => {
  const f = fixture();
  try {
    const start = f.controller.start("marin", revision); f.prepare.resolve(); f.create.resolve(created); await start; f.handlers().connected();
    assert.equal(f.controller.audio.sample().input.active, false); assert.equal(f.controller.audio.sample().output.active, false);
    assert.equal(f.controller.getSnapshot().phase, "connected");
  } finally { f.controller.dispose(); }
});

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

test("startup context metadata is scoped to this chat, ignores foreign reports, and clears before a new start", async () => {
  const f = fixture();
  const context: VoiceContext = { mode: "recent", method: "recent", chat_session_id: "ngn-chat-root", fingerprint: "a".repeat(64), message_count: 3, characters: 100, bytes: 100, summary_included: false, omitted_messages: 0, omitted_content: false, notice: "Recent chat included." };
  try {
    const start = f.controller.start("marin", revision, "ngn-chat-root");
    f.prepare.resolve(); f.create.resolve({ ...created, context }); await start;
    assert.deepEqual(f.controller.getSnapshot().context, context);
    f.handlers().connected();
    f.read.resolve({ ...snapshot, context: { ...context, chat_session_id: "ngn-other-root" } });
    await setImmediate();
    assert.deepEqual(f.controller.getSnapshot().context, context);
    await f.controller.end();
    const restart = f.controller.start("marin", revision, "ngn-other-root");
    assert.equal(f.controller.getSnapshot().context, undefined);
    await restart;
    assert.equal(f.controller.getSnapshot().context, undefined, "an old response cannot seed a different chat's UI");
  } finally { f.controller.dispose(); }
});

test("real media levels follow call ownership, mute and playback availability and clear immediately on end", async () => {
  const f = fixture();
  const start = f.controller.start("marin", revision, "ngn-chat-root");
  f.prepare.resolve(); f.create.resolve(created); await start;
  const old = f.handlers();
  assert.equal(f.controller.getSnapshot().inputLevel, 0); assert.equal(f.controller.getSnapshot().outputLevel, 0);
  old.levels?.(0.4, 0.7);
  assert.equal(f.controller.getSnapshot().inputLevel, 0, "connection setup does not imply live audio");
  old.connected(); old.levels?.(0.4, 0.7);
  assert.equal(f.controller.getSnapshot().inputLevel, 0.4); assert.equal(f.controller.getSnapshot().outputLevel, 0.7);
  f.controller.muteInput(); assert.equal(f.controller.getSnapshot().inputLevel, 0);
  old.levels?.(0.9, 0.3); assert.equal(f.controller.getSnapshot().inputLevel, 0);
  f.controller.muteOutput(); assert.equal(f.controller.getSnapshot().outputLevel, 0);
  old.levels?.(0.9, 0.8); assert.equal(f.controller.getSnapshot().outputLevel, 0);
  f.controller.muteInput(); f.controller.muteOutput(); old.levels?.(2, NaN);
  assert.equal(f.controller.getSnapshot().inputLevel, 1); assert.equal(f.controller.getSnapshot().outputLevel, 0);
  old.levels?.(0.3, 0.8); old.playbackBlocked(true);
  assert.equal(f.controller.getSnapshot().outputLevel, 0);
  old.levels?.(0.3, 0.8); assert.equal(f.controller.getSnapshot().outputLevel, 0);
  old.playbackBlocked(false); old.levels?.(0.3, 0.8);
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().inputLevel, 0); assert.equal(f.controller.getSnapshot().outputLevel, 0);
  old.levels?.(1, 1); assert.equal(f.controller.getSnapshot().inputLevel, 0);
  await f.controller.start("marin", revision, "ngn-chat-root"); f.handlers().connected(); f.handlers().levels?.(0.2, 0.5);
  old.levels?.(1, 1);
  assert.equal(f.controller.getSnapshot().inputLevel, 0.2); assert.equal(f.controller.getSnapshot().outputLevel, 0.5);
  f.controller.dispose();
  assert.equal(f.controller.getSnapshot().inputLevel, 0); assert.equal(f.controller.getSnapshot().outputLevel, 0);
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

test("login voice uses only the server relay and waits for actual browser audio connection", async () => {
  const f = fixture();
  const start = f.controller.start("cove", revision, "ngn-chat-root", "websocket");
  f.prepare.resolve(); f.create.resolve({ ...created, model: "gpt-live-1-codex" }); await start;
  assert.deepEqual(f.transports, ["websocket"]);
  assert.equal(f.controller.getSnapshot().phase, "connecting");
  f.handlers().connected(); assert.equal(f.controller.getSnapshot().phase, "connected");
  await f.controller.end(); f.controller.dispose();
});

test("an obsolete direct-provider transport fails before acquiring devices or creating a call", async () => {
  const f = fixture();
  await f.controller.start("cove", revision, "ngn-chat-root", "webrtc" as LiveTransport);
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.match(f.controller.getSnapshot().error, /server relay/);
  assert.deepEqual(f.calls, []); f.controller.dispose();
});

for (const status of ["closed", "error"] as const) test(`ending login voice stops browser media before server ${status} confirmation`, async () => {
  const f = fixture(), closing = deferred<LiveSnapshot>();
  const start = f.controller.start("cove", revision, "ngn-chat-root");
  f.prepare.resolve(); f.create.resolve(created); await start; f.handlers().connected();
  f.closeSnapshot(closing.promise);
  const ending = f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, "ending");
  assert.ok(f.calls.indexOf("media-close") < f.calls.indexOf("close:live-test"));
  assert.equal(f.mediaSignal()?.aborted, true);
  f.read.resolve(snapshot); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ending", "the old poll cannot resurrect the call");
  closing.resolve({ ...snapshot, status, message: "Provider finalization result.", cursor: 1,
    events: [{ seq: 1, type: "transcript", speaker: "assistant", text: "Final caption" }],
  });
  await ending;
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1);
  assert.equal(f.controller.getSnapshot().phase, status === "closed" ? "ended" : "error");
  assert.equal(f.controller.getSnapshot().captions[0].text, "Final caption");
  if (status === "error") assert.match(f.controller.getSnapshot().error, /Provider finalization/);
  f.controller.dispose();
});

test("server closing snapshots finish polling after browser media stops", async () => {
  const f = fixture(), final = deferred<LiveSnapshot>();
  const start = f.controller.start("cove", revision, "ngn-chat-root");
  f.prepare.resolve(); f.create.resolve(created); await start; f.handlers().connected();
  f.closeSnapshot({ ...snapshot, status: "closing" }); f.nextRead(final.promise);
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, "ending");
  assert.ok(f.calls.includes("media-close")); assert.equal(f.mediaSignal()?.aborted, true);
  final.resolve({ ...snapshot, status: "closed" }); await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "ended");
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1); f.controller.dispose();
});

for (const alreadyEnding of [false, true]) test(`unmount ${alreadyEnding ? "during" : "starts"} server finalization without retaining browser media`, async () => {
  const f = fixture(), closing = deferred<LiveSnapshot>();
  const start = f.controller.start("cove", revision, "ngn-chat-root");
  f.prepare.resolve(); f.create.resolve(created); await start; f.handlers().connected();
  f.closeSnapshot(closing.promise);
  if (alreadyEnding) void f.controller.end();
  let changes = 0; f.controller.subscribe(() => { changes++; });
  f.controller.dispose(); f.controller.dispose();
  assert.ok(f.calls.includes("media-close"));
  assert.equal(f.calls.filter((call) => call === "close:live-test").length, 1);
  assert.equal(f.mediaSignal()?.aborted, true);
  closing.resolve({ ...snapshot, status: "closed" }); await setImmediate();
  assert.equal(f.calls.filter((call) => call === "media-close").length, 1); assert.equal(changes, 0);
});

test("voice handoffs follow authoritative queued, working and completed lifecycle events", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture(100); t.after(() => f.controller.dispose());
  const start = f.controller.start("marin", revision, "ngn-chat-root"); f.prepare.resolve(); f.create.resolve(created); await start;
  f.read.resolve({ ...snapshot, cursor: 1, events: [{ type: "delegation", ...handoff("request-a", 1, "queued") }] }); await setImmediate();
  assert.deepEqual(f.controller.getSnapshot().delegations, [{
    id: "request-a", sessionId: "live-test", chatSessionId: "ngn-chat-root", seq: 1, status: "queued", agent: "Workspace assistant",
    provider: "anthropic", model: "configured-model", runId: "", text: "Assistant queued",
  }]);
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 2, events: [{ type: "delegation", ...handoff("request-a", 2, "working") }] }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.status, "working");
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.runId, "run-request-a");
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 3, events: [{ type: "delegation", ...handoff("request-a", 3, "completed") }] }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.status, "completed");
  const stable = f.controller.getSnapshot().delegations;
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 3, events: [], delegations: [handoff("request-a", 3, "completed")] }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(f.controller.getSnapshot().delegations, stable, "identical snapshots do not create repeated status updates");
});

test("snapshot handoffs recover evicted events and a late terminal result cannot hide other active work", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture(100); t.after(() => f.controller.dispose());
  const start = f.controller.start("marin", revision, "ngn-chat-root"); f.prepare.resolve(); f.create.resolve(created); await start;
  f.read.resolve({ ...snapshot, cursor: 500, events: [], delegations: [handoff("request-a", 20, "working")] }); await setImmediate();
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.id, "request-a");
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 503, events: [], delegations: [
    handoff("request-a", 503, "completed"), handoff("request-b", 502, "queued"),
  ] }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.id, "request-b");
  assert.equal(currentLiveDelegation([...f.controller.getSnapshot().delegations].reverse())?.id, "request-b");
  assert.equal(f.controller.getSnapshot().delegations.find((value) => value.id === "request-a")?.status, "completed");
});

test("out-of-order handoff states cannot change admission identity or reopen terminal work", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture(100); t.after(() => f.controller.dispose());
  const start = f.controller.start("marin", revision, "ngn-chat-root"); f.prepare.resolve(); f.create.resolve(created); await start;
  f.read.resolve({ ...snapshot, cursor: 10, events: [], delegations: [handoff("request-a", 10, "working")] }); await setImmediate();
  const working = f.controller.getSnapshot().delegations;
  for (const record of [
    handoff("request-a", 9, "queued"), handoff("request-a", 11, "queued"),
    handoff("request-a", 12, "completed", { run_id: "different-run" }),
    handoff("request-a", 13, "completed", { chat_session_id: "different-root" }),
  ]) {
    f.nextRead(Promise.resolve({ ...snapshot, cursor: 15, events: [], delegations: [record] }));
    t.mock.timers.tick(100); await setImmediate();
    assert.equal(f.controller.getSnapshot().delegations, working);
  }
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 20, events: [], delegations: [handoff("request-a", 20, "failed")] }));
  t.mock.timers.tick(100); await setImmediate();
  const terminal = f.controller.getSnapshot().delegations;
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 21, events: [], delegations: [handoff("request-a", 21, "working")] }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(f.controller.getSnapshot().delegations, terminal);
  f.nextRead(Promise.resolve({ ...snapshot, cursor: 19, events: [], delegations: [handoff("old-unseen", 18, "queued")] }));
  t.mock.timers.tick(100); await setImmediate();
  assert.equal(f.controller.getSnapshot().delegations, terminal, "an older full snapshot cannot introduce stale work");
});

test("malformed or wrong-call handoffs are ignored and spoken delegation claims do not imply work", async (t) => {
  const f = fixture(); t.after(() => f.controller.dispose());
  const start = f.controller.start("marin", revision, "ngn-chat-root"); f.prepare.resolve(); f.create.resolve(created); await start;
  const invalid: Record<string, unknown>[] = [
    { seq: "1" }, { seq: 0 }, { seq: 1.5 }, { seq: 501 }, { seq: NaN },
    { status: "waiting" }, { status: "done" }, { delegation_id: "" }, { agent: " " }, { text: [] },
    { voice_session_id: "another-call" }, { chat_session_id: "another-chat" }, { chat_session_id: "" },
    { run_id: null }, { model: 12 }, { provider: {} }, { type: "transcript" },
    { status: "working", run_id: "" }, { status: "completed", run_id: "" },
  ];
  f.read.resolve({ ...snapshot, cursor: 500, events: [
    { type: "transcript", seq: 500, speaker: "assistant", text: "I am sending this to the assistant now." },
  ], delegations: [...invalid.map((change) => ({ ...handoff("bad", 1, "queued"), ...change })), null, []] } as LiveSnapshot);
  await setImmediate();
  assert.deepEqual(f.controller.getSnapshot().delegations, []);
  assert.equal(f.controller.getSnapshot().captions.length, 1); assert.notEqual(f.controller.getSnapshot().phase, "error");
});

test("bounded recent terminal handoffs do not evict active work", async (t) => {
  const f = fixture(); t.after(() => f.controller.dispose());
  const start = f.controller.start("marin", revision, "ngn-chat-root"); f.prepare.resolve(); f.create.resolve(created); await start;
  f.read.resolve({ ...snapshot, cursor: 100, events: [], delegations: [
    handoff("active", 1, "working"),
    ...Array.from({ length: 40 }, (_, i) => handoff(`finished-${i}`, i + 2, "completed")),
  ] }); await setImmediate();
  assert.equal(f.controller.getSnapshot().delegations.length, 33);
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.id, "active");
  assert.equal(f.controller.getSnapshot().delegations.some((value) => value.id === "finished-0"), false);
});

for (const status of ["queued", "working", "completed", "failed", "cancelled"] as const) test(`closing voice preserves actual ${status} work status and reconnect clears it`, async (t) => {
  const f = fixture(); t.after(() => f.controller.dispose());
  const start = f.controller.start("marin", revision, "ngn-chat-root"); f.prepare.resolve(); f.create.resolve(created); await start;
  f.closeSnapshot({ ...snapshot, status: "closed", cursor: 3, delegations: [handoff("request-a", 3, status)] });
  await f.controller.end();
  assert.equal(f.controller.getSnapshot().phase, "ended");
  assert.equal(currentLiveDelegation(f.controller.getSnapshot().delegations)?.status, status);
  const nextRead = deferred<LiveSnapshot>(); f.nextRead(nextRead.promise);
  const restart = f.controller.start("marin", revision, "another-root");
  assert.deepEqual(f.controller.getSnapshot().delegations, []);
  await restart;
  f.read.resolve({ ...snapshot, cursor: 99, delegations: [handoff("old-poll", 99, "working")] }); await setImmediate();
  assert.deepEqual(f.controller.getSnapshot().delegations, [], "late old-call polling cannot leak the previous handoff");
});

test("device switches keep the live session and mute state, and failed changes leave the previous device selected", async () => {
  const f = fixture();
  const starting = f.controller.start("marin", revision, "ngn-chat-root");
  f.prepare.resolve(); f.create.resolve(created); await starting; f.handlers().connected();
  f.controller.muteInput(); f.controller.muteOutput();
  try {
    await f.controller.switchDevice("input", "new-mic");
    await f.controller.switchDevice("output", "new-speaker");
    assert.deepEqual(f.controller.getSnapshot().devices, { inputId: "new-mic", outputId: "new-speaker" });
    assert.equal(f.controller.getSnapshot().sessionId, created.session_id);
    assert.equal(f.controller.getSnapshot().micMuted, true);
    assert.equal(f.controller.getSnapshot().outputMuted, true);
    assert.equal(f.calls.filter(call => call.startsWith("create:")).length, 1);
    f.nextDevice(Promise.reject(new Error("Selected device unavailable")));
    await assert.rejects(f.controller.switchDevice("input", "missing"), /unavailable/);
    assert.equal(f.controller.getSnapshot().devices.inputId, "new-mic");
    assert.equal(f.controller.getSnapshot().phase, "connected");
    assert.ok(!f.calls.some(call => call.startsWith("close:")));
  } finally { f.controller.dispose(); }
});

test("ending voice prevents a pending device switch from committing to the ended or a later session", async () => {
  const f = fixture();
  await assert.rejects(f.controller.switchDevice("input", "unconnected"), /not connected/);
  const starting = f.controller.start("marin", revision, "ngn-chat-root");
  f.prepare.resolve(); f.create.resolve(created); await starting; f.handlers().connected();
  const pending = deferred<void>(); f.nextDevice(pending.promise);
  const oldDevice = f.controller.getSnapshot().devices.inputId;
  const switching = f.controller.switchDevice("input", "late-mic");
  await f.controller.end(); pending.resolve();
  await assert.rejects(switching, /Voice ended/);
  assert.equal(f.controller.getSnapshot().devices.inputId, oldDevice);
  assert.equal(f.controller.getSnapshot().phase, "ended");
  f.controller.dispose();
});
