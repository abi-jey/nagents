import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import type { ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { AudioDeviceSettings } from "./AudioDeviceSettings.js";
import { readAudioDevices, saveAudioDevices } from "./devices.js";

function view({ output = true, mediaAvailable = true } = {}) {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://localhost" });
  const original = Object.getOwnPropertyDescriptors(globalThis);
  const changes = new Set<() => void>();
  let permissionCalls = 0, stops = 0;
  let inventory = [
    { kind: "audioinput", deviceId: "mic-one", label: "Desk microphone" },
    { kind: "audiooutput", deviceId: "speaker-one", label: "Headphones" },
  ];
  let permission = async () => ({ getTracks: () => [{ stop: () => { stops++; } }] });
  const media = {
    enumerateDevices: async () => inventory,
    getUserMedia: async () => { permissionCalls++; return permission(); },
    addEventListener: (_name: string, handler: () => void) => { changes.add(handler); },
    removeEventListener: (_name: string, handler: () => void) => { changes.delete(handler); },
  };
  class AudioContext {}
  if (output) Object.assign(AudioContext.prototype, { setSinkId: async () => {} });
  for (const [key, value] of Object.entries({
    window: dom.window, document: dom.window.document, navigator: mediaAvailable ? { mediaDevices: media } : {},
    localStorage: dom.window.localStorage, AudioContext, IS_REACT_ACT_ENVIRONMENT: true,
    addEventListener: dom.window.addEventListener.bind(dom.window), removeEventListener: dom.window.removeEventListener.bind(dom.window),
  })) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  saveAudioDevices({ inputId: "", outputId: "" });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  let mounted = true;
  const picker = (name: string) => container.querySelector<HTMLSelectElement>(`select[aria-label="${name}"]`)!;
  const button = (name: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(element => element.textContent === name)!;
  return {
    root, container, dom, media, changes, picker, button,
    permissionCalls: () => permissionCalls, stops: () => stops,
    inventory: (next: typeof inventory) => { inventory = next; },
    permission: (next: typeof permission) => { permission = next; },
    render: async (props: ComponentProps<typeof AudioDeviceSettings> = {}) => act(async () => root.render(createElement(AudioDeviceSettings, props))),
    change: async (name: string, value: string) => act(async () => {
      const field = picker(name); field.value = value; field.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    }),
    unmount: async () => { await act(async () => root.unmount()); mounted = false; },
    async close() {
      if (mounted) await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "navigator", "localStorage", "AudioContext", "IS_REACT_ACT_ENVIRONMENT", "addEventListener", "removeEventListener"]) {
        if (original[key]) Object.defineProperty(globalThis, key, original[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("device selectors start at system defaults and persist independent input/output choices immediately", async () => {
  const ui = view();
  try {
    await ui.render();
    assert.equal(ui.picker("Microphone").value, ""); assert.equal(ui.picker("Speaker").value, "");
    assert.equal(ui.picker("Microphone").selectedOptions[0].textContent, "System default");
    assert.equal(ui.permissionCalls(), 0, "opening settings only enumerates devices");
    await ui.change("Microphone", "mic-one"); await ui.change("Speaker", "speaker-one");
    assert.deepEqual(readAudioDevices(), { inputId: "mic-one", outputId: "speaker-one" });
    await ui.change("Microphone", ""); await ui.change("Speaker", "");
    assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
    await act(async () => ui.button("Refresh devices").click());
    assert.equal(ui.permissionCalls(), 0, "refresh with known labels does not start a microphone");
  } finally { await ui.close(); }
});

test("missing selected devices remain visible and hardware changes refresh choices without changing the preference", async () => {
  const ui = view();
  try {
    saveAudioDevices({ inputId: "unplugged-mic", outputId: "speaker-one" });
    await ui.render();
    assert.equal(ui.picker("Microphone").value, "unplugged-mic");
    assert.equal(ui.picker("Microphone").selectedOptions[0].textContent, "Saved device · unavailable");
    ui.inventory([{ kind: "audioinput", deviceId: "unplugged-mic", label: "Reconnected microphone" }]);
    await act(async () => ui.changes.forEach(changed => changed()));
    assert.equal(ui.picker("Microphone").selectedOptions[0].textContent, "Reconnected microphone");
    assert.equal(ui.picker("Speaker").value, "speaker-one");
    assert.equal(ui.picker("Speaker").selectedOptions[0].textContent, "Saved device · unavailable");
    await ui.unmount(); assert.equal(ui.changes.size, 0);
  } finally { await ui.close(); }
});

test("unsupported speaker selection preserves a clearable saved choice while input remains selectable", async () => {
  const ui = view({ output: false });
  try {
    saveAudioDevices({ inputId: "", outputId: "speaker-one" });
    await ui.render();
    assert.equal(ui.picker("Speaker").disabled, false, "System default must remain selectable");
    assert.equal(ui.picker("Speaker").querySelector<HTMLOptionElement>('option[value="speaker-one"]')?.disabled, true);
    await ui.change("Speaker", "");
    assert.equal(ui.picker("Speaker").disabled, true);
    assert.equal(ui.picker("Microphone").disabled, false);
    await ui.change("Microphone", "mic-one");
    assert.deepEqual(readAudioDevices(), { inputId: "mic-one", outputId: "" });
    assert.match(ui.container.textContent!, /system default speaker/);
  } finally { await ui.close(); }
});

test("an unavailable media browser retains safe defaults without requesting a microphone", async () => {
  const ui = view({ output: false, mediaAvailable: false });
  try {
    await ui.render();
    assert.equal(ui.picker("Microphone").value, ""); assert.equal(ui.picker("Speaker").value, "");
    assert.equal(ui.button("Refresh devices").disabled, true); assert.equal(ui.permissionCalls(), 0);
    assert.match(ui.container.textContent!, /localhost or HTTPS/);
  } finally { await ui.close(); }
});

test("Show devices alone requests permission and stops its temporary microphone", async () => {
  const ui = view();
  ui.inventory([{ kind: "audioinput", deviceId: "mic-one", label: "" }]);
  ui.permission(async () => {
    ui.inventory([{ kind: "audioinput", deviceId: "mic-one", label: "Permitted microphone" }]);
    return { getTracks: () => [{ stop: () => { stopped++; } }] };
  });
  let stopped = 0;
  try {
    await ui.render(); assert.equal(ui.permissionCalls(), 0);
    await act(async () => ui.button("Show devices").click());
    assert.equal(ui.permissionCalls(), 1); assert.equal(stopped, 1);
    assert.equal(ui.picker("Microphone").options[1].textContent, "Permitted microphone");
    assert.equal(ui.picker("Microphone").value, "", "permission does not change the selected device");
  } finally { await ui.close(); }
});

test("closing settings during permission stops a late microphone and cannot save a stale choice", async () => {
  const ui = view();
  let resolve!: (stream: { getTracks: () => { stop: () => void }[] }) => void;
  let stopped = 0;
  ui.inventory([{ kind: "audioinput", deviceId: "mic-one", label: "" }]);
  ui.permission(() => new Promise(done => { resolve = done; }));
  try {
    await ui.render();
    await act(async () => ui.button("Show devices").click());
    await ui.unmount();
    await act(async () => resolve({ getTracks: () => [{ stop: () => { stopped++; } }] }));
    assert.equal(stopped, 1); assert.equal(ui.changes.size, 0);
    assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
  } finally { await ui.close(); }
});

test("the browser speaker chooser can authorize an output not previously listed", async () => {
  const ui = view();
  Object.assign(ui.media, { selectAudioOutput: async () => ({ deviceId: "new-speaker", label: "Meeting room speaker" }) });
  try {
    await ui.render();
    await act(async () => ui.button("Choose speaker…").click());
    assert.equal(ui.picker("Speaker").selectedOptions[0].textContent, "Meeting room speaker");
    assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "new-speaker" });
    assert.equal(ui.permissionCalls(), 0);
  } finally { await ui.close(); }
});

test("storage failure explains that device choices still apply in the current browser tab", async (t) => {
  const ui = view();
  try {
    await ui.render();
    t.mock.method(ui.dom.window.Storage.prototype, "setItem", () => { throw new Error("Storage unavailable"); });
    await ui.change("Microphone", "mic-one");
    assert.equal(ui.picker("Microphone").value, "mic-one");
    assert.deepEqual(readAudioDevices(), { inputId: "mic-one", outputId: "" });
    assert.match(ui.container.textContent!, /Saved for this tab only/);
    t.mock.restoreAll();
    saveAudioDevices({ inputId: "", outputId: "" });
  } finally { await ui.close(); }
});

test("active device changes wait for successful routing before saving and serialize input/output selections", async () => {
  const ui = view();
  const calls: [string, string][] = [];
  let complete!: () => void;
  const applyDevice = (kind: "input" | "output", id: string) => {
    calls.push([kind, id]);
    return new Promise<void>(resolve => { complete = resolve; });
  };
  try {
    await ui.render({ active: true, applyDevice });
    assert.equal(ui.picker("Microphone").disabled, false);
    assert.equal(ui.picker("Speaker").disabled, false);
    assert.match(ui.container.textContent!, /Applies immediately/);
    await ui.change("Microphone", "mic-one");
    assert.deepEqual(calls, [["input", "mic-one"]]);
    assert.equal(ui.picker("Microphone").value, "");
    assert.equal(ui.picker("Microphone").disabled, true);
    assert.equal(ui.picker("Speaker").disabled, true);
    assert.equal(ui.container.querySelector("section")?.getAttribute("aria-busy"), "true");
    assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
    await ui.change("Speaker", "speaker-one");
    assert.equal(calls.length, 1, "a second change cannot race the pending microphone route");
    await act(async () => complete());
    assert.equal(ui.picker("Microphone").value, "mic-one");
    assert.deepEqual(readAudioDevices(), { inputId: "mic-one", outputId: "" });
    await ui.change("Speaker", "speaker-one");
    assert.deepEqual(calls[1], ["output", "speaker-one"]);
    await act(async () => complete());
    assert.equal(ui.picker("Speaker").disabled, false);
    assert.deepEqual(readAudioDevices(), { inputId: "mic-one", outputId: "speaker-one" });
  } finally { await ui.close(); }
});

test("a failed live device switch keeps the prior selection and preference and reports an inline error", async () => {
  const ui = view();
  saveAudioDevices({ inputId: "mic-one", outputId: "speaker-one" });
  try {
    await ui.render({ active: true, applyDevice: async () => { throw new Error("The selected microphone is no longer available."); } });
    await ui.change("Microphone", "");
    assert.equal(ui.picker("Microphone").value, "mic-one");
    assert.deepEqual(readAudioDevices(), { inputId: "mic-one", outputId: "speaker-one" });
    assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /no longer available/);
    assert.equal(ui.picker("Microphone").disabled, false);
    assert.equal(ui.picker("Speaker").disabled, false);
  } finally { await ui.close(); }
});

test("active settings only enumerate devices even when microphone labels are missing", async () => {
  const ui = view();
  ui.inventory([{ kind: "audioinput", deviceId: "mic-one", label: "" }]);
  try {
    await ui.render({ active: true });
    assert.equal(ui.button("Show devices"), undefined);
    await act(async () => ui.button("Refresh devices").click());
    assert.equal(ui.permissionCalls(), 0);
    assert.match(ui.container.textContent!, /Ready to change during voice/);
    assert.doesNotMatch(ui.container.textContent!, /microphone turns off/);
  } finally { await ui.close(); }
});

test("native speaker selection during voice applies routing and retains the current speaker on failure", async () => {
  const ui = view();
  saveAudioDevices({ inputId: "", outputId: "speaker-one" });
  Object.assign(ui.media, { selectAudioOutput: async () => ({ deviceId: "new-speaker", label: "Meeting room speaker" }) });
  const calls: [string, string][] = [];
  try {
    await ui.render({ active: true, applyDevice: async (kind, id) => { calls.push([kind, id]); throw new Error("Speaker permission expired."); } });
    await act(async () => ui.button("Choose speaker…").click());
    assert.deepEqual(calls, [["output", "new-speaker"]]);
    assert.equal(ui.picker("Speaker").value, "speaker-one");
    assert.equal(readAudioDevices().outputId, "speaker-one");
    assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /Speaker permission expired/);
    assert.equal(ui.permissionCalls(), 0);
  } finally { await ui.close(); }
});

test("unmounting during an active switch does not save a late result", async () => {
  const ui = view();
  let complete!: () => void;
  try {
    await ui.render({ active: true, applyDevice: () => new Promise<void>(resolve => { complete = resolve; }) });
    await ui.change("Microphone", "mic-one");
    await ui.unmount();
    await act(async () => complete());
    assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "" });
  } finally { await ui.close(); }
});

