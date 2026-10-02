import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LiveDock } from "./LiveDock.js";
import type { LiveDockProps } from "./LiveDock.js";

function view() {
  const dom = new JSDOM("<main><div id='root'></div><button id='chat'>Chat remains available</button></main>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const actions: string[] = [];
  const props: LiveDockProps = {
    state: { phase: "idle", sessionId: "", error: "", notice: "", micMuted: false, outputMuted: false, playbackBlocked: false, startedAt: 0, endedAt: 0, captions: [] },
    status: "Ready to connect", elapsed: "00:00", subtitle: "Talk with your assistant.", disabled: false, unavailable: "", loading: false, voice: "cove", provider: "ChatGPT", backend: "Current assistant", settingsOpen: false, settingsSaving: false,
    start: () => actions.push("start"), end: () => actions.push("end"), close: () => actions.push("close"), configure: () => actions.push("configure"), muteInput: () => actions.push("mute-input"), muteOutput: () => actions.push("mute-output"), play: () => actions.push("play"), refresh: () => actions.push("refresh"),
  };
  return {
    dom, container, actions, props,
    render: async () => act(async () => root.render(createElement(LiveDock, props))),
    click: async (selector: string) => act(async () => container.querySelector<HTMLButtonElement>(selector)!.click()),
    async close() {
      await act(async () => root.unmount()); dom.window.close();
      for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("the voice dock is a nonmodal region with explicit connection controls and no separate transcript", async () => {
  const ui = view();
  ui.props.state.captions = [{ id: 1, speaker: "user", text: "A caption belonging in the chat", start: 1, end: 2 }];
  try {
    await ui.render();
    assert.ok(ui.container.querySelector('aside[aria-label="Live voice"]'));
    assert.equal(ui.container.querySelector("dialog,[aria-modal=true],[role=log]"), null);
    assert.equal(ui.dom.window.document.getElementById("chat")?.closest("[inert]"), null);
    assert.doesNotMatch(ui.container.textContent!, /A caption belonging in the chat/);
    assert.match(ui.container.textContent!, /Captions appear in this chat/);
    await ui.click(".live-dock-start"); await ui.click('[aria-label="Close Live voice"]');
    assert.deepEqual(ui.actions, ["start", "close"]);
    ui.props.disabled = true; await ui.render();
    assert.equal(ui.container.querySelector<HTMLButtonElement>(".live-dock-start")?.disabled, true);
  } finally { await ui.close(); }
});

test("connected controls expose microphone and speaker state with separate playback/end actions", async () => {
  const ui = view();
  Object.assign(ui.props.state, { phase: "connected", outputMuted: true, playbackBlocked: true });
  try {
    await ui.render();
    assert.equal(ui.container.querySelector('[aria-label="Mute microphone"]')?.getAttribute("aria-pressed"), "false");
    assert.equal(ui.container.querySelector('[aria-label="Unmute speaker"]')?.getAttribute("aria-pressed"), "true");
    await ui.click('[aria-label="Mute microphone"]'); await ui.click('[aria-label="Unmute speaker"]');
    await ui.click(".live-dock-play"); await ui.click('[aria-label="End voice"]'); await ui.click('[aria-label="End voice and close"]');
    assert.deepEqual(ui.actions, ["mute-input", "mute-output", "play", "end", "close"]);
    assert.equal(ui.container.querySelector<HTMLButtonElement>('[aria-label="Voice settings"]')?.disabled, true);
  } finally { await ui.close(); }
});

test("connection cancellation, recoverable errors and settings remain explicit and independently accessible", async () => {
  const ui = view();
  ui.props.children = createElement("section", { "aria-label": "Voice preferences" }, "Device settings");
  ui.props.state.phase = "permission";
  try {
    await ui.render();
    assert.equal(ui.container.querySelector<HTMLButtonElement>('[aria-label="Mute microphone"]')?.disabled, true);
    await ui.click('[aria-label="Cancel connection"]');
    assert.deepEqual(ui.actions, ["end"]);
    Object.assign(ui.props.state, { phase: "error", error: "Your microphone disconnected." });
    ui.props.settingsOpen = true; await ui.render();
    assert.equal(ui.container.querySelector<HTMLElement>(".live-dock-settings")?.hidden, false);
    assert.equal(ui.container.querySelector('[aria-label="Voice settings"]')?.getAttribute("aria-expanded"), "true");
    assert.equal(ui.container.querySelector<HTMLButtonElement>(".live-dock-start")?.disabled, true);
    assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /microphone disconnected/);
    await ui.click(".live-dock-feedback button:last-child");
    assert.equal(ui.actions.at(-1), "refresh");
    ui.props.settingsSaving = true; await ui.render();
    assert.equal(ui.container.querySelector<HTMLButtonElement>('[aria-label="Close Live voice"]')?.disabled, true);
  } finally { await ui.close(); }
});
