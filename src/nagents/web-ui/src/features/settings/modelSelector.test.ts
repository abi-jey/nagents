import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ModelSelector } from "./ModelSelector.js";

test("global model catalog selection changes only the model draft", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const requested: string[] = [];
  const chosen: string[] = [];
  t.mock.method(globalThis, "fetch", async (path: string) => {
    requested.push(path);
    if (path === "/api/provider-scopes/global/providers") return Response.json({ active: "codex" });
    if (path === "/api/providers/codex/models") return Response.json({ models: ["gpt-new"] });
    throw new Error(`Unexpected request: ${path}`);
  });
  try {
    await act(async () => root.render(createElement(ModelSelector, {
      token: "token", scope: "global", model: "gpt-old", disabled: false,
      update: (model: string) => chosen.push(model),
    })));
    await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
    const catalog = container.querySelector<HTMLSelectElement>("select")!;
    await act(async () => { catalog.value = "gpt-new"; catalog.dispatchEvent(new dom.window.Event("change", { bubbles: true })); });
    assert.deepEqual(chosen, ["gpt-new"]);
    assert.deepEqual(requested, ["/api/provider-scopes/global/providers", "/api/providers/codex/models"]);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
