import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { VoiceContextDetails } from "./VoiceContextDetails.js";
import type { VoiceContext, VoiceContextDetailsRecord } from "./types.js";

const context: VoiceContext = {
  mode: "recent", method: "recent", chat_session_id: "ngn-chat", fingerprint: "a".repeat(64),
  message_count: 1, characters: 30, bytes: 30, summary_included: false, omitted_messages: 2,
  omitted_content: true, notice: "Recent conversation was included.",
};

function detail(metadata = context, sessionId = "voice-one", changes: Partial<VoiceContextDetailsRecord> = {}): VoiceContextDetailsRecord {
  const instructions = "Use the selected assistant. <private>Exact voice instructions</private>";
  return { ...metadata, voice_session_id: sessionId, available: true, reason: "", history_truncated: false,
    instructions: { text: instructions, characters: instructions.length, truncated: false },
    history: [{ type: "message", role: "assistant", content: [{ type: "input_text", text: "The exact startup message." }] }], ...changes };
}

function deferred() {
  let resolve!: (response: Response) => void;
  return { promise: new Promise<Response>(done => { resolve = done; }), resolve: (response: Response) => resolve(response) };
}

function view() {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container); let mounted = true;
  const payload = (name: string) => [...container.querySelectorAll("details")].find(item => item.querySelector("summary")?.textContent?.startsWith(name));
  return { container, payload,
    render: async (metadata = context, sessionId = "voice-one", token = "private-token") => act(async () => root.render(createElement(VoiceContextDetails, { context: metadata, sessionId, token, close() {} }))),
    refresh: async () => act(async () => [...container.querySelectorAll("button")].find(item => item.textContent === "Refresh context")!.click()),
    unmount: async () => { await act(async () => root.unmount()); mounted = false; },
    async close() {
      if (mounted) await act(async () => root.unmount());
      dom.window.close();
      for (const key of Object.keys(globals)) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("expanded context fetches only the authenticated session and renders captured instructions and startup messages as text", async (t) => {
  const ui = view(), data = detail();
  const calls: { path: string; init: RequestInit }[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => { calls.push({ path, init }); return Response.json(data); });
  try {
    await ui.render();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, "/api/live/sessions/voice-one/context");
    assert.equal(calls[0].init.method, "GET");
    assert.deepEqual(calls[0].init.headers, { "X-Ngn-Token": "private-token" });
    assert.equal(calls[0].init.credentials, "same-origin");
    assert.equal(calls[0].init.cache, "no-store");
    assert.equal(calls[0].init.body, undefined);
    assert.equal(ui.payload("Voice instructions")?.querySelector("pre")?.textContent, data.instructions.text);
    assert.deepEqual(JSON.parse(ui.payload("Voice startup history")!.querySelector("pre")!.textContent!), data.history);
    assert.equal(ui.container.querySelector("private"), null, "captured instructions never become markup");
    assert.match(ui.container.textContent!, /JSON formatting below is for inspection/);
    assert.match(ui.container.textContent!, /Voice settings → Starting context/);
    const disclosure = ui.payload("Voice startup history")!;
    assert.equal(disclosure.open, false);
    await act(async () => disclosure.querySelector<HTMLElement>("summary")!.click());
    assert.equal(disclosure.open, true);
  } finally { await ui.close(); }
});

for (const [name, change] of [
  ["another voice session", { voice_session_id: "voice-other" }],
  ["another chat", { chat_session_id: "ngn-other" }],
  ["another seed", { fingerprint: "b".repeat(64) }],
  ["unsupported history role", { history: [{ type: "message", role: "system", content: [{ type: "input_text", text: "Not an actual seed message" }] }] }],
  ["non-text seed data", { history: [{ type: "message", role: "user", content: [{ type: "input_image", data: "not text" }] }] }],
  ["an invalid capture length", { instructions: { text: "🎤", characters: 2, truncated: false } }],
  ["oversized instructions", { instructions: { text: "x".repeat(16385), characters: 16385, truncated: false } }],
  ["instructions over the UTF-8 byte limit", { instructions: { text: "🌍".repeat(5000), characters: 5000, truncated: false } }],
  ["too many seed messages", { history: Array.from({ length: 17 }, () => detail().history[0]) }],
] as const) {
  test(`context details reject ${name} without exposing retained content`, async (t) => {
    const ui = view(); t.mock.method(globalThis, "fetch", async () => Response.json({ ...detail(), ...change }));
    try {
      await ui.render();
      assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /different voice session|incomplete or inconsistent/);
      assert.equal(ui.container.querySelector("pre"), null);
    } finally { await ui.close(); }
  });
}

