import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import {
  appearancePresets, appearanceStorageKey, defaultAppearance, parseAppearance, saveAppearance,
  selectedAppearancePreset, useAppearance,
} from "./appearance.js";

function fixture() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  function Probe() {
    const appearance = useAppearance();
    return createElement("output", { "data-value": JSON.stringify(appearance) }, JSON.stringify(appearance));
  }
  return {
    dom, container,
    async mount() { await act(async () => root.render(createElement(Probe))); },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"])
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    },
  };
}

const preset = (id: string) => appearancePresets.find(item => item.id === id)!.value;

test("the default appearance is the Compact preset", () => {
  assert.deepEqual(defaultAppearance, preset("compact"));
  assert.equal(selectedAppearancePreset(defaultAppearance)?.id, "compact");
  assert.deepEqual(parseAppearance(null), defaultAppearance);
});

test("malformed and oversized storage values fall back to Compact", () => {
  for (const raw of ["{", JSON.stringify({ version: 2, activity: {} }), " ".repeat(16385)]) {
    assert.deepEqual(parseAppearance(raw), defaultAppearance);
  }
  const arrays = parseAppearance(JSON.stringify({ version: 1, runUsage: ["expanded"], activity: {
    shell: { placement: ["separate"], details: ["expanded"] },
  } }));
  assert.deepEqual(arrays.activity.shell, defaultAppearance.activity.shell);
  assert.equal(arrays.runUsage, defaultAppearance.runUsage);
});

test("partially invalid appearance settings preserve valid categories and default invalid fields", () => {
  const parsed = parseAppearance(JSON.stringify({ version: 1, runUsage: "invalid", activity: {
    shell: { placement: "separate", details: "expanded" },
    files: { placement: "hidden", details: "invalid" },
    tools: { placement: "invalid", details: "expanded" },
    reasoning: null,
  } }));
  assert.deepEqual(parsed.activity.shell, { placement: "separate", details: "expanded" });
  assert.deepEqual(parsed.activity.files, defaultAppearance.activity.files);
  assert.deepEqual(parsed.activity.tools, defaultAppearance.activity.tools);
  assert.deepEqual(parsed.activity.reasoning, defaultAppearance.activity.reasoning);
  assert.equal(parsed.runUsage, defaultAppearance.runUsage);
  assert.equal(selectedAppearancePreset(parsed), undefined);
});

test("local saves persist and notify this tab; browser storage events refresh other-tab changes", async () => {
  const ui = fixture();
  try {
    ui.dom.window.localStorage.setItem(appearanceStorageKey, JSON.stringify(preset("compact")));
    await ui.mount();
    const output = () => ui.container.querySelector("output")!;
    assert.equal(output().textContent, JSON.stringify(preset("compact")));

    let saved = false;
    await act(async () => { saved = saveAppearance(preset("detailed")); });
    assert.equal(saved, true);
    assert.equal(ui.dom.window.localStorage.getItem(appearanceStorageKey), JSON.stringify(preset("detailed")));
    assert.equal(output().textContent, JSON.stringify(preset("detailed")));

    ui.dom.window.localStorage.setItem(appearanceStorageKey, JSON.stringify(preset("quiet")));
    await act(async () => ui.dom.window.dispatchEvent(new ui.dom.window.StorageEvent("storage", {
      key: appearanceStorageKey, newValue: JSON.stringify(preset("quiet")), oldValue: JSON.stringify(preset("detailed")),
    })));
    assert.equal(output().textContent, JSON.stringify(preset("quiet")));
  } finally { await ui.close(); }
});

test("a blocked localStorage write still updates the current tab from its cached in-memory value", async t => {
  const ui = fixture();
  try {
    ui.dom.window.localStorage.setItem(appearanceStorageKey, JSON.stringify(preset("compact")));
    await ui.mount();
    t.mock.method(ui.dom.window.Storage.prototype, "setItem", () => { throw new Error("storage denied"); });
    let saved = true;
    await act(async () => { saved = saveAppearance(preset("everything")); });
    assert.equal(saved, false);
    assert.equal(ui.dom.window.localStorage.getItem(appearanceStorageKey), JSON.stringify(preset("compact")));
    assert.equal(ui.container.querySelector("output")!.textContent, JSON.stringify(preset("everything")));
  } finally { await ui.close(); }
});

test("when localStorage reads are blocked, saving still updates this tab through memory", async t => {
  const ui = fixture();
  try {
    t.mock.method(ui.dom.window.Storage.prototype, "getItem", () => { throw new Error("storage denied"); });
    await ui.mount();
    assert.equal(ui.container.querySelector("output")!.textContent, JSON.stringify(defaultAppearance));
    let saved = false;
    await act(async () => { saved = saveAppearance(preset("quiet")); });
    assert.equal(saved, true);
    assert.equal(ui.container.querySelector("output")!.textContent, JSON.stringify(preset("quiet")));
  } finally { await ui.close(); }
});
