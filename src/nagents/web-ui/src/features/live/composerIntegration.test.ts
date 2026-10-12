import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, createRef, Fragment, useMemo, useState } from "react";
import type { RefObject } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { Composer } from "../chat/Composer.js";
import type { ComposerVoiceRefs } from "../chat/Composer.js";
import { LiveComposer } from "./LiveComposer.js";
import type { LiveComposerProps, LiveComposerTargets } from "./LiveComposer.js";

interface HarnessProps {
  live: Omit<LiveComposerProps, "targets">;
  open: boolean;
  child: boolean;
  input: RefObject<HTMLTextAreaElement | null>;
  submit(): void;
}

function Harness({ live, open, child, input, submit }: HarnessProps) {
  const [targets, setTargets] = useState<LiveComposerTargets>({ presence: null, caption: null, controls: null, details: null });
  const refs = useMemo<ComposerVoiceRefs>(() => {
    const capture = (name: keyof LiveComposerTargets) => (element: HTMLDivElement | null) => {
      setTargets(current => current[name] === element ? current : { ...current, [name]: element });
    };
    return { presence: capture("presence"), caption: capture("caption"), controls: capture("controls"), details: capture("details") };
  }, []);
  const [prompt, setPrompt] = useState("Keep the original draft here");
  const [childPrompt, setChildPrompt] = useState("Separate note for Explorer");
  return createElement(Fragment, {},
    createElement(Composer, {
      inputRef: input, prompt: child ? childPrompt : prompt, setPrompt: child ? setChildPrompt : setPrompt,
      recipient: child ? "Explorer" : "ngn", commandsEnabled: !child, demo: false, disabled: false,
      canSubmit: true, running: false, submit, cancel() {}, voiceLayout: open && !child, voiceRefs: refs,
    }),
    open && createElement(LiveComposer, { ...live, targets }));
}

