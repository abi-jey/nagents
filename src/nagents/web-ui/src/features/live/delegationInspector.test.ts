import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { DelegationInspector } from "./DelegationInspector.js";
import type { LiveDelegation, LiveDelegationDetails, LiveDelegationRecord } from "./types.js";

function delegation(changes: Partial<LiveDelegation> = {}): LiveDelegation {
  return {
    id: "request-one", sessionId: "voice-one", chatSessionId: "ngn-chat-one", runId: "run-one",
    seq: 4, status: "completed", agent: "Research assistant", provider: "chat-connection", model: "chat-model",
    text: "The assistant finished. Its answer is ready.", ...changes,
  };
}

function detail(item = delegation(), changes: Partial<LiveDelegationDetails> = {}): LiveDelegationDetails {
  const transcript = '[{"speaker":"user","text":"Two plus two?"}]';
  const input = "Exact assistant instructions\n\nTwo plus two?";
  const event: LiveDelegationRecord = {
    seq: item.seq, delegation_id: item.id, voice_session_id: item.sessionId, chat_session_id: item.chatSessionId,
    run_id: item.runId, status: item.status, agent: item.agent, provider: item.provider, model: item.model, text: item.text,
  };
  return {
    ...event, source: "app_callback",
    request: {
      transcript: { text: transcript, characters: transcript.length, truncated: false },
      input: { text: input, characters: input.length, truncated: false },
    },
    result: { kind: "assistant_output", text: "Four.\n", characters: 6, truncated: false },
    timeline: [
      { ...event, seq: 1, status: "queued", run_id: "", text: "Waiting for the assistant." },
      { ...event, seq: 2, status: "working", text: "The assistant is working." },
      { ...event, seq: 3, status: "working", text: "The request was sent to the assistant." },
      event,
    ],
    timeline_truncated: false, ...changes,
  };
}

function pendingResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>(done => { resolve = done; });
  return { promise, resolve };
}

