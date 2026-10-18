import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import type { Snapshot, WireEvent } from "../../types.js";
import { Conversation } from "./Conversation.js";
import { applySnapshot, LiveSessions } from "./liveTranscript.js";

type HistoryRow = Snapshot["history"][number];
const row = (history_id: string, role: string, content: string, extra: Partial<HistoryRow> = {}): HistoryRow => ({
  history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra,
});
const responseMetadata = (response_id: string, history_id: string, status: "responding" | "completed") => ({
  response_id, response_session_id: "root", run_id: response_id === "old-response" ? "old-run" : "run",
  revision: status === "completed" ? 2 : 1, round_number: 1, history_id, status,
  summary: "A synthetic response summary", summary_truncated: false, summary_expired: false, usage_reported: false,
});

function fixture() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true });
  Object.assign(dom.window.HTMLElement.prototype, {
    attachEvent(this: HTMLElement, type: string, listener: EventListener) { this.addEventListener(type.replace(/^on/, ""), listener); },
    detachEvent(this: HTMLElement, type: string, listener: EventListener) { this.removeEventListener(type.replace(/^on/, ""), listener); },
  });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const render = (entries: ReturnType<LiveSessions["get"]>["entries"]) => act(async () => root.render(createElement(Conversation, {
    entries, sessionId: "root", demo: false, canSubmit: false, submit() {},
  })));
  return {
    dom, container, render,
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"])
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    },
  };
}

function snapshot(history: HistoryRow[], active_run?: Snapshot["active_run"]): Snapshot {
  return { session_id: "root", sessions: [], retained_tasks: [], history, active_run };
}

test("outgoing row stays mounted across early run_started, sparse active hydration, delayed acknowledgement and saved history", async () => {
  const ui = fixture();
  const cache = new LiveSessions();
  const baseline = snapshot([row("earlier-user", "user", "Earlier request", { message_id: "earlier-id" })]);
  cache.set("root", applySnapshot({ entries: [] }, baseline, false));
  const message = { session_id: "root", message_id: "new-id", prompt: "Keep this outgoing row visible" };
  let cursor = 0;
  const receive = (record: WireEvent) => cache.receive({ type: "event", session_id: "root", epoch: "one", cursor: ++cursor, record });
  try {
    await ui.render(cache.get("root").entries);
    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    Object.defineProperty(feed, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(feed, "scrollHeight", { configurable: true, value: 600 });
    cache.enqueue(message);
    await ui.render(cache.get("root").entries);
    const outgoing = [...ui.container.querySelectorAll<HTMLElement>("article.entry.user")].find(element =>
      element.textContent?.includes(message.prompt))!;
    const key = outgoing.dataset.recordKey;
    assert.ok(key);
    assert.match(outgoing.textContent!, /Sending…/);
    const position = [...ui.container.querySelectorAll("article.entry")].indexOf(outgoing);
    feed.scrollTop = 180;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));

    receive({ event: "run_started", run_id: "run", message_id: message.message_id });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), outgoing, "run_started updates the same DOM row");
    assert.equal(feed.scrollTop, 180);

    cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: ++cursor, snapshot: snapshot(baseline.history, {
      id: "run", status: "running", message_id: message.message_id,
      events: [{ event: "run_started", run_id: "run", message_id: message.message_id }],
    }) });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), outgoing,
      "An active replay that has not hydrated the user row must retain the bound optimistic row");
    assert.equal([...ui.container.querySelectorAll("article.entry")].indexOf(outgoing), position);
    assert.equal(feed.scrollTop, 180);

    receive({ event: "user_message", run_id: "run", history_id: "new-user-row", message_id: message.message_id, text: message.prompt });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelectorAll(`[data-record-key="${key}"]`).length, 1);
    assert.equal(outgoing.textContent?.includes(message.prompt), true);

    cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: ++cursor, snapshot: snapshot([
      ...baseline.history, row("new-user-row", "user", message.prompt, { message_id: message.message_id }),
    ], { id: "run", status: "running", message_id: message.message_id, events: [
      { event: "run_started", run_id: "run", message_id: message.message_id },
      { event: "user_message", run_id: "run", history_id: "new-user-row", message_id: message.message_id, text: message.prompt },
    ] }) });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), outgoing);
    assert.equal(ui.container.querySelectorAll("article.entry.user").length, 2);
    assert.equal(feed.scrollTop, 180);
  } finally { await ui.close(); }
});

