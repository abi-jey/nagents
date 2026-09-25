import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { loadAsset, MediaBudget, type LocalDelivery } from "./deliveries.js";
import { LocalDeliveryCard, MediaToken } from "./LocalDelivery.js";
import { appendEvent, fromHistory } from "./transcript.js";
import { applySnapshot } from "./liveTranscript.js";
import type { Snapshot } from "../../types.js";

const delivery: LocalDelivery = {
  delivery_id: "delivery-one", session_id: "root", channel: "builtin.web", text: "same text", sequence: 1,
  anchor_message_id: "2", call_position: 0, earlier: false,
  assets: [{ asset_id: "asset-one", filename: "image.png", media_type: "image/png", byte_length: 3, position: 0 }],
};

test("authenticated streaming fetch bounds bytes before Blob and releases retained reservations exactly once", async (t) => {
  const budget = new MediaBudget(3, 1);
  let requested = "", headers: HeadersInit | undefined;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    requested = path; headers = init.headers;
    return new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Type": "image/png" } });
  });
  const create = t.mock.method(URL, "createObjectURL", (blob: Blob) => { assert.equal(blob.size, 3); return "blob:owned"; });
  const revoke = t.mock.method(URL, "revokeObjectURL", () => {});
  const loaded = await loadAsset("secret-token", delivery, delivery.assets[0], new AbortController().signal, budget);
  assert.equal(requested, "/api/sessions/root/deliveries/delivery-one/assets/asset-one");
  assert.equal(new Headers(headers).get("X-Ngn-Token"), "secret-token");
  assert.doesNotMatch(requested, /secret-token|\?/);
  assert.throws(() => budget.reserve(1), /budget is full/);
  loaded.release(); loaded.release();
  assert.equal(create.mock.callCount(), 1); assert.equal(revoke.mock.callCount(), 1);
  budget.reserve(3).release();
});

for (const kind of ["overflow", "incomplete", "wrong-type", "cancelled"] as const) {
  test(`streaming ${kind} creates no object URL and frees the budget`, async (t) => {
    const budget = new MediaBudget(3, 1);
    const controller = new AbortController();
    const create = t.mock.method(URL, "createObjectURL", () => assert.fail("No oversized/partial Blob"));
    t.mock.method(globalThis, "fetch", async () => {
      if (kind === "cancelled") controller.abort();
      const body = new ReadableStream<Uint8Array>({ start(stream) {
        stream.enqueue(new Uint8Array(kind === "overflow" ? 4 : kind === "incomplete" ? 2 : 3)); stream.close();
      } });
      return new Response(body, { headers: { "Content-Type": kind === "wrong-type" ? "image/svg+xml" : "image/png" } });
    });
    await assert.rejects(loadAsset("token", delivery, delivery.assets[0], controller.signal, budget));
    assert.equal(create.mock.callCount(), 0);
    budget.reserve(3).release();
  });
}

test("concurrent requests and many small retained previews share finite budgets", () => {
  const budget = new MediaBudget(5, 2);
  const first = budget.reserve(1), second = budget.reserve(1);
  assert.throws(() => budget.reserve(1), /budget is full/);
  first.loaded(); second.loaded();
  const third = budget.reserve(3); third.loaded();
  assert.throws(() => budget.reserve(1), /budget is full/);
  for (const item of [first, second, third]) item.release();
  budget.reserve(5).release();
});

