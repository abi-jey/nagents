import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import type { ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LiveDialog } from "./LiveDialog.js";
import { LiveController } from "./controller.js";
import { readAudioDevices, saveAudioDevices } from "./devices.js";
import type { LiveCapability, LiveDelegationDetails, LiveDelegationRecord, LiveSettingsSnapshot, LiveSnapshot } from "./types.js";

const capability: LiveCapability = {
  available: true, reason: "", enabled: true, key_configured: true, revision: "voice-revision",
  provider: "openai", voice_auth: "chatgpt", transport: "webrtc", model: "gpt-live-1-codex",
  backend_mode: "assistant", backend_model: "gpt-test", voice: "cove", voices: ["cove", "breeze"],
  assistant: { agent: "assistant", provider: "openai", model: "gpt-test" }, active_session_id: "",
};
const preferences = { enabled: true, connection_id: "", backend_mode: "assistant" as const, model: "gpt-live-1-codex", backend_model: "gpt-test", voice: "cove" };
const settings: LiveSettingsSnapshot = {
  values: { ...preferences, provider: "openai", base_url: "" }, revision: "settings-revision", scope: "workspace",
  global_preferences: preferences, overrides: {}, key_configured: true, voice_auth: "chatgpt",
  providers: ["openai"], voices: ["cove", "breeze"], live_supported: true, profile_name: "chatgpt",
  origins: { enabled: "global", connection_id: "global", backend_mode: "global", model: "global", backend_model: "global", voice: "global" },
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function view(options: { capability?: Promise<Response>; settings?: Promise<Response>; denied?: boolean; delegation?: boolean } = {}) {
  const dom = new JSDOM("<footer class='composer-area'><div id='root'></div><form id='text-form'><textarea id='composer'></textarea><button type='submit'>Send</button></form></footer>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const frames = new Map<number, FrameRequestCallback>();
  let frameId = 0, captures = 0, creates = 0, closures = 0, capabilityReads = 0, settingsReads = 0, dismissals = 0, submissions = 0;
  let currentCapability = capability;
  const tracks: { enabled: boolean; readyState: string; stop(): void; addEventListener(): void }[] = [];
  const record: LiveDelegationRecord = {
    seq: 1, delegation_id: "delegation-one", voice_session_id: "voice-one", chat_session_id: "ngn-one", run_id: "run-one",
    status: "working", agent: "assistant", provider: "openai", model: "gpt-test", text: "The assistant is working.",
  };
  const detail: LiveDelegationDetails = { ...record, source: "app_callback", request: {}, timeline: [record], timeline_truncated: false };
  const snapshot = (status: LiveSnapshot["status"]): LiveSnapshot => ({
    session_id: "voice-one", status, model: capability.model, voice: "cove", cursor: 1, events: [],
    message: status === "closed" ? "Live session closed. Finalization confirmed." : "Connected",
    delegations: options.delegation ? [record] : [],
  });
  class Audio {
    srcObject = null;
    pause() {}
    async play() {}
  }
  class Peer {
    connectionState = "new";
    onconnectionstatechange: (() => void) | undefined;
    createDataChannel() { return { close() {} }; }
    addTrack() { return { async replaceTrack() {} }; }
    async createOffer() { return { sdp: "v=0\r\nfixture-offer\r\n" }; }
    async setLocalDescription() {}
    async setRemoteDescription() { this.connectionState = "connected"; this.onconnectionstatechange?.(); }
    close() { this.connectionState = "closed"; this.onconnectionstatechange?.(); }
  }
  const globals = {
    window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    HTMLMediaElement: dom.window.HTMLMediaElement, localStorage: dom.window.localStorage,
    Audio, AudioContext: undefined, RTCPeerConnection: Peer, isSecureContext: true, IS_REACT_ACT_ENVIRONMENT: true,
    navigator: { mediaDevices: {
      getUserMedia: async () => {
        captures++;
        if (options.denied) throw new DOMException("Not allowed", "NotAllowedError");
        const track = { enabled: true, readyState: "live", stop() { this.readyState = "ended"; }, addEventListener() {} };
        tracks.push(track);
        return { getTracks: () => [track], getAudioTracks: () => [track] };
      },
      enumerateDevices: async () => [{ kind: "audioinput", deviceId: "microphone", label: "Microphone" }],
      addEventListener() {}, removeEventListener() {},
    } },
    requestAnimationFrame: (callback: FrameRequestCallback) => { const id = ++frameId; frames.set(id, callback); return id; },
    cancelAnimationFrame: (id: number) => { frames.delete(id); },
    fetch: async (path: string, init: RequestInit) => {
      if (path === "/api/live") { capabilityReads++; return options.capability && capabilityReads === 1 ? options.capability : Response.json(currentCapability); }
      if (path.startsWith("/api/live/settings")) { settingsReads++; return options.settings && settingsReads === 1 ? options.settings : Response.json(settings); }
      if (path === "/api/live/sessions") {
        assert.equal(init.method, "POST"); creates++;
        return Response.json({ session_id: "voice-one", model: capability.model, voice: "cove", sdp: "v=0\r\nfixture-answer\r\n" });
      }
      if (path === "/api/live/sessions/voice-one/close") { closures++; return Response.json(snapshot("closed")); }
      if (path.includes("/delegations/")) return Response.json(detail);
      if (path.startsWith("/api/live/sessions/voice-one?after=")) return Response.json(snapshot("connected"));
      throw new Error(`Unexpected request: ${path}`);
    },
  };
  Object.defineProperty(dom.window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  saveAudioDevices({ inputId: "", outputId: "" });
  const container = dom.window.document.getElementById("root")!;
  const textarea = dom.window.document.querySelector<HTMLTextAreaElement>("textarea")!;
  dom.window.document.getElementById("text-form")!.addEventListener("submit", event => { event.preventDefault(); submissions++; });
  const root = createRoot(container);
  let mounted = true;
  const button = (name: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(item => (item.getAttribute("aria-label") || item.textContent?.trim()) === name)!;
  return {
    dom, container, textarea, tracks, button,
    counts: () => ({ captures, creates, closures, capabilityReads, dismissals, submissions }),
    capability: (next: LiveCapability) => { currentCapability = next; },
    render: async (props: Partial<ComponentProps<typeof LiveDialog>> = {}) => act(async () => root.render(createElement(LiveDialog, {
      token: "fixture-token", sessionId: "ngn-one", close: () => { dismissals++; }, ...props,
      key: `${props.token || "fixture-token"}:${props.sessionId || "ngn-one"}`,
    }))),
    click: async (name: string) => act(async () => button(name).click()),
    expandPreferences: async () => act(async () => {
      const details = container.querySelector<HTMLDetailsElement>("details.voice-preferences")!;
      assert.ok(details);
      if (!details.open) details.querySelector("summary")!.click();
      // Native details dispatches toggle in a queued task; let React receive it.
      await new Promise<void>(resolve => setTimeout(resolve, 0));
      assert.equal(details.open, true);
    }),
    flushFrames: async () => act(async () => { const current = [...frames.values()]; frames.clear(); current.forEach(callback => callback(0)); }),
    unmount: async () => { await act(async () => root.unmount()); mounted = false; },
    async close() {
      if (mounted) await act(async () => root.unmount());
      frames.clear(); dom.window.close();
      for (const key of Object.keys(globals)) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("settings-only entry and saving preferences never start microphone or provider media", async (t) => {
  const ui = view();
  const start = t.mock.method(LiveController.prototype, "start");
  try {
    await ui.render({ autoStart: false });
    assert.equal(ui.container.querySelector<HTMLElement>("#voice-inline-settings")?.hidden, false);
    await ui.expandPreferences();
    await ui.click("Save settings");
    await ui.flushFrames();
    assert.equal(ui.container.querySelector<HTMLElement>("#voice-inline-settings")?.hidden, true);
    assert.ok(ui.counts().capabilityReads >= 2);
    assert.equal(start.mock.calls.length, 0);
    assert.equal(ui.counts().captures, 0);
    assert.equal(ui.counts().creates, 0);
    await ui.click("Start voice");
    assert.equal(start.mock.calls.length, 1);
    assert.equal(ui.counts().creates, 1, "starting afterward is an explicit separate action");
  } finally { await ui.close(); }
});

test("settings-only quick device selection saves locally without requiring a connected call", async () => {
  const ui = view();
  try {
    await ui.render({ autoStart: false });
    const picker = ui.container.querySelector<HTMLSelectElement>('.voice-quick-devices select[aria-label="Microphone"]')!;
    assert.ok(picker);
    assert.equal(picker.closest("form"), null);
    assert.equal(ui.container.querySelector<HTMLDetailsElement>(".voice-preferences")?.open, false);
    await act(async () => {
      picker.value = "microphone";
      picker.dispatchEvent(new ui.dom.window.Event("change", { bubbles: true }));
    });
    assert.equal(readAudioDevices().inputId, "microphone");
    assert.equal(picker.value, "microphone");
    assert.equal(ui.counts().captures, 0);
    assert.equal(ui.counts().creates, 0);
    assert.equal(ui.container.querySelector('.voice-quick-devices [role="alert"]'), null);
  } finally { await ui.close(); }
});

for (const departure of ["close", "unmount", "settings", "pagehide", "session-switch"] as const) {
  test(`pending one-click capability check cannot start after ${departure}`, async (t) => {
    const pending = deferred<Response>();
    const ui = view({ capability: pending.promise });
    const start = t.mock.method(LiveController.prototype, "start");
    try {
      await ui.render();
      assert.equal(ui.counts().captures, 0);
      if (departure === "close") await ui.click("Close voice controls");
      else if (departure === "unmount") await ui.unmount();
      else if (departure === "settings") await ui.click("Audio devices and voice settings");
      else if (departure === "pagehide") await act(async () => ui.dom.window.dispatchEvent(new ui.dom.window.Event("pagehide")));
      else await ui.render({ sessionId: "ngn-two", autoStart: false });
      await act(async () => pending.resolve(Response.json(capability)));
      assert.equal(start.mock.calls.length, 0);
      assert.equal(ui.counts().captures, 0);
      assert.equal(ui.counts().creates, 0);
      if (departure === "close") assert.equal(ui.counts().dismissals, 1);
    } finally { await ui.close(); }
  });
}

test("one-click voice starts once and End capability refresh never restarts it", async (t) => {
  const ui = view();
  const start = t.mock.method(LiveController.prototype, "start");
  try {
    await ui.render();
    assert.equal(start.mock.calls.length, 1);
    assert.equal(ui.counts().captures, 1);
    assert.equal(ui.counts().creates, 1);
    assert.ok(ui.button("End voice"));
    await ui.render();
    assert.equal(start.mock.calls.length, 1, "ordinary composer rerenders keep the same call");
    await ui.click("End voice");
    assert.ok(ui.button("Start again"));
    assert.ok(ui.counts().capabilityReads >= 2);
    assert.equal(start.mock.calls.length, 1);
    assert.equal(ui.counts().captures, 1);
    assert.equal(ui.counts().closures, 1);
    assert.ok(ui.tracks.every(track => track.readyState === "ended"));
  } finally { await ui.close(); }
});

test("permission errors and manual capability refresh never replay the consumed voice intent", async (t) => {
  const ui = view({ denied: true });
  const start = t.mock.method(LiveController.prototype, "start");
  try {
    await ui.render();
    assert.match(ui.container.textContent!, /Microphone access was denied/);
    assert.equal(start.mock.calls.length, 1);
    assert.equal(ui.counts().captures, 1);
    await ui.click("Refresh");
    await ui.render();
    assert.equal(start.mock.calls.length, 1);
    assert.equal(ui.counts().captures, 1);
    assert.equal(ui.counts().creates, 0);
    await ui.click("Retry");
    assert.equal(start.mock.calls.length, 2, "retry requires a new explicit click");
  } finally { await ui.close(); }
});

for (const blocked of ["unavailable", "occupied"] as const) {
  test(`recovering ${blocked} capability does not reuse a previous start intent`, async (t) => {
    const initial = blocked === "unavailable"
      ? { ...capability, available: false, reason: "Voice is unavailable." }
      : { ...capability, active_session_id: "another-call" };
    const ui = view({ capability: Promise.resolve(Response.json(initial)) });
    const start = t.mock.method(LiveController.prototype, "start");
    try {
      await ui.render();
      assert.equal(start.mock.calls.length, 0);
      ui.capability(capability);
      await ui.click("Refresh");
      assert.equal(start.mock.calls.length, 0);
      assert.equal(ui.counts().captures, 0);
      await ui.click("Start voice");
      assert.equal(start.mock.calls.length, 1);
      assert.equal(ui.counts().creates, 1);
    } finally { await ui.close(); }
  });
}

test("opening and closing inline Audio and request details leaves the connected call mounted", async (t) => {
  const ui = view({ delegation: true });
  const end = t.mock.method(LiveController.prototype, "end");
  const dispose = t.mock.method(LiveController.prototype, "dispose");
  try {
    await ui.render();
    await ui.click("Audio devices and voice settings");
    await ui.click("Audio devices and voice settings");
    await ui.click("Audio devices and voice settings");
    await act(async () => ui.container.querySelector(".voice-quick-devices select")!.dispatchEvent(new ui.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    assert.equal(ui.container.querySelector<HTMLElement>("#voice-inline-settings")?.hidden, true);
    await ui.click("Request & events");
    assert.ok(ui.container.querySelector(".delegation-inspector"));
    await ui.click("Close delegation details");
    await ui.click("Request & events");
    await act(async () => ui.container.querySelector("#delegation-inspector-title")!.dispatchEvent(new ui.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    assert.equal(ui.container.querySelector(".delegation-inspector"), null);
    assert.equal(end.mock.calls.length, 0);
    assert.equal(dispose.mock.calls.length, 0);
    assert.equal(ui.counts().creates, 1);
    assert.equal(ui.counts().closures, 0);
    assert.ok(ui.tracks.every(track => track.readyState === "live"));
  } finally { await ui.close(); }
});

test("settings finishing their background load does not steal focus from typed chat", async () => {
  const pending = deferred<Response>();
  const ui = view({ settings: pending.promise });
  try {
    await ui.render({ autoStart: false });
    await ui.expandPreferences();
    ui.textarea.focus();
    await act(async () => pending.resolve(Response.json(settings)));
    assert.equal(ui.dom.window.document.activeElement, ui.textarea);
    assert.equal(ui.counts().captures, 0);
  } finally { await ui.close(); }
});

test("the sphere is a native button outside the text form and toggles microphone without ending or submitting", async () => {
  const ui = view();
  try {
    await ui.render();
    const sphere = ui.container.querySelector<HTMLButtonElement>("button.live-sphere")!;
    assert.ok(sphere);
    assert.equal(sphere.type, "button");
    assert.equal(sphere.tabIndex, 0);
    assert.equal(sphere.closest("form"), null);
    assert.match(sphere.getAttribute("aria-label") || "", /mute (?:voice )?microphone/i);
    await act(async () => sphere.click());
    assert.equal(ui.tracks[0].enabled, false);
    assert.ok(ui.button("Unmute microphone"));
    await act(async () => sphere.click());
    assert.equal(ui.tracks[0].enabled, true);
    assert.equal(ui.counts().closures, 0);
    assert.equal(ui.counts().submissions, 0);
  } finally { await ui.close(); }
});
