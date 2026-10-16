import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { settingsValues } from "./testFixtures.js";
import { AgentNavigator } from "./navigator/AgentNavigator.js";
import { inheritField, voiceDraft, voiceOrigin, voiceWrite } from "./navigator/state.js";
import type { AgentSettingsReply, NavigatorRequest, NavigatorVoiceReply, VoiceProfile } from "./navigator/state.js";

const voice: VoiceProfile = { enabled: true, model: { provider: "voice-provider", id: "speech-model" }, voice: "cove", instructions: "Speak calmly", context_mode: "recent", backend_mode: "assistant", backend_model: "backend-model" };
function live(scope: "global" | "workspace" | "agent", agent = ""): NavigatorVoiceReply {
  const preferences = { ...voice, model: voice.model.id, connection_id: voice.model.provider };
  return { scope, agent, values: { ...preferences, provider: "openai", base_url: "" }, effective: voice, inherited: voice, revision: `voice-${scope}-${agent}`,
    global_preferences: preferences, overrides: {}, agent_overrides: {}, definition_live: {}, saved_live_present: false, providers: ["openai"], voices: ["cove", "breeze"],
    key_configured: true, voice_auth: "chatgpt", live_supported: true, profile_name: "voice-provider", connections: [{ name: "voice-provider", provider: "openai", scope: "global" }],
    origins: { enabled: "global", model: "global", voice: "global", instructions: "global", context_mode: "global", backend_mode: "global", backend_model: "global" } };
}
function prefs(scope: "global" | "workspace" | "agent", agent = ""): AgentSettingsReply {
  return { scope, agent, revision: `prefs-${scope}-${agent}`, preferences: {}, values: { instructions: "Native guidance", tools: ["read", "write"] }, inherited: { instructions: "Native guidance", tools: ["read", "write"] },
    origins: { instructions: "definition", tools: "definition" }, definition: { instructions: "Native guidance", tools: null }, source_path: ".ngn/agents/reviewer.md", native_instructions: "Native instructions",
    workspace_instructions: [{ path: "AGENTS.md", text: "Workspace instructions" }], effective_tools: ["read"],
    profiles: [{ id: "assistant", name: "assistant", description: "Main conversation", mode: "build", source: "" }, { id: "reviewer", name: "reviewer", description: "Reviews code", mode: "reviewer", source: "reviewer.md" }, { id: "unused", name: "unused", description: "An unused predefined agent", mode: "build", source: "unused.md" }],
    tools: [{ name: "read", description: "Read files", parameters: {}, category: "files", available: true, reason: "" }, { name: "write", description: "Write files", parameters: {}, category: "files", available: false, reason: "Blocked by read-only agent mode" }] };
}
function modelReply(path: string) {
  const binding = { provider: "text-provider", id: "conversation-model" };
  return path.startsWith("/api/agent-models/")
    ? { agent: "reviewer", revision: "model-revision", model: null, definition_model: null, inherited: binding, effective: binding, source: "workspace", provider_origin: "global", source_path: "", available: true, unavailable_reason: "" }
    : { values: settingsValues({ model: binding.id }), model_binding: binding, model_binding_source: "global", connection: { provider: "openai" } };
}
function browser() {
  const dom = new JSDOM("<button id='trigger'>Settings</button><div id='root'></div>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, localStorage: dom.window.localStorage, IS_REACT_ACT_ENVIRONMENT: true,
    navigator: { mediaDevices: { enumerateDevices: async () => [], addEventListener() {}, removeEventListener() {} } } };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const root = createRoot(document.getElementById("root")!);
  const visible = <T extends HTMLElement>(selector: string) => [...document.querySelectorAll<T>(selector)].filter(element => !element.closest("[hidden]"));
  const button = (label: string) => visible<HTMLButtonElement>("button").find(element => (element.getAttribute("aria-label") || element.textContent?.trim()) === label)!;
  const render = async (request?: NavigatorRequest, catalogRevision = "") => act(async () => root.render(createElement(AgentNavigator, { request, catalogRevision, token: "token", close() {}, changed() {} })));
  return { dom, root, visible, button, render,
    click: async (label: string) => { assert.ok(button(label), `Missing button ${label}`); await act(async () => button(label).click()); },
    async close() { await act(async () => root.unmount()); dom.window.close(); for (const key of Object.keys(globals)) { if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key); } },
  };
}

