import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { Conversation } from "./Conversation.js";
import type { Entry } from "./transcript.js";

async function fixture(observe = true) {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const observed = new Set<Element>();
  let notify = () => {}, disconnected = false;
  if (observe) Object.defineProperty(dom.window, "ResizeObserver", { value: class {
    constructor(callback: () => void) { notify = callback; }
    observe(element: Element) { observed.add(element); }
    disconnect() { disconnected = true; }
  } });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const entries: Entry[] = [
    { id: "earlier", kind: "assistant", text: "Earlier discussion" },
    { id: "reading", kind: "assistant", text: "The message being read" },
    { id: "tool", kind: "tool", title: "shell", text: "Actual tool output", state: "Completed" },
  ];
  await act(async () => root.render(createElement(Conversation, {
    entries, sessionId: "resize", demo: false, canSubmit: false, submit: () => assert.fail("Unexpected submit"),
  })));
  const feed = container.querySelector<HTMLElement>(".conversation")!;
  const inner = container.querySelector<HTMLElement>(".conversation-inner")!;
  const geometry = { width: 700, height: 300, contentHeight: 1000, firstHeight: 400 };
  let top = 0, writes = 0;
  Object.defineProperties(feed, {
    clientWidth: { get: () => geometry.width }, clientHeight: { get: () => geometry.height },
    scrollHeight: { get: () => geometry.contentHeight },
    scrollTop: { get: () => top, set: (value: number) => { writes++; top = Math.max(0, Math.min(value, geometry.contentHeight - geometry.height)); } },
  });
  feed.getBoundingClientRect = () => new dom.window.DOMRect(0, 0, geometry.width, geometry.height);
  const articles = [...container.querySelectorAll<HTMLElement>("article")];
  articles[0].getBoundingClientRect = () => new dom.window.DOMRect(0, -top, geometry.width, geometry.firstHeight);
  articles[1].getBoundingClientRect = () => new dom.window.DOMRect(0, geometry.firstHeight - top, geometry.width, 400);
  articles[2].getBoundingClientRect = () => new dom.window.DOMRect(0, geometry.firstHeight + 400 - top, geometry.width, 200);
  const summary = articles[2].querySelector("summary")!;
  summary.getBoundingClientRect = () => new dom.window.DOMRect(0, geometry.firstHeight + 400 - top, geometry.width, 44);
  const scroll = async (position: number) => act(async () => {
    feed.scrollTop = position;
    feed.dispatchEvent(new dom.window.Event("scroll"));
  });
  return {
    dom, feed, inner, summary, geometry, observed, scroll,
    resize: () => act(async () => notify()),
    windowResize: () => act(async () => dom.window.dispatchEvent(new dom.window.Event("resize"))),
    get writes() { return writes; },
    get disconnected() { return disconnected; },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[name]) Object.defineProperty(globalThis, name, previous[name]);
        else Reflect.deleteProperty(globalThis, name);
      }
    },
  };
}

test("opening and closing inline composer panels keeps bottom followers at the latest message without new entries", async () => {
  const f = await fixture();
  try {
    assert.deepEqual(f.observed, new Set([f.feed, f.inner]));
    await f.scroll(700);
    f.geometry.height = 140;
    // Browsers may dispatch an incidental scroll before the resize observer.
    await f.scroll(700);
    await f.resize();
    assert.equal(f.feed.scrollTop, 860);
    f.geometry.height = 440;
    await f.scroll(860); // The browser clamps the former offset to the new end.
    await f.resize();
    assert.equal(f.feed.scrollTop, 560);
    f.geometry.contentHeight += 120;
    await f.resize();
    assert.equal(f.feed.scrollTop, 680, "Late content sizing also follows the latest message");
  } finally { await f.close(); }
});

test("readers keep their visible record through panel resize and transcript width reflow", async () => {
  const f = await fixture();
  try {
    await f.scroll(450);
    f.geometry.height = 160;
    await f.resize();
    assert.equal(f.feed.scrollTop, 450);
    f.geometry.width = 320;
    f.geometry.firstHeight += 120;
    f.geometry.contentHeight += 120;
    await f.resize();
    assert.equal(f.feed.scrollTop, 570, "The same text retains its offset after content above it reflows");
    f.geometry.height = 340;
    await f.windowResize();
    assert.equal(f.feed.scrollTop, 570, "Restoring space must not pull a reader to the end");
  } finally { await f.close(); }
});

test("resizing preserves focused tool inspection near the end until following is explicitly resumed", async () => {
  const f = await fixture();
  try {
    await f.scroll(700);
    await act(async () => f.summary.focus());
    f.geometry.height = 160;
    await f.resize();
    assert.equal(f.feed.scrollTop, 700);
    assert.equal(f.dom.window.document.activeElement, f.summary);
    await act(async () => f.feed.focus());
    await f.scroll(840);
    f.geometry.height = 100;
    await f.resize();
    assert.equal(f.feed.scrollTop, 900);
  } finally { await f.close(); }
});

test("resize observation is disposed and older browsers still follow window resize", async () => {
  const f = await fixture();
  await f.scroll(700);
  await f.close();
  assert.equal(f.disconnected, true);
  const writes = f.writes;
  await f.resize();
  assert.equal(f.writes, writes, "An already queued observer callback is inert after unmount");
  const fallback = await fixture(false);
  try {
    await fallback.scroll(700);
    fallback.geometry.height = 200;
    await fallback.windowResize();
    assert.equal(fallback.feed.scrollTop, 800);
  } finally { await fallback.close(); }
});