test("unavailable and partially retained seeds have explicit labels without inventing missing input", async (t) => {
  const ui = view(); let reads = 0;
  t.mock.method(globalThis, "fetch", async () => Response.json(++reads === 1
    ? detail(context, "voice-one", { available: false, reason: "Starting context is no longer retained.", history: [], instructions: { text: "", characters: 0, truncated: false } })
    : detail(context, "voice-one", { history_truncated: true, instructions: { text: "Start", characters: 100, truncated: true } })));
  try {
    await ui.render();
    assert.match(ui.container.textContent!, /no longer retained/);
    assert.equal(ui.container.querySelector("pre"), null);
    await ui.refresh();
    assert.match(ui.payload("Voice instructions")?.textContent || "", /Showing the first 5 of 100 characters/);
    assert.match(ui.container.textContent!, /retained startup history is incomplete/);
    assert.deepEqual(JSON.parse(ui.payload("Voice startup history")!.querySelector("pre")!.textContent!), detail().history);
  } finally { await ui.close(); }
});

test("late context responses cannot replace another call's captured seed", async (t) => {
  const ui = view(), first = deferred(), second = deferred(); const signals: AbortSignal[] = [];
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => { signals.push(init.signal as AbortSignal); return signals.length === 1 ? first.promise : second.promise; });
  const next = { ...context, chat_session_id: "ngn-other", fingerprint: "b".repeat(64) };
  try {
    await ui.render();
    await ui.render(next, "voice-two");
    assert.equal(signals[0].aborted, true);
    await act(async () => second.resolve(Response.json(detail(next, "voice-two", { instructions: { text: "Selected instructions", characters: 21, truncated: false } }))));
    assert.equal(ui.payload("Voice instructions")?.querySelector("pre")?.textContent, "Selected instructions");
    await act(async () => first.resolve(Response.json(detail())));
    assert.equal(ui.payload("Voice instructions")?.querySelector("pre")?.textContent, "Selected instructions");
  } finally { await ui.close(); }
});

test("an empty truncated history preview does not claim the voice session received no history", async (t) => {
  const ui = view();
  t.mock.method(globalThis, "fetch", async () => Response.json(detail(context, "voice-one", { history: [], history_truncated: true })));
  try {
    await ui.render();
    assert.match(ui.payload("Voice startup history")?.textContent || "", /Startup history was omitted from this capture/);
    assert.doesNotMatch(ui.container.textContent!, /No history messages were supplied/);
  } finally { await ui.close(); }
});

test("token replacement hides a retained seed and unmount cancels the pending authenticated request", async (t) => {
  const ui = view(), pending = deferred(); let calls = 0; let signal: AbortSignal | undefined;
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => { signal = init.signal as AbortSignal; return ++calls === 1 ? Response.json(detail()) : pending.promise; });
  try {
    await ui.render();
    assert.ok(ui.payload("Voice instructions"));
    await ui.render(context, "voice-one", "rotated-token");
    assert.equal(ui.container.querySelector("pre"), null);
    await ui.unmount();
    assert.equal(signal?.aborted, true);
    await act(async () => pending.resolve(Response.json(detail())));
    assert.equal(ui.container.childElementCount, 0);
  } finally { await ui.close(); }
});

test("failed context requests can be explicitly refreshed", async (t) => {
  const ui = view(); let calls = 0;
  t.mock.method(globalThis, "fetch", async () => ++calls === 1 ? Response.json({ detail: "Context unavailable temporarily." }, { status: 503 }) : Response.json(detail()));
  try {
    await ui.render();
    assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /unavailable temporarily/);
    await ui.refresh();
    assert.equal(ui.container.querySelector('[role="alert"]'), null);
    assert.ok(ui.payload("Voice instructions"));
    assert.equal(calls, 2);
  } finally { await ui.close(); }
});
