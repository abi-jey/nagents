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
    render: async (item = delegation(), items = [item], token = "private-local-token") => act(async () => root.render(createElement(DelegationInspector, {
      token, delegation: item, delegations: items,
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
    assert.equal(calls[0].path, "/api/live/sessions/voice-one/delegation-details?delegation_id=request-one");
    assert.equal(calls[0].init.method, "GET");
    assert.deepEqual(calls[0].init.headers, { "X-Ngn-Token": "private-local-token" });
    assert.equal(calls[0].init.credentials, "same-origin");
    assert.equal(calls[0].init.cache, "no-store");
    assert.equal(calls[0].init.body, undefined);
    assert.equal(ui.payload("Speech transcript context")?.querySelector("pre")?.textContent, data.request.transcript!.text);
    assert.equal(ui.payload("Assistant request input")?.querySelector("pre")?.textContent, data.request.input.text);
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
      assert.equal(ui.payload("Speech transcript context"), undefined);
      assert.equal(ui.payload("Assistant request input"), undefined);
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
      assert.equal(ui.payload("Speech transcript context"), undefined);
      assert.equal(ui.payload("Assistant request input"), undefined);
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
      assert.ok(ui.payload("Speech transcript context"));
      const next = delegation(change);
      await ui.render(next);
      assert.equal(calls, 2, "every bound identity change fetches scoped details");
      assert.equal(ui.payload("Speech transcript context"), undefined, "an old payload cannot be shown under new ownership");
      await act(async () => pending.resolve(Response.json(detail(next))));
      assert.ok(ui.payload("Speech transcript context"));
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
    assert.equal(ui.payload("Speech transcript context"), undefined);
    await act(async () => ui.button("Refresh").click());
    assert.deepEqual(paths, Array(2).fill("/api/live/sessions/voice-one/delegation-details?delegation_id=request-one"));
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
    assert.match(ui.payload("Speech transcript context")!.textContent!, /Showing the first 3 of 20 characters/);
    assert.match(ui.payload("Assistant request input")!.textContent!, /No assistant request input was captured/);
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

function modelDetail(): LiveDelegationDetails {
  const data = detail(delegation({ seq: 6 }));
  const call = "a".repeat(32), attempt = "b".repeat(32);
  const model = { ...data.timeline.at(-1)!, type: "model_context" as const, seq: 2, status: "working" as const, model_call_id: call, round: 1, text: "Model input captured." };
  const body = { ...model, type: "http_request_body" as const, seq: 3, attempt_id: attempt, segmented: true, text: "Provider body chunk captured." };
  const payload = '{"messages":[{"role":"user","content":"<private>model input</private>"}],"tools":[],"config":{"temperature":0}}';
  data.timeline = [data.timeline[0], model, body, { ...body, seq: 4, capture_limited: true, text: "Model request capture limit reached." }, data.timeline.at(-1)!];
  data.model_requests = [
    { seq: 2, type: "model_context", model_call_id: call, round: 1, payload: { text: payload, characters: payload.length, truncated: false } },
    { seq: 3, type: "http_request_body", model_call_id: call, round: 1, attempt_id: attempt, segmented: true, payload: { text: '{"model":', characters: 9, truncated: true, characters_complete: false } },
  ];
  data.model_requests_truncated = true;
  return data;
}

test("typed model event rows expand their actual capture, with body chunks and retention limits explicitly labeled", async (t) => {
  const ui = view(), data = modelDetail();
  t.mock.method(globalThis, "fetch", async () => Response.json(data));
  try {
    await ui.render(delegation({ seq: 6 }));
    assert.equal(ui.container.querySelector('[role="alert"]'), null);
    assert.deepEqual([...ui.container.querySelectorAll(".inspection-event-type")].map(item => item.textContent), ["delegation", "model_context", "http_request_body", "http_request_body", "delegation"]);
    const model = ui.container.querySelector<HTMLElement>('[data-event-type="model_context"]')!;
    assert.equal(model.querySelector("pre")?.textContent, data.model_requests![0].payload.text);
    assert.match(model.textContent!, /Post-plugin model input, before provider encoding/);
    assert.equal(model.querySelector("private"), null);
    const disclosure = model.querySelector<HTMLDetailsElement>("details")!;
    assert.equal(disclosure.open, false);
    await act(async () => disclosure.querySelector<HTMLElement>("summary")!.click());
    assert.equal(disclosure.open, true);
    assert.deepEqual(JSON.parse(model.querySelector(".inspection-event-metadata pre")!.textContent!), data.timeline[1]);
    const bodies = ui.container.querySelectorAll('[data-event-type="http_request_body"]');
    assert.equal(bodies[0].querySelector("pre")?.textContent, '{"model":');
    assert.match(bodies[0].textContent!, /Observed provider request body chunk/);
    assert.match(bodies[0].textContent!, /full size was not recorded/);
    assert.match(bodies[1].querySelector("summary")!.textContent!, /Capture retention limit reached/);
    assert.match(bodies[1].textContent!, /No additional request payload was retained/);
    assert.match(ui.container.textContent!, /Some model request captures were omitted/);
    assert.equal([...ui.container.querySelectorAll("pre")].filter(pre => pre.textContent === data.model_requests![0].payload.text).length, 1, "timeline captures are not duplicated in a second list");
  } finally { await ui.close(); }
});

test("captures retained beyond the timeline remain separately expandable", async (t) => {
  const ui = view(), data = modelDetail();
  data.timeline = [data.timeline.at(-1)!]; data.timeline_truncated = true;
  t.mock.method(globalThis, "fetch", async () => Response.json(data));
  try {
    await ui.render(delegation({ seq: 6 }));
    assert.match(ui.container.textContent!, /Earlier model captures/);
    assert.equal(ui.payload("model_context · round 1 · #2")?.querySelector("pre")?.textContent, data.model_requests![0].payload.text);
    assert.match(ui.payload("http_request_body · round 1 · #3")?.textContent || "", /body chunk/);
  } finally { await ui.close(); }
});

for (const [name, change] of [
  ["missing model correlation", (data: LiveDelegationDetails) => { delete data.timeline[1].model_call_id; }],
  ["a limit marker without correlation", (data: LiveDelegationDetails) => { delete data.timeline[3].model_call_id; }],
  ["a mismatched capture round", (data: LiveDelegationDetails) => { data.model_requests![0].round = 2; }],
  ["an unsegmented HTTP payload", (data: LiveDelegationDetails) => { data.model_requests![1].segmented = false; }],
  ["duplicate capture identities", (data: LiveDelegationDetails) => { data.model_requests!.push(data.model_requests![0]); }],
  ["a capture over its payload bound", (data: LiveDelegationDetails) => { data.model_requests![0].payload = { text: "x".repeat(65537), characters: 65537, truncated: false }; }],
  ["a Unicode capture over its byte bound", (data: LiveDelegationDetails) => { data.model_requests![0].payload = { text: "🌍".repeat(20000), characters: 20000, truncated: false }; }],
  ["captures over their aggregate byte bound", (data: LiveDelegationDetails) => {
    data.seq = 30;
    data.model_requests = Array.from({ length: 5 }, (_, index) => ({ ...data.model_requests![0], seq: 20 + index, payload: { text: "界".repeat(21000), characters: 21000, truncated: false } }));
  }],
  ["an unknown total on a supposedly complete payload", (data: LiveDelegationDetails) => { data.model_requests![0].payload.characters_complete = false; }],
] as const) {
  test(`inspector rejects ${name} without rendering captured input`, async (t) => {
    const ui = view(), data = modelDetail(); change(data);
    t.mock.method(globalThis, "fetch", async () => Response.json(data));
    try {
      await ui.render(delegation({ seq: 6 }));
      assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /incomplete or inconsistent|did not match/);
      assert.equal(ui.container.querySelector("pre"), null);
    } finally { await ui.close(); }
  });
}

test("a changed authentication token hides retained request captures until the new scoped fetch returns", async (t) => {
  const ui = view(), pending = pendingResponse(); let calls = 0;
  t.mock.method(globalThis, "fetch", async () => ++calls === 1 ? Response.json(modelDetail()) : pending.promise);
  try {
    const item = delegation({ seq: 6 });
    await ui.render(item);
    assert.ok(ui.container.querySelector('[data-event-type="model_context"] pre'));
    await ui.render(item, [item], "new-token");
    assert.equal(ui.container.querySelector("pre"), null);
    await act(async () => pending.resolve(Response.json(modelDetail())));
    assert.ok(ui.container.querySelector('[data-event-type="model_context"] pre'));
  } finally { await ui.close(); }
});

function appendDetail(): LiveDelegationDetails {
  const data = detail(delegation({ id: "provider:delegation/one.v2", seq: 7 }));
  const base = data.timeline.at(-1)!;
  data.timeline = [...data.timeline.slice(0, 3),
    { ...base, seq: 4, type: "delegation", detail_type: "live_append", text: "Sent thinking to voice." },
    { ...base, seq: 5, type: "delegation", detail_type: "live_append", text: "Sent commentary to voice." },
    { ...base, seq: 6, type: "delegation", detail_type: "live_delivery_failed", text: "A voice write failed." },
    base];
  const content = (text: string) => ({ text, characters: Array.from(text).length, truncated: false });
  data.live_updates = [
    { seq: 4, kind: "thinking", outcome: "sent", wire_type: "delegation.context.append", content: content("Working on <private>the request</private>.") },
    { seq: 5, kind: "commentary", outcome: "sent", wire_type: "session.commentary.append", content: content("The result is four.") },
    { seq: 6, kind: "instructions", outcome: "failed", wire_type: "", content: content("An update is ready.") },
  ];
  data.live_updates_truncated = false;
  return data;
}

test("Live append rows show their channel, exact expandable payload and honest transport outcomes", async t => {
  const ui = view(), data = appendDetail(), item = delegation({ id: data.delegation_id, seq: 7 });
  t.mock.method(globalThis, "fetch", async () => Response.json(data));
  try {
    await ui.render(item);
    assert.equal(ui.container.querySelector('[role="alert"]'), null);
    const sent = ui.container.querySelector('[data-event-type="live_append"]')!;
    assert.match(sent.querySelector("summary")!.textContent!, /live_append · thinking.*Sent.*#4/);
    assert.equal(sent.querySelector("pre")!.textContent, data.live_updates![0].content.text);
    assert.equal(sent.querySelector("private"), null, "retained update content is inert text");
    assert.match(sent.textContent!, /does not confirm provider acknowledgment or spoken audio/);
    assert.equal(sent.querySelector("details")!.open, false);
    await act(async () => sent.querySelector("summary")!.click());
    assert.equal(sent.querySelector("details")!.open, true);
    assert.deepEqual(JSON.parse(sent.querySelector(".inspection-event-metadata pre")!.textContent!), data.timeline[3]);
    const failed = ui.container.querySelector('[data-event-type="live_delivery_failed"]')!;
    assert.match(failed.querySelector("summary")!.textContent!, /instructions.*Failed/);
    assert.match(failed.textContent!, /not confirmed as sent.*remains available in chat/);
    assert.equal(failed.querySelector("pre")!.textContent, "An update is ready.");
    assert.equal(failed.getAttribute("data-write-outcome"), "failed");
  } finally { await ui.close(); }
});

test("retired Live payloads and earlier retained updates remain explicit and separately expandable", async t => {
  const ui = view(), data = appendDetail();
  data.timeline = [data.timeline[4], data.timeline.at(-1)!]; data.timeline_truncated = true;
  data.live_updates = [data.live_updates![0], data.live_updates![2]]; data.live_updates_truncated = true;
  t.mock.method(globalThis, "fetch", async () => Response.json(data));
  try {
    await ui.render(delegation({ id: data.delegation_id, seq: 7 }));
    assert.equal(ui.container.querySelector('[role="alert"]'), null);
    assert.match(ui.container.querySelector('[data-event-type="live_append"]')!.textContent!, /payload is no longer retained/);
    assert.match(ui.container.textContent!, /Earlier Live updates/);
    assert.match(ui.container.textContent!, /Some Live update payloads are no longer retained/);
    assert.equal(ui.payload("live_append · thinking · Sent · #4")?.querySelector("pre")?.textContent, data.live_updates[0].content.text);
    assert.equal(ui.payload("live_append · instructions · Failed · #6")?.querySelector("pre")?.textContent, data.live_updates[1].content.text);
  } finally { await ui.close(); }
});

for (const id of ["provider:request/part.one", ".", "..", "provider/#?% request", "界".repeat(256), "😀".repeat(256)]) {
  test(`provider delegation ID ${id.length > 30 ? "Unicode bound" : JSON.stringify(id)} uses an exact same-origin query lookup`, async t => {
    const ui = view(), item = delegation({ id }); let target = "";
    t.mock.method(globalThis, "fetch", async (path: string) => { target = path; return Response.json(detail(item)); });
    try {
      await ui.render(item);
      assert.equal(ui.container.querySelector('[role="alert"]'), null);
      const url = new URL(target, "https://localhost/");
      assert.equal(url.origin, "https://localhost");
      assert.equal(url.pathname, "/api/live/sessions/voice-one/delegation-details");
      assert.equal(url.searchParams.get("delegation_id"), id);
      assert.equal([...url.searchParams.keys()].length, 1);
    } finally { await ui.close(); }
  });
}

for (const id of ["", " \n", "x".repeat(257), "界".repeat(257)]) {
  test(`invalid provider ID of ${id.length} characters never requests details`, async t => {
    const ui = view(); let reads = 0;
    t.mock.method(globalThis, "fetch", async () => { reads++; return Response.json(detail()); });
    try {
      await ui.render(delegation({ id }));
      assert.equal(reads, 0);
      assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /identifier is invalid/);
    } finally { await ui.close(); }
  });
}

for (const [name, change] of [
  ["future update", (data: LiveDelegationDetails) => { data.live_updates![0].seq = 8; }],
  ["duplicate update", (data: LiveDelegationDetails) => { data.live_updates!.push(data.live_updates![0]); }],
  ["mismatched timeline outcome", (data: LiveDelegationDetails) => { data.timeline[3].detail_type = "live_delivery_failed"; }],
  ["wrong wire channel", (data: LiveDelegationDetails) => { data.live_updates![0].wire_type = "session.instructions.append"; }],
  ["successful write without wire event", (data: LiveDelegationDetails) => { data.live_updates![0].wire_type = ""; }],
  ["failed write claiming a sent event", (data: LiveDelegationDetails) => { data.live_updates![2].wire_type = "session.instructions.append"; }],
  ["nonintegral update sequence", (data: LiveDelegationDetails) => { data.live_updates![0].seq = 4.5; }],
  ["too many updates", (data: LiveDelegationDetails) => { data.live_updates = Array(97).fill(data.live_updates![0]); }],
  ["too many payload bytes", (data: LiveDelegationDetails) => { data.live_updates![0].content = { text: "界".repeat(667), characters: 667, truncated: false }; }],
  ["inconsistent payload count", (data: LiveDelegationDetails) => { data.live_updates![0].content.characters = 0; }],
  ["model capture using an opaque provider ID", (data: LiveDelegationDetails) => { data.timeline[3].type = "model_context"; data.timeline[3].model_call_id = "provider:call/one"; data.timeline[3].round = 1; }],
] as const) {
  test(`inspector rejects ${name} before showing any Live payload`, async t => {
    const ui = view(), data = appendDetail(); change(data);
    t.mock.method(globalThis, "fetch", async () => Response.json(data));
    try {
      await ui.render(delegation({ id: data.delegation_id, seq: 7 }));
      assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /incomplete or inconsistent|did not match/);
      assert.equal(ui.container.querySelector("pre"), null);
    } finally { await ui.close(); }
  });
}