test("navigator retains agent and scope voice drafts through navigation and closing, lists unused definitions", async t => {
  const ui = browser(); const writes: Record<string, unknown>[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    const url = new URL(path, "https://localhost"), scope = (url.searchParams.get("scope") || "agent") as "agent" | "global" | "workspace", agent = url.searchParams.get("agent") || "";
    if (path.startsWith("/api/agent-settings")) return Response.json(prefs(scope, agent));
    if (path.startsWith("/api/live/settings")) {
      if (init.method !== "GET") { const body = JSON.parse(String(init.body)); writes.push(body); return Response.json({ ...live(body.scope, body.agent), overrides: body.overrides, agent_overrides: body.overrides, effective: { ...voice, ...body.overrides }, saved_live_present: true }); }
      return Response.json(live(scope, agent));
    }
    if (path.startsWith("/api/agent-models/") || path === "/api/settings" || path === "/api/settings/global") return Response.json(modelReply(path));
    throw new Error(`Unexpected request ${path}`);
  });
  try {
    const opening: NavigatorRequest = { scope: "agent", agent: "reviewer", section: "Voice", serial: 1 };
    await ui.render(opening);
    assert.ok(ui.button("unused"), "Unused predefined agents belong in the hierarchy");
    assert.equal(document.querySelectorAll("dialog").length, 1);
    assert.equal(ui.visible<HTMLSelectElement>('select[aria-label="Voice provider connection"]')[0].querySelector<HTMLOptionElement>('option[value=""]')!.disabled, true, "Agent model bindings require a named saved connection");
    assert.match(ui.visible<HTMLElement>(".an-summary")[0].textContent!, /conversation-model/, "Voice entry resolves the independent conversation model without visiting Model");
    const change = async (value: string) => act(async () => { const select = ui.visible<HTMLSelectElement>('select[aria-label="Voice"]')[0]; select.value = value; select.dispatchEvent(new ui.dom.window.Event("change", { bubbles: true })); });
    await change("breeze");
    await ui.click("Global defaults");
    await change("breeze");
    await ui.click("reviewer");
    assert.equal(ui.visible<HTMLSelectElement>('select[aria-label="Voice"]')[0].value, "breeze");
    await ui.render(); assert.equal(document.querySelector<HTMLDialogElement>("dialog")!.open, false);
    await ui.render({ ...opening, serial: 2 });
    assert.equal(ui.visible<HTMLSelectElement>('select[aria-label="Voice"]')[0].value, "breeze");
    await ui.click("Save voice");
    assert.deepEqual(writes, [{ scope: "agent", agent: "reviewer", revision: "voice-agent-reviewer", overrides: { voice: "breeze" } }]);
    assert.match(document.querySelector(".an-drafts-note")!.textContent!, /1 unsaved section/, "Saving this agent must not mark the global draft as saved");
  } finally { await ui.close(); }
});

test("tool conflicts retain the selection, blocked reasons, and require explicit reload without retry", async t => {
  const ui = browser(); let writes = 0;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (path === "/api/agent-settings" && init.method === "PUT") { writes++; return Response.json({ detail: "Agent changed" }, { status: 409 }); }
    if (path.startsWith("/api/agent-settings")) return Response.json(prefs("agent", "reviewer"));
    if (path.startsWith("/api/live/settings")) return Response.json(live("agent", "reviewer"));
    if (path.startsWith("/api/agent-models/") || path === "/api/settings" || path === "/api/settings/global") return Response.json(modelReply(path));
    throw new Error(`Unexpected request ${path}`);
  });
  try {
    await ui.render({ scope: "agent", agent: "reviewer", section: "Tools", serial: 1 });
    assert.match(ui.visible<HTMLElement>(".an-tool-blocked")[0].textContent!, /read-only/);
    await act(async () => ui.visible<HTMLInputElement>('input[aria-label="Allow read"]')[0].click());
    await ui.click("Save tools");
    assert.equal(writes, 1);
    assert.equal(ui.visible<HTMLInputElement>('input[aria-label="Allow read"]')[0].checked, false);
    assert.equal(ui.button("Save tools").disabled, true);
    assert.match(ui.visible<HTMLElement>('[role="alert"]')[0].textContent!, /No request was retried/);
    await ui.click("Voice"); await ui.click("Tools");
    assert.equal(writes, 1); assert.equal(ui.button("Save tools").disabled, true);
    await ui.click("Discard instruction and tool drafts and reload");
    assert.equal(ui.visible<HTMLInputElement>('input[aria-label="Allow read"]')[0].checked, true);
    assert.equal(writes, 1);
  } finally { await ui.close(); }
});

test("voice bindings are atomic and agent empty overrides distinguish inheritance from source", () => {
  const workspace = live("workspace");
  workspace.overrides = { connection_id: "voice-provider" };
  assert.deepEqual(voiceDraft(workspace), { model: voice.model });
  assert.deepEqual(voiceWrite({ scope: "workspace", agent: "" }, "revision", { model: { provider: "new-provider", id: "new-speech" }, voice: "breeze" }), {
    scope: "workspace", revision: "revision", overrides: { voice: "breeze", connection_id: "new-provider", model: "new-speech" },
  });
  const agent = { ...live("agent", "reviewer"), agent_overrides: { voice: "breeze", model: voice.model }, saved_live_present: false };
  assert.deepEqual(voiceDraft(agent), agent.agent_overrides);
  assert.deepEqual(voiceWrite({ scope: "agent", agent: "reviewer" }, "revision", {}), { scope: "agent", agent: "reviewer", revision: "revision", overrides: {} });
  assert.deepEqual(inheritField(agent.agent_overrides, "model"), { voice: "breeze" });
});