test("exact saved anchors and delivery IDs survive retry, identical text, replay and compaction", () => {
  const base: Snapshot = { session_id: "root", retained_tasks: [], sessions: [], history: [
    { history_id: "1", role: "user", content: "send", name: "", tool_call_id: "", tool_calls: [] },
    { history_id: "2", role: "assistant", content: "", name: "", tool_call_id: "", tool_calls: [
      { id: "reused", name: "channel_send", arguments: {} },
      { id: "reused", name: "channel_send", arguments: {} },
    ], deliveries: [delivery, { ...delivery, delivery_id: "delivery-two", sequence: 2, call_position: 1 }] },
  ] };
  let entries = appendEvent([], { event: "user_message", text: "send", history_id: "1", run_id: "run" });
  entries = appendEvent(entries, { event: "tool_call", id: "reused", name: "channel_send", run_id: "run", transcript_event_id: "abandoned" });
  entries = appendEvent(entries, { event: "transcript_abandoned", run_id: "run", calls: [{ event_id: "abandoned", call_position: 0 }] });
  for (let position = 0; position < 2; position++) {
    entries = appendEvent(entries, { event: "tool_call", id: "reused", name: "channel_send", run_id: "run", transcript_event_id: String(position) });
  }
  entries = appendEvent(entries, { event: "transcript_anchor", run_id: "run", history_id: "2",
    calls: [{ event_id: "0", call_position: 0 }, { event_id: "1", call_position: 1 }] });
  const first = applySnapshot({ entries }, base);
  const second = applySnapshot(first, base);
  assert.deepEqual(second.entries.filter((entry) => entry.delivery).map((entry) => entry.id), ["delivery:delivery-one", "delivery:delivery-two"]);
  for (const current of [first, second]) {
    const pending = current.entries.filter((entry) => entry.kind === "tool");
    assert.equal(pending.filter((entry) => entry.abandoned).length, 1);
    assert.equal(pending.filter((entry) => entry.historyId === "2").length, 2);
    for (const [index, entry] of current.entries.entries()) if (entry.delivery)
      assert.equal(current.entries[index - 1].callPosition, entry.delivery.call_position);
  }
  const cold = fromHistory(base);
  assert.deepEqual(cold.filter((entry) => entry.delivery).map((entry) => entry.id), ["delivery:delivery-one", "delivery:delivery-two"]);
  const compacted = applySnapshot(second, { ...base, history: [{ role: "local_delivery", content: "", name: "", tool_call_id: "", tool_calls: [],
    deliveries: [delivery, { ...delivery, delivery_id: "delivery-two", sequence: 2 }].map((item) => ({ ...item, earlier: true })),
  }] });
  assert.equal(compacted.entries.filter((entry) => entry.delivery).length, 2);
  assert.ok(compacted.entries.filter((entry) => entry.delivery).every((entry) => entry.delivery?.earlier));
});

test("snapshot recovery of a missed result replaces pending tool status without inventing live success", () => {
  let entries = appendEvent([], { event: "tool_call", id: "send", name: "channel_send", run_id: "run", transcript_event_id: "1" });
  entries = appendEvent(entries, { event: "transcript_anchor", run_id: "run", history_id: "2", calls: [{ event_id: "1", call_position: 0 }] });
  entries = appendEvent(entries, { event: "run_finished", run_id: "run", status: "unknown" });
  assert.equal(entries[0].state, "No result recorded");
  const snapshot: Snapshot = { session_id: "root", retained_tasks: [], sessions: [], history: [
    { history_id: "2", role: "assistant", content: "", name: "", tool_call_id: "", tool_calls: [{ id: "send", name: "channel_send", arguments: {} }], deliveries: [delivery] },
    { history_id: "3", role: "tool", content: "saved receipt", name: "channel_send", tool_call_id: "send", tool_calls: [] },
  ] };
  const recovered = applySnapshot({ entries }, snapshot);
  assert.equal(recovered.entries[0].state, "Recorded result");
  assert.equal(recovered.entries[0].result, "saved receipt");
  assert.equal(recovered.entries[0].id, entries[0].id);
});

for (const [type, tag] of [["image/png", "img"], ["audio/wav", "audio"], ["video/mp4", "video"]]) {
  test(`mounted ${tag} loads on demand, preserves its URL on replay, offers download and revokes on navigation`, async (t) => {
    const dom = new JSDOM("<div id='root'></div>");
    const descriptors = Object.getOwnPropertyDescriptors(globalThis);
    Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
    const container = dom.window.document.getElementById("root")!;
    const root = createRoot(container);
    const create = t.mock.method(URL, "createObjectURL", () => "blob:preview");
    const revoke = t.mock.method(URL, "revokeObjectURL", () => {});
    const fetch = t.mock.method(globalThis, "fetch", async () => new Response(new Uint8Array(3), { headers: { "Content-Type": type } }));
    const value = { ...delivery, assets: [{ ...delivery.assets[0], media_type: type }] };
    async function render(current: LocalDelivery) {
      await act(async () => root.render(createElement(MediaToken.Provider, { value: "token" }, createElement(LocalDeliveryCard, { delivery: current }))));
    }
    try {
      await render(value);
      assert.equal(fetch.mock.callCount(), 0);
      await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
      const player = container.querySelector(tag)!;
      assert.equal(player.getAttribute("src"), "blob:preview");
      assert.equal(player.hasAttribute("autoplay"), false);
      if (tag !== "img") assert.equal(player.hasAttribute("controls"), true);
      await render({ ...value });
      assert.equal(container.querySelector(tag), player);
      assert.equal(create.mock.callCount(), 1);
      await act(async () => player.dispatchEvent(new dom.window.Event("error")));
      assert.match(container.textContent!, /could not decode/);
      assert.equal(container.querySelector("a[download]")?.getAttribute("href"), "blob:preview");
      await render({ ...value, session_id: "different-root" });
      assert.equal(revoke.mock.callCount(), 1);
      assert.equal(container.querySelector(tag), null);
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
        else Reflect.deleteProperty(globalThis, key);
      }
    }
  });
}