function view() {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const selected: string[] = [], viewed: string[] = [];
  let dismissed = 0, mounted = true;
  const button = (name: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(element => (element.getAttribute("aria-label") || element.textContent?.trim()) === name)!;
  const payload = (name: string) => [...container.querySelectorAll("details")].find(element => element.querySelector("summary")?.textContent?.startsWith(name));
  return {
    container, dom, button, payload, selected, viewed, dismissed: () => dismissed,
    render: async (item = delegation(), items = [item]) => act(async () => root.render(createElement(DelegationInspector, {
      token: "private-local-token", delegation: item, delegations: items,
      select: id => { selected.push(id); }, close: () => { dismissed++; }, viewChat: id => { viewed.push(id); },
    }))),
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

test("inspector fetches only the selected authenticated request and shows its exact payload, result, and app events", async (t) => {
  const ui = view();
  const data = detail();
  const input = "Instructions\n<private-payload>Two plus two?</private-payload>\n";
  data.request.input = { text: input, characters: input.length, truncated: false };
  const calls: { path: string; init: RequestInit }[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => { calls.push({ path, init }); return Response.json(data); });
  try {
    await ui.render(delegation(), [delegation(), delegation({ id: "request-two" })]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, "/api/live/sessions/voice-one/delegations/request-one");
    assert.equal(calls[0].init.method, "GET");
    assert.deepEqual(calls[0].init.headers, { "X-Ngn-Token": "private-local-token" });
    assert.equal(calls[0].init.credentials, "same-origin");
    assert.equal(calls[0].init.cache, "no-store");
    assert.equal(calls[0].init.body, undefined);
    assert.equal(ui.payload("Voice request payload")?.querySelector("pre")?.textContent, data.request.transcript!.text);
    assert.equal(ui.payload("Input sent to assistant")?.querySelector("pre")?.textContent, data.request.input.text);
    assert.equal(ui.container.querySelector("private-payload"), null, "payloads render as text, never markup");
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Four.\n");
    const events = [...ui.container.querySelectorAll("ol li")].map(row => JSON.parse(row.querySelector("pre")!.textContent!));
    assert.deepEqual(events, data.timeline);
    assert.match(ui.container.textContent!, /Application lifecycle events/);
    assert.match(ui.container.textContent!, /Tool execution and approvals are shown in the chat/);
    assert.match(ui.payload("Request identifiers")!.textContent!, /request-one.*voice-one.*ngn-chat-one.*run-one.*app_callback/);
    assert.equal(ui.dom.window.document.activeElement?.textContent, "Delegation details");
    await act(async () => ui.button("View in chat").click());
    assert.deepEqual(ui.viewed, ["run-one"]);
    await act(async () => {
      const select = ui.container.querySelector("select")!;
      select.value = "request-two"; select.dispatchEvent(new ui.dom.window.Event("change", { bubbles: true }));
    });
    assert.deepEqual(ui.selected, ["request-two"]);
    assert.equal(calls.length, 1, "changing the picker delegates selection to the parent");
    await act(async () => ui.button("Close delegation details").click());
    assert.equal(ui.dismissed(), 1);
  } finally { await ui.close(); }
});

test("a single request needs no picker while its target, timeline and payload identity remain available", async (t) => {
  const ui = view();
  let reads = 0;
  t.mock.method(globalThis, "fetch", async () => { reads++; return Response.json(detail()); });
  try {
    await ui.render();
    assert.equal(ui.container.querySelector(".delegation-request-picker"), null);
    assert.match(ui.container.querySelector(".delegation-target")?.textContent || "", /Research assistant.*chat-connection.*chat-model/);
    assert.deepEqual([...ui.container.querySelectorAll(".delegation-timeline > li")].map(item => item.getAttribute("data-status")), ["queued", "working", "working", "completed"]);
    assert.match(ui.payload("Request identifiers")?.textContent || "", /request-one.*voice-one.*ngn-chat-one.*run-one/);
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Four.\n");
    const intro = ui.container.querySelector<HTMLElement>(".delegation-inspector-intro")!;
    assert.equal(intro.hidden, false);
    assert.equal(intro.getAttribute("aria-hidden"), null, "Mobile compaction retains accessible descriptive text");
    await ui.render(delegation(), [delegation(), delegation({ id: "request-two" })]);
    assert.equal(ui.container.querySelectorAll(".delegation-request-picker option").length, 2);
    await ui.render();
    assert.equal(ui.container.querySelector("select"), null);
    assert.equal(reads, 1, "Adding or removing a picker does not refetch the unchanged selected request");
  } finally { await ui.close(); }
});

for (const [field, value] of [
  ["voice_session_id", "other-voice"], ["chat_session_id", "ngn-other-chat"], ["run_id", "other-run"],
  ["delegation_id", "other-request"], ["source", "provider_raw_event"],
] as const) {
  test(`inspector refuses a response with mismatched ${field}`, async (t) => {
    const ui = view();
    t.mock.method(globalThis, "fetch", async () => Response.json({ ...detail(), [field]: value }));
    try {
      await ui.render();
      assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /different voice request/);
      assert.equal(ui.payload("Voice request payload"), undefined);
      assert.equal(ui.payload("Input sent to assistant"), undefined);
      assert.equal(ui.container.querySelector("ol"), null);
      assert.equal(ui.button("Refresh").disabled, false);
    } finally { await ui.close(); }
  });
}

const inconsistentDetails: [string, (data: LiveDelegationDetails) => void][] = [
  ["a timeline event from another chat", data => { data.timeline[1].chat_session_id = "ngn-other-chat"; }],
  ["a working event without an admitted run", data => { data.timeline[1].run_id = ""; }],
  ["a payload count shorter than its actual text", data => { data.request.input!.characters = 1; }],
  ["an untruncated Unicode payload counted as UTF-16 units", data => {
    data.request.transcript = { text: "🎤", characters: 2, truncated: false };
  }],
];

for (const [name, change] of inconsistentDetails) {
  test(`inspector rejects ${name} before rendering any payload`, async (t) => {
    const ui = view(), data = detail();
    change(data);
    t.mock.method(globalThis, "fetch", async () => Response.json(data));
    try {
      await ui.render();
      assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /incomplete or inconsistent/);
      assert.equal(ui.payload("Voice request payload"), undefined);
      assert.equal(ui.payload("Input sent to assistant"), undefined);
      assert.equal(ui.container.querySelector("ol"), null);
      assert.equal(ui.button("Refresh").disabled, false);
    } finally { await ui.close(); }
  });
}

test("a late response from an aborted prior selection never replaces the selected request", async (t) => {
  const ui = view(), first = pendingResponse(), second = pendingResponse();
  const signals: AbortSignal[] = [];
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    signals.push(init.signal as AbortSignal);
    return ++calls === 1 ? first.promise : second.promise;
  });
  const next = delegation({ id: "request-two", sessionId: "voice-two", chatSessionId: "ngn-chat-two", runId: "run-two" });
  try {
    await ui.render();
    assert.equal(ui.button("Refresh").disabled, true);
    await ui.render(next);
    assert.equal(signals[0].aborted, true);
    assert.equal(signals[1].aborted, false);
    await act(async () => second.resolve(Response.json(detail(next, { result: { kind: "assistant_output", text: "Selected result", characters: 15, truncated: false } }))));
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Selected result");
    await act(async () => first.resolve(Response.json(detail())));
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Selected result");
    assert.doesNotMatch(ui.payload("Request identifiers")!.textContent!, /request-one|voice-one|ngn-chat-one|run-one/);
    assert.equal(ui.container.querySelector('[role="alert"]'), null);
  } finally { await ui.close(); }
});