function view() {
  const dom = new JSDOM("<main id='root'></main>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = {
    window: dom.window, document: dom.window.document, Element: dom.window.Element, HTMLElement: dom.window.HTMLElement,
    ResizeObserver: class { observe() {} disconnect() {} }, IS_REACT_ACT_ENVIRONMENT: true,
  };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const input = createRef<HTMLTextAreaElement>();
  const actions: string[] = [];
  let submissions = 0;
  const live: HarnessProps["live"] = {
    state: { phase: "connected", sessionId: "voice-one", error: "", notice: "", micMuted: false, outputMuted: false,
      playbackBlocked: false, startedAt: 1, endedAt: 0, captions: [], delegations: [], devices: { inputId: "", outputId: "" }, inputLevel: 0, outputLevel: 0 },
    status: "Live in this chat", elapsed: "00:05", disabled: false, unavailable: "", loading: false, voice: "sol",
    settingsOpen: false, settingsSaving: false, inspectionOpen: false,
    start: () => actions.push("start"), end: () => actions.push("end"), close: () => actions.push("close"),
    configure: () => actions.push("configure"), muteInput: () => actions.push("mute-input"), muteOutput: () => actions.push("mute-output"),
    play: () => actions.push("play"), refresh: () => actions.push("refresh"), inspect: () => actions.push("inspect"), viewChat: id => actions.push(`view-chat:${id}`),
  };
  return {
    dom, container, input, live, actions,
    submissions: () => submissions,
    render: async (open = true, child = false) => act(async () => root.render(createElement(Harness, {
      live, open, child, input, submit: () => { submissions++; },
    }))),
    click: async (selector: string) => {
      if (container.querySelector(selector)?.closest("[inert]"))
        await act(async () => container.querySelector<HTMLButtonElement>(".voice-controls-trigger")!.click());
      const button = container.querySelector<HTMLButtonElement>(selector)!;
      assert.equal(button.type, "button");
      await act(async () => button.click());
    },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of Object.keys(globals)) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

test("voice entry, caption updates, mute and settings preserve the editable draft and one inset sphere", async () => {
  const ui = view();
  try {
    await ui.render(false);
    const textarea = ui.input.current!;
    textarea.focus();
    textarea.setSelectionRange(5, 17, "backward");
    await ui.render();
    const sphere = ui.container.querySelector(".live-sphere")!;
    assert.ok(sphere);
    assert.equal(sphere.closest("form"), textarea.form);
    const changes = [
      () => { ui.live.state.captions = [{ id: 1, speaker: "assistant" as const, text: "This is spoken output.", start: 0, end: 100 }]; },
      () => { ui.live.state.micMuted = true; },
      () => { ui.live.settingsOpen = true; },
      () => { ui.live.settingsOpen = false; ui.live.state.phase = "ended"; },
    ];
    for (const change of changes) {
      change(); await ui.render();
      assert.equal(ui.input.current, textarea, "Voice rerenders must not replace the textarea");
      assert.equal(textarea.value, "Keep the original draft here");
      assert.equal(ui.dom.window.document.activeElement, textarea);
      assert.equal(textarea.selectionStart, 5);
      assert.equal(textarea.selectionEnd, 17);
      assert.equal(textarea.selectionDirection, "backward");
      assert.equal(ui.container.querySelector(".live-sphere"), sphere, "Keep the same network during ordinary state updates");
      assert.equal(ui.container.querySelectorAll(".live-sphere").length, 1);
    }
    await ui.render(false);
    assert.equal(ui.container.querySelector(".live-sphere"), null);
    assert.equal(ui.input.current, textarea);
    assert.equal(textarea.value, "Keep the original draft here");
    assert.equal(textarea.selectionStart, 5);
    assert.equal(ui.submissions(), 0);
  } finally { await ui.close(); }
});

test("the inset shows only current assistant speech and never copies speech into the draft", async () => {
  const ui = view();
  try {
    ui.live.state.captions = [{ id: 1, speaker: "user", text: "Please inspect these files.", start: 0, end: 100 }];
    await ui.render();
    const caption = ui.container.querySelector(".composer-voice-caption")!;
    assert.match(caption.textContent || "", /Assistant/);
    assert.match(caption.textContent || "", /Live/);
    assert.equal(caption.querySelector("p"), null, "The header is sufficient before assistant speech arrives");
    assert.doesNotMatch(caption.textContent || "", /Please inspect these files/);
    ui.live.state.captions = [
      ...ui.live.state.captions,
      { id: 2, speaker: "assistant", text: "The first spoken reply.", start: 200, end: 400 },
      { id: 3, speaker: "user", text: "Another user request.", start: 500, end: 800 },
      { id: 4, speaker: "assistant", text: "Current <script>caption</script> & reply.", start: 900, end: 1200 },
      { id: 5, speaker: "user", text: "User speech must not become assistant text.", start: 1300, end: 1500 },
    ];
    await ui.render();
    assert.match(caption.textContent || "", /Current <script>caption<\/script> & reply/);
    assert.doesNotMatch(caption.textContent || "", /first spoken reply|Another user request|User speech/);
    assert.equal(caption.querySelector("script, textarea, input, [contenteditable=true], [role=log], [aria-live=polite], [aria-live=assertive]"), null);
    assert.equal(ui.input.current?.value, "Keep the original draft here");
    assert.equal(ui.live.state.captions.length, 5, "Presentation does not consume or rewrite transcript records");
    ui.live.state.phase = "ended"; await ui.render();
    assert.match(caption.textContent || "", /Current <script>caption<\/script> & reply/);
  } finally { await ui.close(); }
});

test("a child composer hides every root voice port without replacing the root sphere or either draft", async () => {
  const ui = view();
  try {
    ui.live.state.captions = [{ id: 1, speaker: "assistant", text: "Root conversation speech.", start: 0, end: 100 }];
    await ui.render();
    const textarea = ui.input.current!;
    const sphere = ui.container.querySelector(".live-sphere")!;
    await ui.render(true, true);
    assert.equal(ui.input.current, textarea);
    assert.equal(textarea.value, "Separate note for Explorer");
    assert.equal(textarea.placeholder, "Message Explorer…");
    for (const selector of [".composer-voice-presence", ".composer-voice-caption", ".composer-voice-controls", ".composer-voice-details"]) {
      assert.ok(ui.container.querySelector(selector)?.closest("[hidden]"), `${selector} must not appear in the child conversation`);
    }
    assert.equal(ui.container.querySelector(".live-sphere"), sphere);
    assert.equal(ui.container.querySelectorAll(".live-sphere").length, 1);
    await ui.render(true, false);
    assert.equal(textarea.value, "Keep the original draft here");
    assert.equal(ui.container.querySelector(".live-sphere"), sphere);
    assert.equal(sphere.closest("[hidden]"), null);
    assert.match(ui.container.querySelector(".composer-voice-caption")?.textContent || "", /Root conversation speech/);
    assert.equal(ui.submissions(), 0);
  } finally { await ui.close(); }
});

test("inset voice controls never submit the text form, while Send still sends the unchanged draft", async () => {
  const ui = view();
  try {
    await ui.render();
    for (const selector of [".live-sphere", ".voice-mic", ".voice-speaker", ".voice-settings-trigger", ".voice-stop", ".voice-dismiss"])
      await ui.click(selector);
    assert.deepEqual(ui.actions, ["mute-input", "mute-input", "mute-output", "configure", "end", "close"]);
    assert.equal(ui.submissions(), 0);
    assert.equal(ui.input.current?.value, "Keep the original draft here");
    await act(async () => ui.container.querySelector<HTMLButtonElement>('button[type="submit"]')!.click());
    assert.equal(ui.submissions(), 1);
  } finally { await ui.close(); }
});
