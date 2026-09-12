import assert from "node:assert/strict";
import test from "node:test";
import { RequestError } from "../../api/client.js";
import { transcribe } from "./transport.js";
import { deferred, dictationContext } from "./testFixtures.js";
import { WavEncoder } from "./pcm.js";

function audio() {
  const encoder = new WavEncoder(16000, 1, 36096);
  encoder.push(new Float32Array(160));
  return encoder.finish();
}

test("transcription sends raw WAV only to the local endpoint with exact scoped headers and cancellation", async (t) => {
  const blob = audio();
  const controller = new AbortController();
  const fetch = t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "/api/dictation/transcribe");
    assert.equal(init.method, "POST");
    assert.deepEqual(init.headers, {
      "X-Ngn-Token": dictationContext.token,
      "X-Ngn-Session": dictationContext.sessionId,
      "X-Ngn-Settings-Revision": dictationContext.config.revision,
      "Content-Type": "audio/wav",
    });
    assert.equal(init.body, blob);
    assert.equal(init.signal, controller.signal);
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.cache, "no-store");
    return Response.json({ text: "  Exact reviewed <text> 🎤  " });
  });
  assert.equal(await transcribe(dictationContext, blob, controller.signal), "  Exact reviewed <text> 🎤  ");
  assert.equal(fetch.mock.callCount(), 1);
});

test("invalid upload type, empty audio, missing scope, and oversize audio never reach fetch", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("Unexpected fetch"); });
  for (const blob of [new Blob([], { type: "audio/wav" }), new Blob([new Uint8Array(50)], { type: "audio/webm" }), new Blob([new Uint8Array(36097)], { type: "audio/wav" })])
    await assert.rejects(transcribe(dictationContext, blob, new AbortController().signal));
  for (const context of [{ ...dictationContext, sessionId: "" }, { ...dictationContext, token: "" }, { ...dictationContext, config: { ...dictationContext.config, revision: "" } }])
    await assert.rejects(transcribe(context, audio(), new AbortController().signal), /Reconnect/);
  const cancelled = new AbortController();
  cancelled.abort();
  await assert.rejects(transcribe(dictationContext, audio(), cancelled.signal), { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 0);
});

test("responses validate unknown text and do not accept blank or malformed transcripts", async (t) => {
  for (const value of [null, [], {}, { text: 1 }, { text: ["not text"] }, { text: "" }, { text: " \n" }]) {
    const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(value));
    await assert.rejects(transcribe(dictationContext, audio(), new AbortController().signal));
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.restore();
  }
  t.mock.method(globalThis, "fetch", async () => new Response("not JSON"));
  await assert.rejects(transcribe(dictationContext, audio(), new AbortController().signal), SyntaxError);
});

for (const status of [403, 409, 413, 415, 422, 502]) {
  test(`${status} transcription failures retain HTTP status and never retry`, async (t) => {
    const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({ detail: "Safe transcription error." }, { status }));
    await assert.rejects(transcribe(dictationContext, audio(), new AbortController().signal), (cause: unknown) => {
      assert.ok(cause instanceof RequestError);
      assert.equal(cause.status, status);
      assert.equal(cause.message, "Safe transcription error.");
      return true;
    });
    assert.equal(fetch.mock.callCount(), 1);
  });
}

test("cancellation propagates without retry and a response parsed after cancellation is rejected", async (t) => {
  const controller = new AbortController();
  const response = deferred<Response>();
  const fetch = t.mock.method(globalThis, "fetch", async () => response.promise);
  const pending = transcribe(dictationContext, audio(), controller.signal);
  controller.abort();
  response.resolve(Response.json({ text: "stale" }));
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 1);
});

test("fetch abortion and non-JSON upstream errors keep the original transport outcome", async (t) => {
  const controller = new AbortController();
  const fetch = t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
  }));
  const pending = transcribe(dictationContext, audio(), controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 1);
  fetch.mock.restore();
  t.mock.method(globalThis, "fetch", async () => new Response("proxy error", { status: 503 }));
  await assert.rejects(transcribe(dictationContext, audio(), new AbortController().signal), (cause: unknown) => cause instanceof RequestError && cause.status === 503);
});
