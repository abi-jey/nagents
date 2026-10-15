import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { VoiceControls, type VoiceControlsProps } from "./VoiceControls.js";

function view(initial: Partial<VoiceControlsProps> = {}) {
  const dom = new JSDOM("<div id='root'></div><textarea id='composer'></textarea><dialog open><button id='dialog-action'>Device</button></dialog>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  // Deliberately do not install HTMLElement/Element globals: DOM checks use the owner document.
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const calls = { start: 0, end: 0, close: 0, configure: 0, input: 0, output: 0 };
  let props: VoiceControlsProps = {
    phase: "idle", status: "Ready for voice", voice: "Sol", elapsed: "", micMuted: false, outputMuted: false,
    startDisabled: false, settingsOpen: false, settingsSaving: false,
    start: () => { calls.start++; }, end: () => { calls.end++; }, close: () => { calls.close++; },
    configure: () => { calls.configure++; }, muteInput: () => { calls.input++; }, muteOutput: () => { calls.output++; },
    ...initial,
  };
  const button = (selector: string) => container.querySelector<HTMLButtonElement>(selector)!;
  return {
    dom, container, calls, button,
    trigger: () => button(".voice-controls-trigger"),
    panel: () => container.querySelector<HTMLDivElement>(".voice-controls-panel")!,
    async render(next: Partial<VoiceControlsProps> = {}) {
      props = { ...props, ...next };
      await act(async () => root.render(createElement(VoiceControls, props)));
    },
    async click(selector: string) { await act(async () => button(selector).click()); },
    async close() {
      await act(async () => root.unmount()); dom.window.close();
      for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("voice disclosure expands the same trigger without starting or changing audio", async () => {
  const ui = view();
  try {
    await ui.render();
    const trigger = ui.trigger();
    assert.equal(trigger.getAttribute("aria-expanded"), "false");
    assert.equal(trigger.getAttribute("aria-controls"), ui.panel().id);
    assert.equal(ui.panel().getAttribute("aria-hidden"), "true");
    assert.ok(ui.panel().hasAttribute("inert"));
    await ui.click(".voice-controls-trigger");
    assert.equal(ui.trigger(), trigger);
    assert.equal(trigger.getAttribute("aria-expanded"), "true");
    assert.equal(ui.panel().getAttribute("aria-hidden"), "false");
    assert.equal(ui.panel().hasAttribute("inert"), false);
    assert.equal(ui.button(".voice-start").textContent, "Start voice");
    assert.ok(ui.button(".voice-mic").disabled);
    assert.ok(ui.button(".voice-speaker").disabled);
    assert.deepEqual(ui.calls, { start: 0, end: 0, close: 0, configure: 0, input: 0, output: 0 });
    assert.ok([...ui.container.querySelectorAll("button")].every(button => button.type === "button"));
    await ui.click(".voice-start");
    assert.equal(ui.calls.start, 1);
  } finally { await ui.close(); }
});

test("connected microphone and speaker states stay separate and update without replacing the trigger", async () => {
  const ui = view({ phase: "connected", status: "Listening", elapsed: "01:23" });
  try {
    await ui.render();
    const trigger = ui.trigger();
    assert.match(trigger.getAttribute("aria-label")!, /Live/);
    assert.equal(trigger.title, "Live · Sol · 01:23");
    await ui.click(".voice-controls-trigger");
    await ui.click(".voice-mic"); await ui.click(".voice-speaker");
    assert.equal(ui.calls.input, 1); assert.equal(ui.calls.output, 1);
    await ui.render({ micMuted: true, outputMuted: true });
    assert.equal(ui.trigger(), trigger);
    assert.match(trigger.getAttribute("aria-label")!, /Live/);
    assert.equal(ui.button(".voice-mic").getAttribute("aria-pressed"), "true");
    assert.equal(ui.button(".voice-mic").getAttribute("aria-label"), "Unmute microphone");
    assert.equal(ui.button(".voice-speaker").getAttribute("aria-pressed"), "true");
    assert.equal(ui.button(".voice-speaker").getAttribute("aria-label"), "Unmute speaker");
    assert.equal(ui.calls.start, 0); assert.equal(ui.calls.end, 0);
    await ui.render({ micMuted: false, outputMuted: false, status: "Speaking" });
    assert.equal(ui.trigger(), trigger);
    assert.match(trigger.getAttribute("aria-label")!, /Live/);
    assert.equal(ui.container.querySelector('[role="status"]')!.textContent, "Live");
  } finally { await ui.close(); }
});

test("Escape collapses controls and restores trigger focus without ending voice", async () => {
  const ui = view({ phase: "connected" });
  try {
    await ui.render(); await ui.click(".voice-controls-trigger");
    await act(async () => {
      ui.button(".voice-speaker").focus();
      ui.button(".voice-speaker").dispatchEvent(new ui.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    assert.equal(ui.trigger().getAttribute("aria-expanded"), "false");
    assert.equal(ui.dom.window.document.activeElement, ui.trigger());
    assert.ok(ui.panel().hasAttribute("inert"));
    assert.equal(ui.calls.end, 0); assert.equal(ui.calls.close, 0);
  } finally { await ui.close(); }
});

test("returning to the draft or clicking outside collapses controls without interrupting media", async () => {
  const ui = view({ phase: "connected" });
  try {
    await ui.render(); await ui.click(".voice-controls-trigger");
    const textarea = ui.dom.window.document.querySelector("textarea")!;
    await act(async () => textarea.focus());
    assert.equal(ui.trigger().getAttribute("aria-expanded"), "false");
    assert.equal(ui.dom.window.document.activeElement, textarea);
    await ui.click(".voice-controls-trigger");
    await act(async () => { textarea.dispatchEvent(new ui.dom.window.Event("pointerdown", { bubbles: true })); });
    assert.equal(ui.trigger().getAttribute("aria-expanded"), "false");
    assert.equal(ui.calls.end, 0); assert.equal(ui.calls.close, 0);
  } finally { await ui.close(); }
});

test("settings stays expanded through modal focus and allows focus to return to Settings", async () => {
  const ui = view({ phase: "connected" });
  try {
    await ui.render(); await ui.click(".voice-controls-trigger");
    await ui.click(".voice-settings-trigger");
    assert.equal(ui.calls.configure, 1);
    const dialogButton = ui.dom.window.document.querySelector<HTMLButtonElement>("#dialog-action")!;
    await act(async () => dialogButton.focus());
    assert.equal(ui.trigger().getAttribute("aria-expanded"), "true", "dialog focus is safe before the settings flag renders");
    await ui.render({ settingsOpen: true });
    await act(async () => ui.dom.window.document.querySelector("textarea")!.focus());
    assert.equal(ui.trigger().getAttribute("aria-expanded"), "true", "an open modal owns focus transitions");
    await ui.render({ settingsOpen: false });
    await act(async () => ui.button(".voice-settings-trigger").focus());
    assert.equal(ui.dom.window.document.activeElement, ui.button(".voice-settings-trigger"));
    assert.equal(ui.panel().hasAttribute("inert"), false);
    assert.equal(ui.calls.start, 0); assert.equal(ui.calls.end, 0);
  } finally { await ui.close(); }
});

test("initial settings entry reveals its return target and never starts voice", async () => {
  const ui = view({ settingsOpen: true });
  try {
    await ui.render();
    assert.equal(ui.trigger().getAttribute("aria-expanded"), "true");
    assert.equal(ui.button(".voice-start").disabled, true);
    await ui.click(".voice-start");
    assert.equal(ui.calls.start, 0);
  } finally { await ui.close(); }
});

test("stop, cancellation and close remain distinct across active phases and saves", async () => {
  const ui = view({ phase: "connected" });
  try {
    await ui.render(); await ui.click(".voice-controls-trigger");
    await ui.click(".voice-stop"); await ui.click(".voice-dismiss");
    assert.equal(ui.calls.end, 1); assert.equal(ui.calls.close, 1);
    await ui.render({ phase: "permission", status: "Allow microphone" });
    assert.equal(ui.button(".voice-stop").getAttribute("aria-label"), "Cancel connection");
    assert.equal(ui.button(".voice-mic").disabled, true);
    await ui.click(".voice-stop"); assert.equal(ui.calls.end, 2);
    await ui.render({ phase: "ending", status: "Ending voice" });
    assert.equal(ui.button(".voice-stop").disabled, true);
    assert.equal(ui.button(".voice-dismiss").disabled, false);
    await ui.click(".voice-stop"); assert.equal(ui.calls.end, 2);
    await ui.render({ settingsSaving: true });
    assert.equal(ui.button(".voice-dismiss").disabled, true);
    assert.equal(ui.button(".voice-settings-trigger").disabled, true);
    await ui.click(".voice-dismiss"); assert.equal(ui.calls.close, 1);
  } finally { await ui.close(); }
});

test("ended and failed sessions require an explicit enabled start action", async () => {
  const ui = view({ phase: "ended", status: "Stopped" });
  try {
    await ui.render(); await ui.click(".voice-controls-trigger");
    assert.equal(ui.button(".voice-start").textContent, "Start again");
    assert.equal(ui.calls.start, 0);
    await ui.click(".voice-start"); assert.equal(ui.calls.start, 1);
    await ui.render({ phase: "error", status: "Connection failed", startDisabled: true });
    assert.equal(ui.button(".voice-start").textContent, "Retry");
    assert.equal(ui.button(".voice-start").disabled, true);
    await ui.click(".voice-start"); assert.equal(ui.calls.start, 1);
    await ui.render({ startDisabled: false });
    await ui.click(".voice-start"); assert.equal(ui.calls.start, 2);
  } finally { await ui.close(); }
});
