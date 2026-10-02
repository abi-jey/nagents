import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LiveSettings } from "./LiveSettings.js";
import type { LiveSettingsInput, LiveSettingsSnapshot } from "./types.js";

const snapshot: LiveSettingsSnapshot = {
  values: { enabled: true, connection_id: "", backend_mode: "assistant", provider: "anthropic", model: "gpt-live-1", backend_model: "hosted-model", voice: "marin", base_url: "" },
  revision: "first", key_configured: true, providers: ["openai"], voices: ["marin", "cedar"],
  scope: "workspace", global_preferences: { enabled: true, connection_id: "", backend_mode: "assistant", model: "gpt-live-1", backend_model: "hosted-model", voice: "marin" },
  overrides: {}, origins: { enabled: "global", connection_id: "global", backend_mode: "global", model: "global", backend_model: "global", voice: "global" },
  connections: [{ name: "voice-openai", provider: "openai", scope: "global" }],
  profile_name: "chat-anthropic", live_supported: false,
};

function view() {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const label = (name: string) => [...container.querySelectorAll("label")].find(element => element.textContent?.startsWith(name));
  const select = (name: string) => label(name)?.querySelector("select")!;
  const button = (name: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(element => element.textContent?.trim() === name)!;
  const change = async (name: string, value: string) => act(async () => {
    const field = select(name); field.value = value; field.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  });
  const customize = async (name: string) => act(async () => label(`Customize ${name}`)?.querySelector<HTMLInputElement>("input")?.click());
  return { dom, root, container, label, select, button, change, customize, async close() {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    }
  } };
}

test("voice setup selects a separate connection without changing the assistant or freezing inherited preferences", async (t) => {
  const ui = view(); const writes: LiveSettingsInput[] = []; const saved: LiveSettingsSnapshot[] = [];
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    if (init.method === "POST") {
      const body = JSON.parse(String(init.body)) as LiveSettingsInput; writes.push(body);
      return Response.json({ ...snapshot, revision: "second", values: { ...snapshot.values, connection_id: "voice-openai", provider: "openai" }, profile_name: "voice-openai", live_supported: true });
    }
    return Response.json(snapshot);
  });
  try {
    await act(async () => ui.root.render(createElement(LiveSettings, { token: "token", blocked: false, back: () => {}, saved: value => saved.push(value), assistant: { agent: "main", provider: "anthropic", model: "claude" } })));
    assert.equal(ui.select("Voice connection").disabled, true);
    assert.equal(ui.label("Hosted backend model"), undefined);
    assert.match(ui.container.textContent!, /main · anthropic: claude/);
    await ui.customize("voice connection");
    assert.equal(ui.select("Voice connection").disabled, false);
    await ui.change("Voice connection", "voice-openai");
    await act(async () => ui.button("Save workspace Voice settings").click());
    assert.deepEqual(writes, [{ scope: "workspace", revision: "first", overrides: { connection_id: "voice-openai" } }]);
    assert.equal(saved[0].values.backend_mode, "assistant");
    assert.match(ui.container.textContent!, /Credentials are available on the server/);
  } finally { await ui.close(); }
});

test("hiding settings preserves the draft and a pending save disables dismissal and edits", async (t) => {
  const ui = view(); const busy: boolean[] = []; let reads = 0; let complete!: (response: Response) => void;
  const pending = new Promise<Response>(resolve => { complete = resolve; });
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    if (init.method === "POST") return pending;
    reads++; return Response.json(snapshot);
  });
  const props = { token: "token", blocked: false, back: () => {}, saved: () => {}, onSavingChange: (value: boolean) => { busy.push(value); } };
  try {
    await act(async () => ui.root.render(createElement(LiveSettings, props)));
    await ui.customize("default voice"); await ui.change("Default voice", "cedar");
    await act(async () => ui.root.render(createElement(LiveSettings, { ...props, hidden: true })));
    assert.equal(ui.container.querySelector("section")?.hidden, true);
    await act(async () => ui.root.render(createElement(LiveSettings, props)));
    assert.equal(reads, 1); assert.equal(ui.select("Default voice").value, "cedar");
    await act(async () => ui.button("Save workspace Voice settings").click());
    assert.equal(busy.at(-1), true);
    assert.equal(ui.container.querySelector("fieldset")?.disabled, true);
    assert.equal(ui.container.querySelector<HTMLButtonElement>('[aria-label="Back to conversation"]')?.disabled, true);
    await act(async () => complete(Response.json(snapshot)));
    assert.equal(busy.at(-1), false);
  } finally { await ui.close(); }
});

test("setup keeps unavailable saved connections visible and only shows hosted model controls in hosted mode", async (t) => {
  const ui = view();
  t.mock.method(globalThis, "fetch", async () => Response.json({ ...snapshot, values: { ...snapshot.values, connection_id: "removed-connection" }, overrides: { connection_id: "removed-connection", backend_mode: "assistant" } }));
  try {
    await act(async () => ui.root.render(createElement(LiveSettings, { token: "token", blocked: false, back: () => {}, saved: () => {} })));
    assert.equal(ui.select("Voice connection").selectedOptions[0].textContent, "removed-connection · unavailable");
    await ui.change("Reasoning backend", "hosted");
    assert.equal(ui.label("Hosted backend model")?.querySelector("input")?.value, "hosted-model");
    await ui.change("Reasoning backend", "assistant");
    assert.equal(ui.label("Hosted backend model"), undefined);
    assert.match(ui.container.textContent!, /selected chat's history/);
  } finally { await ui.close(); }
});

test("ChatGPT voice setup identifies the existing login and restricts new hosted backend selections", async (t) => {
  const ui = view();
  const login: LiveSettingsSnapshot = {
    ...snapshot, voice_auth: "chatgpt", profile_name: "chatgpt", live_supported: true,
    values: { ...snapshot.values, provider: "openai", model: "gpt-live-1-codex", backend_mode: "hosted" },
    overrides: { backend_mode: "hosted" }, voices: ["cedar"],
  };
  t.mock.method(globalThis, "fetch", async () => Response.json(login));
  try {
    await act(async () => ui.root.render(createElement(LiveSettings, { token: "token", blocked: false, back: () => {}, saved: () => {} })));
    assert.match(ui.container.textContent!, /Uses your existing ChatGPT login on the server/);
    assert.equal(ui.select("Reasoning backend").querySelector<HTMLOptionElement>('option[value="hosted"]')?.disabled, true);
    assert.equal(ui.select("Reasoning backend").disabled, false, "a saved hosted configuration can still be corrected to main assistant");
    await ui.change("Reasoning backend", "assistant");
    assert.equal(ui.label("Hosted backend model"), undefined);
    assert.equal(ui.select("Default voice").selectedOptions[0].textContent, "marin · unavailable for this connection");
    assert.deepEqual([...ui.select("Default voice").options].map(option => option.value), ["marin", "cedar"]);
  } finally { await ui.close(); }
});
