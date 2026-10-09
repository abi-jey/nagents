import assert from "node:assert/strict";
import test from "node:test";
import { RequestError } from "./client.js";
import { readSessionHistory } from "./sessionHistory.js";

const snapshot = { session_id: "ngn-selected", sessions: [{ id: "ngn-selected", title: "Selected chat", updated_at: "today" }], history: [], retained_tasks: [] };

test("initial busy session reads retry with bounded backoff and never resubmit work", async t => {
  let attempts = 0;
  const delays: number[] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    assert.equal(url, "/api/sessions"); assert.equal(init.method, "GET"); assert.equal(init.body, undefined);
    assert.equal(init.mode, "same-origin");
    assert.equal(new Headers(init.headers).get("X-Ngn-Token"), "token");
    return ++attempts < 3 ? Response.json({ detail: "Harness busy" }, { status: 409 }) : Response.json(snapshot);
  });
  const result = await readSessionHistory("token", new AbortController().signal, "", async delay => { delays.push(delay); });
  assert.deepEqual(result, snapshot); assert.equal(attempts, 3); assert.deepEqual(delays, [100, 250]);
});

test("known session fallback remains immediate and does not resume or create a session", async t => {
  const paths: string[] = [];
  t.mock.method(globalThis, "fetch", async (url: string, init: RequestInit) => {
    paths.push(url); assert.equal(init.method, "GET");
    return url === "/api/sessions" ? Response.json({ detail: "Harness busy" }, { status: 409 }) : Response.json(snapshot);
  });
  const result = await readSessionHistory("token", new AbortController().signal, "ngn-selected", async () => assert.fail("Fallback should not wait"));
  assert.deepEqual(result, snapshot); assert.deepEqual(paths, ["/api/sessions", "/api/sessions/ngn-selected"]);
});

test("exhausted busy reads preserve the server failure for explicit Reconnect", async t => {
  let attempts = 0;
  const delays: number[] = [];
  t.mock.method(globalThis, "fetch", async () => { attempts++; return Response.json({ detail: "Still busy" }, { status: 409 }); });
  await assert.rejects(readSessionHistory("token", new AbortController().signal, "", async delay => { delays.push(delay); }),
    cause => cause instanceof RequestError && cause.status === 409 && cause.message === "Still busy");
  assert.equal(attempts, 5); assert.deepEqual(delays, [100, 250, 500, 1000]);
});

for (const status of [403, 404, 500]) test(`non-busy HTTP ${status} fails immediately`, async t => {
  let attempts = 0;
  t.mock.method(globalThis, "fetch", async () => { attempts++; return Response.json({ detail: "Actual failure" }, { status }); });
  await assert.rejects(readSessionHistory("token", new AbortController().signal, "", async () => assert.fail("Must not retry")),
    cause => cause instanceof RequestError && cause.status === status && cause.message === "Actual failure");
  assert.equal(attempts, 1);
});

test("invalid history and transport failures are not converted into busy retries", async t => {
  let invalid = true;
  t.mock.method(globalThis, "fetch", async () => {
    if (invalid) return Response.json({ session_id: "bad" });
    throw new Error("Connection unavailable");
  });
  const pause = async () => assert.fail("Only a busy HTTP response can retry");
  await assert.rejects(readSessionHistory("token", new AbortController().signal, "", pause), /Invalid session history/);
  invalid = false;
  await assert.rejects(readSessionHistory("token", new AbortController().signal, "", pause), /Connection unavailable/);
});

test("aborting during real backoff cancels the wait and prevents another request", async t => {
  const controller = new AbortController();
  let attempts = 0, waiting = () => {};
  const scheduled = new Promise<void>(resolve => { waiting = resolve; });
  const originalSetTimeout = globalThis.setTimeout;
  t.mock.method(globalThis, "setTimeout", ((callback: () => void, delay: number) => {
    const timer = originalSetTimeout(callback, delay); waiting(); return timer;
  }) as typeof setTimeout);
  t.mock.method(globalThis, "fetch", async () => { attempts++; return Response.json({ detail: "Busy" }, { status: 409 }); });
  const read = readSessionHistory("token", controller.signal);
  const rejected = assert.rejects(read, cause => cause instanceof DOMException && cause.name === "AbortError");
  await scheduled;
  controller.abort();
  await rejected;
  assert.equal(attempts, 1);
});

test("aborted and non-cooperative late reads cannot publish a stale snapshot", async t => {
  const controller = new AbortController();
  let release: (response: Response) => void = () => {}, called = 0;
  const response = new Promise<Response>(resolve => { release = resolve; });
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    called++; assert.equal(init.signal, controller.signal); return response;
  });
  const reading = readSessionHistory("token", controller.signal);
  controller.abort(); release(Response.json(snapshot));
  await assert.rejects(reading, cause => cause instanceof DOMException && cause.name === "AbortError");
  await assert.rejects(readSessionHistory("token", controller.signal), cause => cause instanceof DOMException && cause.name === "AbortError");
  assert.equal(called, 1);
});
