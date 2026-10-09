import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { scrollWithin } from "../components/scrollWithin.js";

test("record navigation scrolls only its transcript, with bounds and visibility", () => {
  const dom = new JSDOM("<main><section><article></article></section><aside></aside></main>");
  const feed = dom.window.document.querySelector("section")!;
  const target = feed.querySelector("article")!;
  const calls: ScrollToOptions[] = [];
  feed.scrollTo = (options) => calls.push(options as ScrollToOptions);
  Object.defineProperties(feed, {
    clientHeight: { value: 300 }, scrollHeight: { value: 1500 }, clientTop: { value: 1 },
  });
  feed.getBoundingClientRect = () => new dom.window.DOMRect(0, 50, 400, 302);
  feed.scrollTop = 200;
  target.getBoundingClientRect = () => new dom.window.DOMRect(0, 501, 300, 100);
  scrollWithin(feed, target, "nearest", "smooth");
  assert.deepEqual(calls.pop(), { top: 450, behavior: "smooth" });
  scrollWithin(feed, target, "start");
  assert.deepEqual(calls.pop(), { top: 650, behavior: "auto" });
  target.getBoundingClientRect = () => new dom.window.DOMRect(0, 101, 300, 100);
  scrollWithin(feed, target);
  assert.equal(calls.length, 0, "Already visible records do not move the reader");
  target.getBoundingClientRect = () => new dom.window.DOMRect(0, -1000, 300, 100);
  scrollWithin(feed, target);
  assert.equal(calls.pop()?.top, 0);
  target.getBoundingClientRect = () => new dom.window.DOMRect(0, 3000, 300, 100);
  scrollWithin(feed, target);
  assert.equal(calls.pop()?.top, 1200);
  scrollWithin(feed, dom.window.document.querySelector("aside")!);
  assert.equal(calls.length, 0, "Unrelated elements cannot move the transcript");
  dom.window.close();
});