test("global behavior saves omit an agent target and keep explicit empty tool selection", async t => {
  const ui = browser(); let written: Record<string, unknown> | undefined;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (path === "/api/agent-settings" && init.method === "PUT") {
      written = JSON.parse(String(init.body));
      assert.equal(Object.hasOwn(written!, "agent"), false);
      return Response.json({ ...prefs("global"), preferences: { tools: [] }, values: { instructions: "Native guidance", tools: [] } });
    }
    if (path.startsWith("/api/agent-settings")) return Response.json(prefs("global"));
    if (path.startsWith("/api/live/settings")) return Response.json(live("global"));
    if (path.startsWith("/api/agent-models/") || path === "/api/settings" || path === "/api/settings/global") return Response.json(modelReply(path));
    throw new Error(`Unexpected request ${path}`);
  });
  try {
    await ui.render({ scope: "global", agent: "", section: "Tools", serial: 1 });
    await ui.click("Clear selection"); await ui.click("Save tools");
    assert.deepEqual(written, { scope: "global", revision: "prefs-global-", preferences: { tools: [] } });
    assert.equal(ui.button("Save tools").disabled, true);
  } finally { await ui.close(); }
});


test("unrelated voice edits preserve automatic global and explicit workspace connection following", () => {
  const global = live("global");
  global.global_preferences = { ...global.global_preferences, connection_id: "", model: "automatic-speech" };
  global.effective = { ...voice, model: { provider: "currently-active", id: "resolved-speech" } };
  const draft = { ...voiceDraft(global), instructions: "New guidance" };
  assert.deepEqual(draft.model, { provider: "", id: "automatic-speech" });
  const globalWrite = voiceWrite({ scope: "global", agent: "" }, "revision", draft);
  assert.ok("preferences" in globalWrite);
  assert.deepEqual(globalWrite.preferences, { enabled: true, voice: "cove", instructions: "New guidance", context_mode: "recent", backend_mode: "assistant", backend_model: "backend-model", connection_id: "", model: "automatic-speech" });
  const workspace = { ...global, scope: "workspace" as const, overrides: { connection_id: "", voice: "breeze" } };
  assert.deepEqual(voiceDraft(workspace), { voice: "breeze", model: { provider: "", id: "automatic-speech" } });
});


test("resolved voice summary distinguishes authored definitions from operator overrides", () => {
  const snapshot = { ...live("agent", "reviewer"), field_sources: { voice: "definition", model: "overlay" }, origins: { voice: "agent", model: "agent" } };
  assert.equal(voiceOrigin(snapshot, "voice"), "definition");
  assert.equal(voiceOrigin(snapshot, "model"), "agent");
});


test("catalog changes expose new definitions and keep a removed agent's dirty draft under its original identity", async t => {
  const ui = browser(); let removed = false; let writes = 0;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (path.startsWith("/api/agent-settings")) {
      const reply = prefs("agent", "reviewer");
      if (removed) reply.profiles = [...reply.profiles.filter(profile => profile.id !== "reviewer"), { id: "new-agent", name: "new-agent", description: "Added externally", source: "", mode: "build" }];
      return Response.json(reply);
    }
    if (path.startsWith("/api/live/settings")) {
      if (init.method !== "GET") { writes++; return Response.json({ detail: "This agent definition is no longer available." }, { status: 404 }); }
      return Response.json(live("agent", "reviewer"));
    }
    if (path.startsWith("/api/agent-models/") || path === "/api/settings" || path === "/api/settings/global") return Response.json(modelReply(path));
    throw new Error(`Unexpected request ${path}`);
  });
  try {
    const opening: NavigatorRequest = { scope: "agent", agent: "reviewer", section: "Voice", serial: 1 };
    await ui.render(opening, "one");
    await act(async () => { const control = ui.visible<HTMLSelectElement>('select[aria-label="Voice"]')[0]; control.value = "breeze"; control.dispatchEvent(new ui.dom.window.Event("change", { bubbles: true })); });
    removed = true;
    await ui.render(opening, "two");
    assert.ok(ui.button("new-agent")); assert.ok(ui.button("reviewer (removed)"));
    assert.equal(ui.visible<HTMLSelectElement>('select[aria-label="Voice"]')[0].value, "breeze");
    await ui.click("Save voice");
    assert.equal(writes, 1);
    assert.equal(ui.button("Save voice").disabled, true);
    assert.match(ui.visible<HTMLElement>('[role="alert"]')[0].textContent!, /no longer available/);
    await ui.render(); await ui.render({ ...opening, serial: 2 }, "two");
    assert.equal(ui.visible<HTMLSelectElement>('select[aria-label="Voice"]')[0].value, "breeze");
    assert.equal(writes, 1);
  } finally { await ui.close(); }
});