test("assistant row survives completion with an older HTTP history baseline and reconciles by response identity", async () => {
  const ui = fixture();
  const cache = new LiveSessions();
  const oldResponse = responseMetadata("old-response", "old-answer", "completed");
  const baseline = snapshot([
    row("first-user", "user", "First question", { message_id: "first-id" }),
    row("old-answer", "assistant", "The same final answer", { response_id: "old-response", response_metadata: oldResponse }),
    row("current-user", "user", "Current question", { message_id: "current-id" }),
  ]);
  cache.set("root", applySnapshot({ entries: [] }, baseline, false));
  let cursor = 0;
  const receive = (record: WireEvent) => cache.receive({ type: "event", session_id: "root", epoch: "one", cursor: ++cursor, record });
  try {
    await ui.render(cache.get("root").entries);
    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    Object.defineProperty(feed, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(feed, "scrollHeight", { configurable: true, value: 600 });
    feed.scrollTop = 180;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));

    receive({ event: "run_started", run_id: "run", message_id: "current-id" });
    receive({ event: "response_metadata", ...responseMetadata("current-response", "current-answer", "responding") });
    receive({ event: "text_chunk", run_id: "run", response_id: "current-response", response_session_id: "root", chunk: "The same " });
    await ui.render(cache.get("root").entries);
    const streamed = ui.container.querySelector<HTMLElement>('article.entry.assistant[data-run-id="run"]')!;
    const key = streamed.dataset.recordKey;
    assert.ok(key);
    assert.equal(streamed.textContent?.includes("The same final answer"), false);
    const position = [...ui.container.querySelectorAll("article.entry")].indexOf(streamed);

    receive({ event: "text_chunk", run_id: "run", response_id: "current-response", response_session_id: "root", chunk: "final answer" });
    receive({ event: "text_done", run_id: "run", response_id: "current-response", response_session_id: "root",
      history_id: "current-answer", text: "The same final answer" });
    receive({ event: "response_metadata", ...responseMetadata("current-response", "current-answer", "completed") });
    receive({ event: "run_finished", run_id: "run", status: "completed" });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), streamed);
    assert.equal(streamed.textContent?.includes("The same final answer"), true);
    assert.equal([...ui.container.querySelectorAll("article.entry")].indexOf(streamed), position);

    // An unversioned HTTP read can lag completion. It must not evict the live
    // row while its durable history record has not arrived in this snapshot.
    cache.set("root", applySnapshot(cache.get("root"), baseline));
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), streamed);
    assert.equal(streamed.textContent?.includes("The same final answer"), true);
    assert.equal(ui.container.querySelectorAll("article.entry.assistant").length, 2,
      "Identical prose in a prior turn must remain a separate saved row");
    assert.equal(feed.scrollTop, 180);

    const finalMetadata = responseMetadata("current-response", "current-answer", "completed");
    cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: ++cursor, snapshot: snapshot([
      ...baseline.history,
      row("current-answer", "assistant", "The same final answer", { response_id: "current-response", response_metadata: finalMetadata }),
    ], null) });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), streamed,
      "Saved history reconciles to the streaming response's existing row identity");
    const assistantRows = [...ui.container.querySelectorAll<HTMLElement>("article.entry.assistant")];
    assert.equal(assistantRows.length, 2);
    assert.ok(assistantRows.every(element => element.querySelector(".entry-content")?.textContent?.startsWith("The same final answer")));
    assert.deepEqual(assistantRows.map(element => element.querySelector(".entry-label:not([hidden])")?.textContent?.trim()),
      ["Assistant", "Assistant"], "Each admitted turn gets one visible byline");
    assert.equal(feed.scrollTop, 180);
  } finally { await ui.close(); }
});

