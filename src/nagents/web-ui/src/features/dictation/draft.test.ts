import assert from "node:assert/strict";
import test from "node:test";
import { insertDraft, promptFailure } from "./draft.js";

test("insertion preserves the latest draft and exact transcription, adding only a needed separator", () => {
  assert.deepEqual(insertDraft("", "  words 🎤  ", "root"), { ok: true, prompt: "  words 🎤  " });
  assert.deepEqual(insertDraft("Typed while waiting", "words", "root"), { ok: true, prompt: "Typed while waiting\nwords" });
  assert.deepEqual(insertDraft("draft ", "words", "root"), { ok: true, prompt: "draft words" });
  assert.deepEqual(insertDraft("draft", "\nwords", "root"), { ok: true, prompt: "draft\nwords" });
  assert.equal(insertDraft("draft", " \n", "root").ok, false);
});

test("32,000 UTF-16 character boundary never silently truncates", () => {
  assert.equal(insertDraft("", "x".repeat(32000), "root").ok, true);
  assert.equal(insertDraft("x".repeat(32000), "more", "root").ok, false);
  assert.equal(insertDraft("", "🎤".repeat(16001), "root").ok, false);
});

test("64 KiB counts UTF-8, JSON escaping, and the session ID rather than text length alone", () => {
  for (const prompt of ["界".repeat(22000), "\u0000".repeat(12000), "\\\"".repeat(16000)]) {
    const session = "root".repeat(400);
    assert.ok(prompt.length <= 32000);
    const bytes = new TextEncoder().encode(JSON.stringify({ session_id: session, prompt })).byteLength;
    assert.ok(bytes > 65536);
    assert.match(promptFailure(prompt, session), /64 KiB/);
  }
  const session = "root";
  const base = "界".repeat(21800);
  const size = new TextEncoder().encode(JSON.stringify({ session_id: session, prompt: base })).byteLength;
  const exact = base + "x".repeat(65536 - size);
  assert.equal(promptFailure(exact, session), "");
  assert.match(promptFailure(exact + "x", session), /64 KiB/);
});