for (const change of [{ chatSessionId: "ngn-other-chat" }, { runId: "other-run" }]) {
  test(`retained payload is hidden immediately when ${Object.keys(change)[0]} changes`, async (t) => {
    const ui = view(), pending = pendingResponse();
    let calls = 0;
    t.mock.method(globalThis, "fetch", async () => ++calls === 1 ? Response.json(detail()) : pending.promise);
    try {
      await ui.render();
      assert.ok(ui.payload("Voice request payload"));
      const next = delegation(change);
      await ui.render(next);
      assert.equal(calls, 2, "every bound identity change fetches scoped details");
      assert.equal(ui.payload("Voice request payload"), undefined, "an old payload cannot be shown under new ownership");
      await act(async () => pending.resolve(Response.json(detail(next))));
      assert.ok(ui.payload("Voice request payload"));
    } finally { await ui.close(); }
  });
}

test("a failed request can be retried without retaining the error or changing the selected identity", async (t) => {
  const ui = view();
  const paths: string[] = [];
  t.mock.method(globalThis, "fetch", async (path: string) => {
    paths.push(path);
    return paths.length === 1 ? Response.json({ detail: "Details temporarily unavailable." }, { status: 503 }) : Response.json(detail());
  });
  try {
    await ui.render();
    assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /temporarily unavailable/);
    assert.equal(ui.payload("Voice request payload"), undefined);
    await act(async () => ui.button("Refresh").click());
    assert.deepEqual(paths, Array(2).fill("/api/live/sessions/voice-one/delegations/request-one"));
    assert.equal(ui.container.querySelector('[role="alert"]'), null);
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Four.\n");
    assert.equal(ui.button("Refresh").disabled, false);
  } finally { await ui.close(); }
});

test("a newer lifecycle sequence refetches details and an older in-flight response cannot remove its result", async (t) => {
  const ui = view(), pending = pendingResponse();
  const working = delegation({ seq: 3, status: "working", text: "The request was sent to the assistant." });
  const data = detail(working);
  delete data.result;
  data.timeline = data.timeline.slice(0, 3);
  let calls = 0;
  let oldSignal: AbortSignal | undefined;
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    if (++calls === 1) { oldSignal = init.signal as AbortSignal; return pending.promise; }
    return Response.json(detail());
  });
  try {
    await ui.render(working);
    await ui.render(delegation());
    assert.equal(calls, 2);
    assert.equal(oldSignal?.aborted, true);
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Four.\n");
    await act(async () => pending.resolve(Response.json(data)));
    assert.equal(ui.payload("Assistant result")?.querySelector("pre")?.textContent, "Four.\n");
    assert.match(ui.container.querySelector("ol li:last-child")!.textContent!, /completed/);
  } finally { await ui.close(); }
});

test("queued cancellation shows captured request and a terminal explanation without inventing input or output", async (t) => {
  const ui = view();
  const item = delegation({ status: "cancelled", runId: "", seq: 2, text: "Voice ended before this request started." });
  const data = detail(item, {
    request: { transcript: { text: "🎤".repeat(3), characters: 20, truncated: true } },
    result: { kind: "terminal_explanation", text: item.text, characters: item.text.length, truncated: false },
    timeline_truncated: true,
  });
  data.timeline = [data.timeline[0], { ...data.timeline.at(-1)!, status: "cancelled" }];
  t.mock.method(globalThis, "fetch", async () => Response.json(data));
  try {
    await ui.render(item);
    assert.match(ui.payload("Voice request payload")!.textContent!, /Showing the first 3 characters/);
    assert.match(ui.payload("Input sent to assistant")!.textContent!, /has not been dispatched/);
    assert.equal(ui.payload("Outcome")?.querySelector("pre")?.textContent, item.text);
    assert.equal(ui.payload("Assistant result"), undefined);
    assert.equal(ui.button("View in chat"), undefined);
    assert.match(ui.container.textContent!, /Showing the most recent events/);
    assert.match(ui.payload("Request identifiers")!.textContent!, /Not admitted yet/);
  } finally { await ui.close(); }
});

test("unmount aborts the pending details request and ignores its late response", async (t) => {
  const ui = view(), pending = pendingResponse();
  let signal: AbortSignal | undefined;
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => { signal = init.signal as AbortSignal; return pending.promise; });
  try {
    await ui.render();
    assert.equal(signal?.aborted, false);
    await ui.unmount();
    assert.equal(signal?.aborted, true);
    await act(async () => pending.resolve(Response.json(detail())));
    assert.equal(ui.container.childElementCount, 0);
  } finally { await ui.close(); }
});
