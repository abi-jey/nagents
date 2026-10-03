import assert from "node:assert/strict";
import test from "node:test";
import { apiPath, serverSocketUrl } from "./origin.js";
import { request, requestBinary } from "./client.js";
import { subscribeEvents } from "./subscription.js";
import { loadMedia, MediaBudget } from "../features/chat/deliveries.js";
import { channelRequest } from "../features/channels/transport.js";

const externalRoutes = [
  "https://api.openai.com/audio", "http://elsewhere.test/audio", "wss://chatgpt.com/audio",
  "//elsewhere.test/audio", "/\\elsewhere.test/audio", "\\\\elsewhere.test/audio",
  "data:audio/wav;base64,AAAA", "blob:https://elsewhere.test/audio", " live/sessions", "live/\nsessions",
];

function page(t: import("node:test").TestContext, href = "https://ngn.local:8765/chat") {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "location");
  Object.defineProperty(globalThis, "location", { configurable: true, value: new URL(href) });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "location", previous);
    else Reflect.deleteProperty(globalThis, "location");
  });
}

test("API routes reject external URLs and browser URL normalization escapes before fetching", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({}));
  assert.equal(apiPath("live/sessions/one?after=2"), "/api/live/sessions/one?after=2");
  for (const route of externalRoutes) {
    assert.throws(() => apiPath(route), /local ngn API route/);
    await assert.rejects(request(route, "private-ngn-token"), /local ngn API route/);
    await assert.rejects(requestBinary(route, "private-ngn-token", new Blob(), {}, new AbortController().signal), /local ngn API route/);
  }
  assert.equal(fetch.mock.calls.length, 0, "neither credentials nor data may reach fetch for an external URL");
});

test("JSON, binary, channel, and attachment fetches prohibit redirects and cross-origin requests", async (t) => {
  const calls: { path: string; init: RequestInit }[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    calls.push({ path, init });
    if (path === "/api/channels") return Response.json({ revision: "one", plugin_path: "plugins", plugins: [], connections: [], bindings: [] });
    if (path === "/api/sessions/one/audio") return new Response(new Uint8Array(2), { headers: { "Content-Type": "audio/wav", "Content-Length": "2" } });
    return Response.json({});
  });
  const signal = new AbortController().signal;
  await request("live/sessions", "ngn-token", { voice: "cove" });
  await requestBinary("uploads", "ngn-token", new Blob(["sample"]), {}, signal);
  await channelRequest("ngn-token", signal, "load");
  const media = await loadMedia("ngn-token", "sessions/one/audio", { media_type: "audio/wav", byte_length: 2 }, signal);
  try {
    assert.match(media.url, /^blob:/, "playback receives a local object URL after ngn serves the media");
    assert.equal(calls.length, 4);
    for (const { path, init } of calls) {
      assert.ok(path.startsWith("/api/"));
      assert.equal(init.mode, "same-origin");
      assert.equal(init.redirect, "error", "ngn cannot redirect the browser to a provider or external attachment URL");
      assert.equal(init.credentials, "same-origin");
      assert.equal(new Headers(init.headers).get("X-Ngn-Token"), "ngn-token");
    }
  } finally { media.release(); }
});

test("external audio URLs are rejected without consuming the media budget", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { assert.fail("external audio must never be fetched"); });
  const budget = new MediaBudget(2, 1);
  for (const path of externalRoutes)
    await assert.rejects(loadMedia("ngn-token", path, { media_type: "audio/wav", byte_length: 2 }, new AbortController().signal, budget), /local ngn API route/);
  assert.equal(fetch.mock.calls.length, 0);
  budget.reserve(2).release();
});

test("event and audio WebSocket addresses remain at the actual ngn origin", (t) => {
  page(t);
  assert.equal(serverSocketUrl("/api/events"), "wss://ngn.local:8765/api/events");
  assert.equal(serverSocketUrl("/api/events", "https://ngn.local:8765/ignored?query=1"), "wss://ngn.local:8765/api/events");
  for (const path of externalRoutes) assert.throws(() => serverSocketUrl(path), /local ngn API route/);
  for (const base of ["https://api.openai.com", "https://chatgpt.com", "https://ngn.local:8766", "http://ngn.local:8765", "wss://ngn.local:8765", "https://user:pass@ngn.local:8765"])
    assert.throws(() => serverSocketUrl("/api/events", base), /ngn server origin/);
  const id = "https://elsewhere.test/../../audio?redirect=1";
  const url = new URL(serverSocketUrl(`/api/live/sessions/${encodeURIComponent(id)}/audio`));
  assert.equal(url.origin, "wss://ngn.local:8765");
  assert.equal(url.pathname, `/api/live/sessions/${encodeURIComponent(id)}/audio`);
  assert.equal(url.search, "");
});

test("localhost audio uses the ngn ws endpoint without upgrading to another origin", (t) => {
  page(t, "http://127.0.0.1:8765/");
  assert.equal(serverSocketUrl("/api/live/sessions/one/audio"), "ws://127.0.0.1:8765/api/live/sessions/one/audio");
});

test("an external event subscription base cannot construct a socket or leak its token", (t) => {
  page(t);
  let sockets = 0, cancelled = false;
  const stop = subscribeEvents({
    token: "private-ngn-token", sessionId: "root", url: "https://chatgpt.com",
    signal: new AbortController().signal, receive: () => {}, status: () => {},
    socket: () => { sockets++; assert.fail("external event socket must not be constructed"); },
    schedule: () => () => { cancelled = true; },
  });
  stop();
  assert.equal(sockets, 0);
  assert.equal(cancelled, true);
});