test("ordinary adapter responses keep their DOM row through same-turn tool rounds and repeated-text history handoff", async () => {
  const ui = fixture();
  const cache = new LiveSessions();
  const answer = "The same answer";
  const baseline = snapshot([
    row("earlier-user", "user", "Earlier question", { message_id: "earlier-id" }),
    row("earlier-answer", "assistant", answer),
    row("current-user", "user", "Current question", { message_id: "current-id" }),
  ]);
  cache.set("root", applySnapshot({ entries: [] }, baseline, false));
  let cursor = 0;
  const receive = (record: WireEvent) => cache.receive({ type: "event", session_id: "root", epoch: "one", cursor: ++cursor, record });
  try {
    await ui.render(cache.get("root").entries);
    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    Object.defineProperty(feed, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(feed, "scrollHeight", { configurable: true, value: 600 });
    feed.scrollTop = 180;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));

    receive({ event: "run_started", run_id: "run", message_id: "current-id" });
    receive({ event: "tool_call", run_id: "run", history_id: "call-one", call_id: "call-one", name: "lookup", arguments: { round: 1 } });
    receive({ event: "tool_result", run_id: "run", history_id: "result-one", call_id: "call-one", result: "first result" });
    receive({ event: "text_done", run_id: "run", history_id: "answer-one", text: answer });
    receive({ event: "tool_call", run_id: "run", history_id: "call-two", call_id: "call-two", name: "lookup", arguments: { round: 2 } });
    receive({ event: "tool_result", run_id: "run", history_id: "result-two", call_id: "call-two", result: "second result" });
    receive({ event: "text_chunk", run_id: "run", chunk: "The same " });
    receive({ event: "text_chunk", run_id: "run", chunk: "answer" });
    await ui.render(cache.get("root").entries);

    const assistants = () => [...ui.container.querySelectorAll<HTMLElement>("article.entry.assistant")];
    const final = assistants().at(-1)!;
    const key = final.dataset.recordKey;
    assert.ok(key);
    assert.equal(final.dataset.runId, "run");
    assert.equal(final.querySelector(".entry-content")?.textContent?.startsWith("The same answer"), true);
    assert.equal(assistants().length, 3, "The prior identical answer and first same-turn response remain distinct");
    const visibleLabels = () => assistants().map(element => element.querySelector(".entry-label:not([hidden])")?.textContent?.trim()).filter(Boolean);
    assert.deepEqual(visibleLabels(), ["Assistant", "Assistant"], "The current run has one byline across both assistant blocks");
    const position = [...ui.container.querySelectorAll("article.entry")].indexOf(final);
    const tool = ui.container.querySelector<HTMLDetailsElement>(".tool-record")!;
    await act(async () => { tool.open = true; tool.dispatchEvent(new ui.dom.window.Event("toggle")); });
    feed.scrollTop = 180;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));

    receive({ event: "text_done", run_id: "run", history_id: "answer-two", text: answer });
    receive({ event: "run_finished", run_id: "run", status: "completed" });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), final);
    assert.equal(final.querySelector(".entry-content")?.textContent?.startsWith("The same answer"), true);
    assert.deepEqual(visibleLabels(), ["Assistant", "Assistant"]);
    assert.equal([...ui.container.querySelectorAll("article.entry")].indexOf(final), position);
    assert.equal(tool.open, true);
    assert.equal(feed.scrollTop, 180);

    // The HTTP history read is not cursor-versioned and can return the older
    // baseline while the committed response is still being indexed.
    cache.set("root", applySnapshot(cache.get("root"), baseline));
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), final);
    assert.equal(tool.open, true);
    assert.equal(feed.scrollTop, 180);

    const saved = snapshot([
      ...baseline.history,
      row("call-one", "assistant", "", { tool_calls: [{ id: "call-one", name: "lookup", arguments: { round: 1 } }] }),
      row("result-one", "tool", "first result", { tool_call_id: "call-one", name: "lookup" }),
      row("answer-one", "assistant", answer),
      row("call-two", "assistant", "", { tool_calls: [{ id: "call-two", name: "lookup", arguments: { round: 2 } }] }),
      row("result-two", "tool", "second result", { tool_call_id: "call-two", name: "lookup" }),
      row("answer-two", "assistant", answer),
    ]);
    cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: ++cursor, snapshot: saved });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), final,
      "Normal responses reconcile by turn order and saved row IDs, not equal prose");
    assert.equal(assistants().length, 3, "The prior answer and two same-turn responses remain separate");
    assert.deepEqual(visibleLabels(), ["Assistant", "Assistant"]);
    assert.equal([...ui.container.querySelectorAll("article.entry")].indexOf(final), position);
    assert.equal(tool.open, true);
    assert.equal(feed.scrollTop, 180);
  } finally { await ui.close(); }
});
