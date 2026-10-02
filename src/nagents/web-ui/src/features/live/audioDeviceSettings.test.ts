import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
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
    render: async () => act(async () => root.render(createElement(AudioDeviceSettings))),
    change: async (name: string, value: string) => act(async () => {
      const field = picker(name); field.value = value; field.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    }),
    unmount: async () => { await act(async () => root.unmount()); mounted = false; },
    async close() {
      if (mounted) await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "navigator", "localStorage", "AudioContext", "IS_REACT_ACT_ENVIRONMENT"]) {
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