test("active pickers follow the live route rather than another tab's saved defaults", async () => {
  const ui = view();
  const activeSelection = { inputId: "mic-one", outputId: "speaker-one" };
  const calls: [string, string][] = [];
  const applyDevice = async (kind: "input" | "output", id: string) => { calls.push([kind, id]); };
  try {
    await ui.render({ active: true, activeSelection, applyDevice });
    assert.equal(ui.picker("Microphone").value, "mic-one");
    assert.equal(ui.picker("Speaker").value, "speaker-one");
    saveAudioDevices({ inputId: "other-tab-mic", outputId: "other-tab-speaker" });
    await act(async () => ui.dom.window.dispatchEvent(new ui.dom.window.StorageEvent("storage")));
    assert.equal(ui.picker("Microphone").value, "mic-one");
    assert.equal(ui.picker("Speaker").value, "speaker-one");
    assert.deepEqual(calls, []);
    await ui.change("Microphone", "");
    assert.deepEqual(calls, [["input", ""]]);
    assert.deepEqual(readAudioDevices(), { inputId: "", outputId: "other-tab-speaker" });
    await ui.render({ active: true, activeSelection: { ...activeSelection, inputId: "" }, applyDevice });
    assert.equal(ui.picker("Microphone").value, "");
    assert.equal(ui.picker("Speaker").value, "speaker-one");
    await ui.render();
    assert.equal(ui.picker("Speaker").value, "other-tab-speaker", "idle settings reflect saved defaults again");
  } finally { await ui.close(); }
});

for (const outcome of ["success", "failure", "moved", "hidden"] as const) test(`device switch focus recovery respects ${outcome}`, async () => {
  const ui = view();
  let complete!: () => void;
  try {
    await ui.render({ active: true, applyDevice: () => new Promise<void>((resolve, reject) => {
      complete = outcome === "failure" ? () => reject(new Error("Device disconnected")) : resolve;
    }) });
    const picker = ui.picker("Microphone");
    picker.focus();
    await ui.change("Microphone", "mic-one");
    // Chrome drops focus to body when a focused select becomes disabled.
    ui.dom.window.document.body.tabIndex = -1;
    ui.dom.window.document.body.focus();
    const elsewhere = ui.dom.window.document.createElement("button");
    ui.dom.window.document.body.append(elsewhere);
    if (outcome === "moved") { elsewhere.focus(); elsewhere.blur(); }
    if (outcome === "hidden") ui.container.hidden = true;
    await act(async () => complete());
    assert.equal(ui.dom.window.document.activeElement, outcome === "success" || outcome === "failure" ? picker : ui.dom.window.document.body);
  } finally { await ui.close(); }
});
