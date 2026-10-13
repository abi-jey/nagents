import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ReasoningEffort } from "./ReasoningEffort.js";

test("effort choices follow model and scope, ignore stale discovery, and preserve explicit drafts", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const requests: { url: URL; signal: AbortSignal; resolve: (response: Response) => void }[] = [];
  t.mock.method(globalThis, "fetch", (path: string, init: RequestInit) => new Promise<Response>(resolve => {
    requests.push({ url: new URL(path, "http://localhost"), signal: init.signal!, resolve });
  }));
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const chosen: string[] = [];
  const view = (model: string) => createElement(ReasoningEffort, {
    token: "token", scope: "global", provider: "codex", model, value: "high", update: (value: string) => chosen.push(value),
  });
  try {
    await act(async () => root.render(view("old-model")));
    await act(async () => { t.mock.timers.tick(200); });
    await act(async () => root.render(view("new-model")));
    assert.equal(requests[0].signal.aborted, true);
    await act(async () => { t.mock.timers.tick(200); });
    assert.equal(requests[1].url.pathname, "/api/reasoning-options");
    assert.equal(requests[1].url.searchParams.get("scope"), "global");
    assert.equal(requests[1].url.searchParams.get("provider"), "codex");
    assert.equal(requests[1].url.searchParams.get("model"), "new-model");
    await act(async () => { requests[1].resolve(Response.json({ options: ["low", "high"] })); });
    const select = container.querySelector("select")!;
    assert.deepEqual([...select.options].map(option => option.value), ["", "low", "high"]);
    assert.equal(select.value, "high");
    await act(async () => { requests[0].resolve(Response.json({ options: ["none"] })); });
    assert.deepEqual([...select.options].map(option => option.value), ["", "low", "high"]);
    assert.deepEqual(chosen, [], "Discovery must never rewrite a user's effort preference");
    await act(async () => root.render(view("unsupported-model")));
    await act(async () => { t.mock.timers.tick(200); requests[2].resolve(Response.json({ options: [] })); });
    assert.equal(select.value, "high", "Keep unsupported saved choices visible until the user changes them");
    assert.equal(select.getAttribute("aria-invalid"), "true");
    await act(async () => { select.value = ""; select.dispatchEvent(new dom.window.Event("change", { bubbles: true })); });
    assert.deepEqual(chosen, [""]);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    }
  }
});
