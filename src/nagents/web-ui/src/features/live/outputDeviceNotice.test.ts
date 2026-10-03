import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { OutputDeviceNotice } from "./OutputDeviceNotice.js";

function fixture() {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const media = new EventTarget();
  let inventory: { kind: string; deviceId: string }[] = [], calls = 0;
  let read = async () => inventory;
  Object.assign(media, { enumerateDevices: () => { calls++; return read(); } });
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, navigator: { mediaDevices: media }, IS_REACT_ACT_ENVIRONMENT: true }))
    Object.defineProperty(globalThis, key, { configurable: true, value, writable: true });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  let closed = false;
  return { dom, container, media,
    calls: () => calls,
    inventory: (ids: string[]) => { inventory = ids.map(deviceId => ({ kind: "audiooutput", deviceId })); },
    read: (callback: typeof read) => { read = callback; },
    change: () => act(async () => media.dispatchEvent(new Event("devicechange"))),
    render: (props: Partial<Parameters<typeof OutputDeviceNotice>[0]> = {}) => act(async () => root.render(createElement(OutputDeviceNotice, {
      connected: true, outputId: "headphones", hidden: false, useDefault: async () => assert.fail("Recovery requires a click"), ...props,
    }))),
    async close() {
      if (closed) return;
      closed = true; await act(async () => root.unmount()); dom.window.close();
      for (const name of ["window", "document", "navigator", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
      }
    },
  };
}

test("only a positively observed selected speaker disappearing warns, with no automatic reroute", async () => {
  const f = fixture();
  try {
    await f.render(); await f.change();
    assert.equal(f.container.textContent, "", "An initially filtered inventory does not prove removal");
    f.inventory(["headphones"]); await f.change();
    f.read(async () => { throw new Error("Permission expired"); }); await f.change();
    assert.equal(f.container.textContent, "", "An enumeration error does not prove removal");
    f.read(async () => []); await f.change();
    assert.match(f.container.querySelector('[role="status"]')?.textContent || "", /speaker is no longer available/);
    assert.equal(f.container.querySelector("button")?.textContent, "Use system default");
    f.read(async () => [{ kind: "audiooutput", deviceId: "headphones" }]); await f.change();
    assert.equal(f.container.textContent, "", "Reconnection clears the warning without changing routing");
  } finally { await f.close(); }
});

test("default and disconnected voice do not monitor outputs; hidden Audio still monitors an established route", async () => {
  const f = fixture();
  try {
    await f.render({ outputId: "" }); await f.change();
    await f.render({ connected: false }); await f.change();
    assert.equal(f.calls(), 0);
    f.inventory(["headphones"]); await f.render({ hidden: true });
    f.inventory([]); await f.change(); assert.equal(f.container.textContent, "");
    await f.render({ hidden: false });
    assert.match(f.container.textContent || "", /speaker is no longer available/);
  } finally { await f.close(); }
});

test("output recovery is explicit, retryable and leaves the call running when routing fails", async () => {
  const f = fixture();
  let attempts = 0, finish!: () => void;
  const useDefault = async () => { attempts++; if (attempts === 1) throw new Error("Speaker permission was denied."); await new Promise<void>(resolve => { finish = resolve; }); };
  try {
    f.inventory(["headphones"]); await f.render({ useDefault });
    f.inventory([]); await f.change();
    await act(async () => f.container.querySelector("button")!.click());
    assert.equal(attempts, 1); assert.match(f.container.querySelector('[role="alert"]')?.textContent || "", /permission was denied/);
    await act(async () => f.container.querySelector("button")!.click());
    assert.equal(attempts, 2); assert.equal(f.container.querySelector("button")!.disabled, true);
    await act(async () => finish());
    await f.render({ outputId: "", useDefault });
    assert.equal(f.container.textContent, "");
  } finally { await f.close(); }
});

test("stale device lists cannot warn about a changed route and late callbacks are inert after unmount", async () => {
  const f = fixture();
  let resolve!: (devices: { kind: string; deviceId: string }[]) => void;
  try {
    f.inventory(["headphones"]); await f.render();
    f.read(() => new Promise(done => { resolve = done; })); await f.change();
    await f.render({ outputId: "" });
    await act(async () => resolve([])); assert.equal(f.container.textContent, "");
    f.read(async () => [{ kind: "audiooutput", deviceId: "headphones" }]); await f.render();
    const count = f.calls(); await f.close();
    f.media.dispatchEvent(new Event("devicechange")); assert.equal(f.calls(), count);
  } finally { await f.close(); }
});
