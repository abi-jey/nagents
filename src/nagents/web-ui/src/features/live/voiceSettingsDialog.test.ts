import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { VoiceSettingsDialog } from "./VoiceSettingsDialog.js";

function view() {
  const dom = new JSDOM("<form id='chat'><button type='button' id='trigger'>Voice settings</button><div id='root'></div></form>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  // React is imported before this isolated DOM and selects its legacy input shim.
  Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  dom.window.HTMLElement.prototype.getClientRects = function () {
    return (this.closest("[hidden]") ? [] : [{ width: 100, height: 40 }]) as unknown as DOMRectList;
  };
  const root = createRoot(dom.window.document.getElementById("root")!);
  const trigger = dom.window.document.getElementById("trigger")!;
  trigger.focus();
  let dismissals = 0;
  const children = createElement("section", {},
    createElement("h3", { id: "live-settings-title", tabIndex: -1 }, "Voice settings"),
    createElement("p", { id: "live-settings-description" }, "Audio devices and voice preferences"),
    createElement("form", { className: "live-settings-body" },
      createElement("select", { "aria-label": "Microphone", id: "first" }, createElement("option", {}, "System default")),
      createElement("textarea", { "aria-label": "Voice instructions" }),
      createElement("button", { type: "button", id: "last" }, "Save settings")));
  return {
    dom, trigger,
    count: () => dismissals,
    dialog: () => dom.window.document.querySelector("dialog")!,
    render: async (open = true, saving = false) => act(async () => root.render(createElement(VoiceSettingsDialog, {
      open, saving, close: () => { dismissals++; }, children,
    }))),
    async cancel() {
      const event = new dom.window.Event("cancel", { cancelable: true });
      await act(async () => dom.window.document.querySelector("dialog")!.dispatchEvent(event));
      assert.equal(event.defaultPrevented, true);
    },
    async pointer(type: string, x: number, y: number) {
      const element = dom.window.document.querySelector("dialog")!;
      element.getBoundingClientRect = () => ({ left: 100, top: 100, right: 600, bottom: 700 }) as DOMRect;
      await act(async () => element.dispatchEvent(new dom.window.MouseEvent(type, { bubbles: true, clientX: x, clientY: y })));
    },
    async close() {
      await act(async () => root.unmount()); dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("settings uses a labelled top-layer dialog outside the chat form and restores focus/scroll", async () => {
  const ui = view();
  ui.dom.window.document.documentElement.style.overflow = "clip";
  try {
    await ui.render();
    const dialog = ui.dialog();
    assert.equal(dialog.open, true);
    assert.equal(dialog.parentElement, ui.dom.window.document.body);
    assert.equal(dialog.getAttribute("aria-modal"), "true");
    assert.equal(dialog.getAttribute("aria-labelledby"), "live-settings-title");
    assert.equal(dialog.getAttribute("aria-describedby"), "live-settings-description");
    assert.equal(ui.dom.window.document.querySelector("form form, dialog dialog"), null);
    assert.equal(ui.dom.window.document.activeElement?.id, "live-settings-title");
    assert.equal(ui.dom.window.document.documentElement.style.overflow, "hidden");
    await ui.render(false);
    assert.equal(dialog.open, false);
    assert.equal(ui.dom.window.document.activeElement, ui.trigger);
    assert.equal(ui.dom.window.document.documentElement.style.overflow, "clip");
    const body = dialog.querySelector<HTMLElement>(".live-settings-body")!;
    body.scrollTop = 250;
    await ui.render();
    assert.equal(body.scrollTop, 0, "reopening Audio starts at its device controls");
  } finally { await ui.close(); }
});

test("Tab and Shift+Tab stay inside the settings controls, including the instruction textarea", async () => {
  const ui = view();
  try {
    await ui.render();
    const document = ui.dom.window.document;
    const key = async (shiftKey: boolean) => {
      const event = new ui.dom.window.KeyboardEvent("keydown", { key: "Tab", shiftKey, bubbles: true, cancelable: true });
      await act(async () => document.activeElement!.dispatchEvent(event));
      return event;
    };
    assert.equal((await key(true)).defaultPrevented, true);
    assert.equal(document.activeElement?.id, "last");
    assert.equal((await key(false)).defaultPrevented, true);
    assert.equal(document.activeElement?.id, "first");
    assert.equal((await key(true)).defaultPrevented, true);
    assert.equal(document.activeElement?.id, "last");
    document.querySelector("textarea")!.focus();
    assert.equal((await key(false)).defaultPrevented, false, "ordinary traversal through form fields remains native");
  } finally { await ui.close(); }
});

test("Escape and a complete backdrop click close only settings, and pending saves prevent dismissal", async () => {
  const ui = view();
  try {
    await ui.render();
    await ui.cancel();
    assert.equal(ui.count(), 1);
    await ui.pointer("pointerdown", 200, 200);
    await ui.pointer("click", 20, 20);
    assert.equal(ui.count(), 1, "dragging from the form to its backdrop does not close it");
    await ui.pointer("pointerdown", 20, 20);
    await ui.pointer("click", 20, 20);
    assert.equal(ui.count(), 2);
    await ui.render(true, true);
    await ui.cancel();
    await ui.pointer("pointerdown", 20, 20);
    await ui.pointer("click", 20, 20);
    assert.equal(ui.count(), 2);
    assert.equal(ui.dialog().open, true);
  } finally { await ui.close(); }
});

test("closing does not steal focus from another application dialog", async () => {
  const ui = view();
  try {
    await ui.render();
    const other = ui.dom.window.document.createElement("dialog");
    other.open = true;
    const control = ui.dom.window.document.createElement("button");
    other.append(control); ui.dom.window.document.body.append(other); control.focus();
    await ui.render(false);
    assert.equal(ui.dom.window.document.activeElement, control);
  } finally { await ui.close(); }
});
