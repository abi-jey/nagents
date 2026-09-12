import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { DictationController, dictationFailure } from "./controller.js";
import { insertDraft } from "./draft.js";
import { deferred, dictationContext } from "./testFixtures.js";
import type { DictationContext, RecordedAudio, RecordingCallbacks } from "./types.js";

const audio: RecordedAudio = { blob: new Blob([new Uint8Array(364)], { type: "audio/wav" }), seconds: 0.01, limited: false };

function fixture() {
  const permission = deferred<void>();
  const reply = deferred<string>();
  const calls: string[] = [];
  let callbacks!: RecordingCallbacks;
  let uploadContext: DictationContext | undefined;
  let signal: AbortSignal | undefined;
  const controller = new DictationController({
    record: (_config, handlers) => {
      calls.push("record");
      callbacks = handlers;
      return { start: () => permission.promise, stop: () => { calls.push("stop"); }, cancel: () => { calls.push("cancel"); } };
    },
    transcribe: async (context, blob, abort) => {
      calls.push("upload");
      assert.equal(blob, audio.blob);
      uploadContext = context;
      signal = abort;
      return reply.promise;
    },
  });
  return { controller, permission, reply, calls, callbacks: () => callbacks, context: () => uploadContext, signal: () => signal };
}

test("permission and double clicks do not upload; explicit stop leads to editable review and latest-draft insertion only", async () => {
  const f = fixture();
  let draft = "Original draft";
  const start = f.controller.start(dictationContext);
  void f.controller.start(dictationContext);
  assert.equal(f.controller.getSnapshot().phase, "permission");
  assert.deepEqual(f.calls, ["record"]);
  f.permission.resolve();
  await start;
  assert.equal(f.controller.getSnapshot().phase, "recording");
  draft += " and typing while waiting";
  f.controller.stop();
  f.controller.stop();
  assert.deepEqual(f.calls, ["record", "stop"]);
  f.callbacks().stopped(audio);
  assert.equal(f.controller.getSnapshot().phase, "transcribing");
  f.reply.resolve("Transcribed text");
  await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "review");
  assert.equal(draft, "Original draft and typing while waiting");
  f.controller.edit("Edited text");
  assert.equal(f.controller.insert((text) => {
    const result = insertDraft(draft, text, dictationContext.sessionId);
    assert.ok(result.ok);
    draft = result.prompt;
    return "";
  }), true);
  assert.equal(draft, "Original draft and typing while waiting\nEdited text");
  assert.equal(f.controller.active, false);
  assert.deepEqual(f.calls, ["record", "stop", "upload"]);
  assert.equal(f.controller.insert(() => assert.fail("No duplicate insert")), false);
});

test("the duration cap stops without automatic upload; explicit Transcribe is single-flight", async () => {
  const f = fixture();
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.callbacks().stopped({ ...audio, limited: true });
  assert.equal(f.controller.getSnapshot().phase, "recorded");
  assert.deepEqual(f.calls, ["record"]);
  const pending = f.controller.transcribe();
  void f.controller.transcribe();
  assert.deepEqual(f.calls, ["record", "upload"]);
  f.reply.resolve("Review me");
  await pending;
  assert.equal(f.controller.getSnapshot().phase, "review");
});

test("an unsolicited recorder completion cannot authorize an upload", async () => {
  const f = fixture();
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.callbacks().stopped(audio);
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.equal(f.controller.active, false);
  assert.equal(f.calls.includes("upload"), false);
});

test("duplicate completion and late capture errors cannot upload again or discard reviewed text", async () => {
  const f = fixture();
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.controller.stop();
  f.callbacks().stopped(audio);
  f.reply.resolve("Review once");
  await setImmediate();
  f.controller.edit("Keep this edited review");
  f.callbacks().stopped(audio);
  f.callbacks().failed(new Error("Late recorder failure"));
  await setImmediate();
  assert.equal(f.calls.filter((call) => call === "upload").length, 1);
  assert.deepEqual(f.controller.getSnapshot(), {
    phase: "review", text: "Keep this edited review", error: "",
  });
});

test("edited review survives hidden/visible transitions without requests or resource operations", async () => {
  const f = fixture();
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.controller.stop();
  f.callbacks().stopped(audio);
  f.reply.resolve("Original transcription");
  await setImmediate();
  f.controller.edit("Keep these edits across a tab switch 🎤");
  const review = f.controller.getSnapshot();
  const calls = [...f.calls];
  let updates = 0;
  const unsubscribe = f.controller.subscribe(() => { updates++; });
  f.controller.visibilityChanged(true);
  f.controller.visibilityChanged(false);
  f.controller.visibilityChanged(true);
  f.controller.visibilityChanged(false);
  await setImmediate();
  assert.equal(f.controller.getSnapshot(), review);
  assert.deepEqual(f.calls, calls);
  assert.equal(f.signal()?.aborted, false);
  assert.equal(updates, 0);
  unsubscribe();
  // Explicit discard and the hook's real unmount/session cleanup still invalidate
  // review ownership; visibility handling must not weaken ordinary cancellation.
  f.controller.cancel();
  assert.equal(f.controller.getSnapshot().phase, "idle");
});

