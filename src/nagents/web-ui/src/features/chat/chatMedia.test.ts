import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ChatMedia } from "./ChatMedia.js";

test("inline images open accessibly at full size and offer download without preview housekeeping", async t => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true, IntersectionObserver: undefined });
  t.mock.method(dom.window.HTMLDialogElement.prototype, "showModal", function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
  t.mock.method(dom.window.HTMLDialogElement.prototype, "close", function (this: HTMLDialogElement) { this.removeAttribute("open"); });
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Type": "image/png" } }));
  t.mock.method(URL, "createObjectURL", () => "blob:landscape");
  const revoke = t.mock.method(URL, "revokeObjectURL", () => {});
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(ChatMedia, { token: "private-token", path: "sessions/chat/uploads/image/preview", asset: {
      filename: "Landscape.png", media_type: "image/png", byte_length: 3,
    } })));
    const image = container.querySelector("img")!;
    Object.defineProperties(image, { naturalWidth: { value: 1200 }, naturalHeight: { value: 800 } });
    await act(async () => image.dispatchEvent(new dom.window.Event("load")));
    assert.match(container.querySelector("figcaption")!.textContent!, /Landscape.png.*1200 × 800.*Download/);
    assert.doesNotMatch(container.textContent!, /preview|unload|Explicit delivery|builtin\.web/i);
    const link = container.querySelector("a[download]")!;
    assert.equal(link.getAttribute("href"), "blob:landscape");
    assert.equal(link.getAttribute("download"), "Landscape.png");
    const open = container.querySelector<HTMLButtonElement>('button[aria-label="Open image Landscape.png"]')!;
    open.focus();
    await act(async () => open.click());
    const viewer = container.querySelector("dialog")!;
    assert.ok(viewer.open);
    assert.equal(viewer.querySelector("img")?.getAttribute("src"), "blob:landscape");
    assert.equal(viewer.querySelector("h2")?.textContent, "Landscape.png");
    assert.equal(viewer.querySelector("a[download]")?.getAttribute("aria-label"), "Download Landscape.png", "The icon-only mobile link keeps its accessible name");
    assert.equal(fetch.mock.callCount(), 1, "Enlarging reuses the existing image bytes");
    await act(async () => viewer.dispatchEvent(new dom.window.Event("cancel", { cancelable: true })));
    assert.equal(container.querySelector("dialog"), null);
    assert.equal(dom.window.document.activeElement, open);
    assert.equal(revoke.mock.callCount(), 0, "Closing the viewer keeps the inline image available");
  } finally {
    await act(async () => root.unmount());
    assert.equal(revoke.mock.callCount(), 1);
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT", "IntersectionObserver"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
