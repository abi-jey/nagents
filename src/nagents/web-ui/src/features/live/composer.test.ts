import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LiveComposer } from "./LiveComposer.js";
import type { LiveComposerProps } from "./LiveComposer.js";

function view() {
  const dom = new JSDOM("<main><div id='root'></div><button id='chat'>Chat remains available</button></main>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const actions: string[] = [];
  const props: LiveComposerProps = {
    state: { phase: "idle", sessionId: "", error: "", notice: "", micMuted: false, outputMuted: false, playbackBlocked: false, startedAt: 0, endedAt: 0, captions: [], delegations: [], devices: { inputId: "", outputId: "" }, inputLevel: 0, outputLevel: 0 },
    status: "Ready to connect", elapsed: "00:00", disabled: false, unavailable: "", loading: false, voice: "cove", settingsOpen: false, settingsSaving: false, inspectionOpen: false, inspect: () => actions.push("inspect"),
    start: () => actions.push("start"), end: () => actions.push("end"), close: () => actions.push("close"), configure: () => actions.push("configure"), muteInput: () => actions.push("mute-input"), muteOutput: () => actions.push("mute-output"), play: () => actions.push("play"), refresh: () => actions.push("refresh"), viewChat: runId => actions.push(`view-chat:${runId}`),
  };
  return {
    dom, container, actions, props,
    render: async () => act(async () => root.render(createElement(LiveComposer, props))),
    click: async (selector: string) => act(async () => container.querySelector<HTMLButtonElement>(selector)!.click()),
    async close() {
      await act(async () => root.unmount()); dom.window.close();
      for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("voice belongs to the composer with explicit controls and no separate transcript", async () => {
  const ui = view();
  ui.props.state.captions = [{ id: 1, speaker: "user", text: "A caption belonging in the chat", start: 1, end: 2 }];
  try {
    await ui.render();
    assert.ok(ui.container.querySelector('section[aria-label="Voice in this chat"]'));
    assert.equal(ui.container.querySelector("dialog,[aria-modal=true],[role=log]"), null);
    assert.equal(ui.dom.window.document.getElementById("chat")?.closest("[inert]"), null);
    assert.doesNotMatch(ui.container.textContent!, /A caption belonging in the chat/);
    assert.match(ui.container.textContent!, /Captions in chat/);
    await ui.click(".voice-start"); await ui.click('[aria-label="Close voice controls"]');
    assert.deepEqual(ui.actions, ["start", "close"]);
    ui.props.disabled = true; await ui.render();
    assert.equal(ui.container.querySelector<HTMLButtonElement>(".voice-start")?.disabled, true);
  } finally { await ui.close(); }
});

test("connected controls expose microphone and speaker state with separate playback/end actions", async () => {
  const ui = view();
  Object.assign(ui.props.state, { phase: "connected", outputMuted: true, playbackBlocked: true });
  try {
    await ui.render();
    assert.equal(ui.container.querySelector('button.voice-mic[aria-label="Mute microphone"]')?.getAttribute("aria-pressed"), "false");
    assert.equal(ui.container.querySelector('[aria-label="Unmute speaker"]')?.getAttribute("aria-pressed"), "true");
    await ui.click('button.voice-mic[aria-label="Mute microphone"]'); await ui.click('[aria-label="Unmute speaker"]');
    await ui.click(".voice-enable-audio"); await ui.click('[aria-label="End voice"]');
    assert.deepEqual(ui.actions, ["mute-input", "mute-output", "play", "end"]);
    assert.equal(ui.container.querySelector<HTMLButtonElement>('.voice-settings-trigger')?.disabled, false);
  } finally { await ui.close(); }
});

test("connection cancellation, recoverable errors and settings remain explicit and independently accessible", async () => {
  const ui = view();
  ui.props.children = createElement("section", { "aria-label": "Voice preferences" }, "Device settings");
  ui.props.state.phase = "permission";
  try {
    await ui.render();
    assert.equal(ui.container.querySelector<HTMLButtonElement>('button.voice-mic[aria-label="Mute microphone"]')?.disabled, true);
    await ui.click('[aria-label="Cancel connection"]');
    assert.deepEqual(ui.actions, ["end"]);
    Object.assign(ui.props.state, { phase: "error", error: "Your microphone disconnected." });
    ui.props.settingsOpen = true; await ui.render();
    assert.equal(ui.container.querySelector<HTMLElement>(".voice-inline-expansion")?.hidden, false);
    assert.equal(ui.container.querySelector('.voice-settings-trigger')?.getAttribute("aria-expanded"), "true");
    assert.equal(ui.container.querySelector<HTMLButtonElement>(".voice-start")?.disabled, true);
    assert.match(ui.container.querySelector('[role="alert"]')?.textContent || "", /microphone disconnected/);
    await ui.click(".voice-feedback button");
    assert.equal(ui.actions.at(-1), "refresh");
    await act(async () => ui.container.querySelector("#voice-session")!.dispatchEvent(new ui.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    assert.equal(ui.actions.at(-1), "configure", "Escape dismisses settings, not the voice dock or call");
    ui.props.settingsSaving = true; await ui.render();
    const beforeEscape = ui.actions.length;
    await act(async () => ui.container.querySelector("#voice-session")!.dispatchEvent(new ui.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    assert.equal(ui.actions.length, beforeEscape, "Escape does not dismiss a pending save");
    assert.equal(ui.container.querySelector<HTMLButtonElement>('[aria-label="Close voice controls"]')?.disabled, true);
  } finally { await ui.close(); }
});

test("delegation shows the actual assistant handoff and keeps ongoing work distinct from ending voice", async () => {
  const ui = view();
  ui.props.state.phase = "connected";
  ui.props.state.delegations = [{ id: "delegation-1", sessionId: "call-1", chatSessionId: "chat-1", seq: 2, status: "working", agent: "researcher", provider: "openai", model: "model", runId: "run-1", text: "Working", }];
  try {
    await ui.render();
    const card = ui.container.querySelector('[aria-label="Voice delegation"]')!;
    assert.match(card.textContent!, /Assistant working/);
    await ui.click(".voice-view-chat");
    assert.equal(ui.actions.at(-1), "view-chat:run-1");
    ui.props.state.phase = "ended"; await ui.render();
    assert.match(card.textContent!, /Assistant still working in chat/);
    assert.doesNotMatch(card.textContent!, /Request stopped|Request complete/);
    ui.props.state.delegations[0] = { ...ui.props.state.delegations[0], status: "completed", seq: 3 };
    await ui.render();
    assert.match(card.textContent!, /Request complete/);
  } finally { await ui.close(); }
});

test("normal closure stays quiet while an unconfirmed closure remains visible", async () => {
  const ui = view();
  Object.assign(ui.props.state, { phase: "ended", notice: "Live session closed. Finalization confirmed." });
  try {
    await ui.render();
    assert.equal(ui.container.querySelector(".voice-feedback"), null);
    ui.props.state.notice = "Microphone off. Provider closure is unconfirmed.";
    await ui.render();
    assert.match(ui.container.querySelector(".voice-feedback")?.textContent || "", /unconfirmed/);
  } finally { await ui.close(); }
});