test("hiding still cancels permission, capture, finalization, uploads, and pending capped audio without restarting", async () => {
  for (const phase of ["permission", "recording", "stopping", "transcribing", "recorded"]) {
    const f = fixture();
    const start = f.controller.start(dictationContext);
    if (phase !== "permission") {
      f.permission.resolve();
      await start;
    }
    if (phase === "stopping" || phase === "transcribing") f.controller.stop();
    if (phase === "transcribing") f.callbacks().stopped(audio);
    if (phase === "recorded") f.callbacks().stopped({ ...audio, limited: true });
    assert.equal(f.controller.getSnapshot().phase, phase);
    f.controller.visibilityChanged(true);
    assert.equal(f.controller.getSnapshot().phase, "idle");
    if (phase === "transcribing") assert.equal(f.signal()?.aborted, true);
    if (["permission", "recording", "stopping"].includes(phase))
      assert.equal(f.calls.filter((call) => call === "cancel").length, 1);
    const calls = [...f.calls];
    f.permission.resolve();
    f.reply.resolve("Late text must not return");
    await start;
    f.controller.visibilityChanged(false);
    await f.controller.transcribe();
    await setImmediate();
    assert.equal(f.controller.getSnapshot().phase, "idle");
    assert.deepEqual(f.calls, calls);
  }
});

test("startup interruption cannot transition back to recording when its awaited operation completes", async () => {
  const f = fixture();
  const start = f.controller.start(dictationContext);
  f.callbacks().failed(new Error("Microphone recording was interrupted. Record again when ready."));
  assert.equal(f.controller.getSnapshot().phase, "error");
  f.permission.resolve();
  await start;
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.equal(f.controller.active, false);
  assert.equal(f.calls.includes("upload"), false);
});

test("recording captures the settings revision before permission and keeps it through upload", async () => {
  const f = fixture();
  const config = { ...dictationContext.config };
  const pending = f.controller.start({ ...dictationContext, config });
  config.revision = "changed-after-mic";
  f.permission.resolve();
  await pending;
  f.controller.stop();
  f.callbacks().stopped(audio);
  assert.equal(f.context()?.config.revision, dictationContext.config.revision);
  assert.equal(f.context()?.sessionId, dictationContext.sessionId);
  f.controller.cancel();
  assert.equal(f.signal()?.aborted, true);
});

test("cancel during permission ignores late start and old recorder callbacks", async () => {
  const f = fixture();
  const pending = f.controller.start(dictationContext);
  const previous = f.callbacks();
  f.controller.cancel();
  f.permission.resolve();
  await pending;
  assert.equal(f.controller.getSnapshot().phase, "idle");
  await f.controller.start(dictationContext);
  previous.stopped(audio);
  previous.failed(new Error("stale error"));
  assert.equal(f.controller.getSnapshot().phase, "recording");
  assert.equal(f.calls.includes("upload"), false);
  f.controller.cancel();
});

test("cancelled uploads cannot replace a newer operation even when the server ignores abort", async () => {
  const f = fixture();
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.controller.stop();
  f.callbacks().stopped(audio);
  f.controller.cancel();
  assert.equal(f.signal()?.aborted, true);
  await f.controller.start({ ...dictationContext, sessionId: "different-session" });
  f.reply.resolve("late transcript");
  await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "recording");
  f.controller.cancel();
});

test("overflow retains both reviewed text and existing draft; correcting review permits insertion", async () => {
  const f = fixture();
  let draft = "x".repeat(31999);
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.callbacks().stopped({ ...audio, limited: true });
  f.reply.resolve("too long");
  await f.controller.transcribe();
  const accept = (text: string) => {
    const result = insertDraft(draft, text, dictationContext.sessionId);
    if (!result.ok) return result.error;
    draft = result.prompt;
    return "";
  };
  assert.equal(f.controller.insert(accept), false);
  const review = f.controller.getSnapshot();
  assert.ok(review.phase === "review");
  assert.equal(review.text, "too long");
  assert.match(review.error, /both are kept/);
  assert.equal(draft.length, 31999);
  draft = "Shortened draft";
  f.controller.edit("corrected");
  assert.equal(f.controller.insert(accept), true);
  assert.equal(draft, "Shortened draft\ncorrected");
});

test("capture and upload failures unlock manual sending, discard audio, and never retry", async () => {
  const f = fixture();
  f.permission.resolve();
  await f.controller.start(dictationContext);
  f.controller.stop();
  f.callbacks().stopped(audio);
  f.reply.reject(new TypeError("Failed to fetch"));
  await setImmediate();
  assert.equal(f.controller.getSnapshot().phase, "error");
  assert.equal(f.controller.active, false);
  await f.controller.transcribe();
  assert.equal(f.calls.filter((call) => call === "upload").length, 1);
  for (const name of ["NotAllowedError", "NotFoundError", "NotReadableError"])
    assert.match(dictationFailure(new DOMException("Device error", name)), /typing/);
});

test("unavailable or administrator-disabled configuration cannot start recording", async () => {
  for (const config of [{ ...dictationContext.config, available: false }, { ...dictationContext.config, enabled: false }, { ...dictationContext.config, admin_enabled: false }]) {
    const f = fixture();
    await f.controller.start({ ...dictationContext, config });
    assert.equal(f.controller.active, false);
    assert.equal(f.controller.getSnapshot().phase, "error");
    assert.deepEqual(f.calls, []);
  }
});
