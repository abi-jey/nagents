import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { AgentNavigator } from "./navigator/AgentNavigator.js";
import { appearancePresets, appearanceStorageKey } from "./appearance.js";

test("navigator Appearance works before visiting a scope and retains local presets after reopen", async t => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const root = createRoot(dom.window.document.getElementById("root")!);
  const requests: { path: string; method: string }[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    requests.push({ path, method: init.method || "GET" });
    throw new Error("Browser-local appearance must not need a server request");
  });
  const render = (key: string) => act(async () => root.render(createElement(AgentNavigator, {
    key, token: "token", close() {}, changed() {},
  })));
  const openAppearance = async () => {
    const dialog = dom.window.document.querySelector<HTMLDialogElement>("dialog.agent-navigator-dialog")!;
    dialog.showModal();
    const button = [...dialog.querySelectorAll<HTMLButtonElement>(".an-appearance-link button")]
      .find(item => item.textContent?.includes("Appearance"))!;
    assert.ok(button, "Appearance has a dedicated navigator item");
    await act(async () => button.click());
    return dialog;
  };
  try {
    await render("first-open");
    let dialog = await openAppearance();
    assert.equal(dialog.querySelectorAll("[role='tabpanel']").length, 0,
      "Appearance is available before any global, workspace, or agent section is visited");
    const panel = dialog.querySelector<HTMLElement>('[aria-label="Chat appearance"]')!;
    assert.ok(panel);
    const quiet = [...panel.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Quiet")!;
    await act(async () => quiet.click());
    assert.equal(quiet.getAttribute("aria-pressed"), "true");
    assert.equal(dom.window.localStorage.getItem(appearanceStorageKey), JSON.stringify(appearancePresets.find(preset => preset.id === "quiet")!.value));
    assert.deepEqual(requests, [], "Changing a browser preference emits no scoped API writes");

    await render("reopened");
    dialog = await openAppearance();
    const reopened = dialog.querySelector<HTMLElement>('[aria-label="Chat appearance"]')!;
    const reopenedQuiet = [...reopened.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "Quiet")!;
    assert.equal(reopenedQuiet.getAttribute("aria-pressed"), "true");
    assert.equal(reopened.querySelector(".appearance-current strong")?.textContent, "Quiet");
    assert.deepEqual(requests, [], "Reopening the navigator keeps appearance local");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"])
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
  }
});
