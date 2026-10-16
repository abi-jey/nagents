import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { AgentModelDialog, type AgentModelReply } from "../team/AgentModelDialog.js";
import { settingsValues } from "./testFixtures.js";
import type { SettingsReply } from "./types.js";
import { useSettings } from "./useSettings.js";

function browser() {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  return { dom, root, container,
    button: (label: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === label)!,
    async close() {
      await act(async () => root.unmount()); dom.window.close();
      for (const name of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
        if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
      }
    },
  };
}

const definition = { provider: "global-openai", id: "definition-model" };
const inherited = { provider: "global-openai", id: "workspace-model" };
const agentReply: AgentModelReply = { agent: "mini", revision: "one", model: null, definition_model: definition, inherited,
  effective: definition, source: "agent", provider_origin: "global", source_path: ".ngn/agents/mini.md", available: true, unavailable_reason: "" };
const registry = { revision: "providers", providers: { "global-openai": { kind: "openai" }, "local-anthropic": { kind: "anthropic" } },
  kinds: { openai: { label: "OpenAI" }, anthropic: { label: "Anthropic" } }, origins: { "global-openai": "global", "local-anthropic": "workspace" } };

test("defined agent settings bind provider and model, distinguish explicit inheritance from restoring its definition", async t => {
  const view = browser();
  const writes: { method: string; body: { revision: string; model?: { provider: string; id: string } } }[] = [];
  let reply = agentReply, changed = 0;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (path === "/api/agent-models/mini") {
      if (init.method !== "GET") {
        const body = JSON.parse(String(init.body)); writes.push({ method: init.method!, body });
        const model = init.method === "DELETE" ? null : body.model;
        reply = { ...reply, revision: String(writes.length + 1), model, effective: model?.id ? model : model ? inherited : definition };
      }
      return Response.json(reply);
    }
    if (path === "/api/provider-scopes/workspace/providers") return Response.json(registry);
    if (path.endsWith("/models")) return Response.json({ models: [path.includes("local-anthropic") ? "new-model" : "definition-model"] });
    throw new Error(`Unexpected request: ${path}`);
  });
  try {
    await act(async () => view.root.render(createElement(AgentModelDialog, { agent: "mini", token: "token", close() {}, changed: () => { changed++; } })));
    assert.match(view.container.textContent!, /Agent configuration/);
    assert.equal(view.button("Save agent model").disabled, true);
    const provider = view.container.querySelector<HTMLSelectElement>(".model-binding-picker select")!;
    assert.match(provider.textContent!, /Global/); assert.match(provider.textContent!, /Workspace/);
    await act(async () => { provider.value = "local-anthropic"; provider.dispatchEvent(new view.dom.window.Event("change", { bubbles: true })); });
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.value, "");
    assert.equal(view.button("Save agent model").disabled, true, "Provider switching cannot reuse another connection's model");
    await act(async () => view.container.querySelector<HTMLButtonElement>(".model-catalog-list button")!.click());
    await act(async () => view.button("Save agent model").click());
    assert.deepEqual(writes[0], { method: "PUT", body: { revision: "one", model: { provider: "local-anthropic", id: "new-model" } } });
    assert.equal(changed, 1);
    await act(async () => view.container.querySelector<HTMLInputElement>('.agent-model-mode input[type="radio"]')!.click());
    await act(async () => view.button("Save agent model").click());
    assert.deepEqual(writes[1], { method: "PUT", body: { revision: "2", model: { provider: "", id: "" } } });
    await act(async () => view.button("Restore definition model").click());
    assert.deepEqual(writes[2], { method: "DELETE", body: { revision: "3" } });
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.value, "definition-model");
    assert.equal(changed, 3);
  } finally { await view.close(); }
});

test("agent model conflict keeps the pair and requires reload before another write", async t => {
  const view = browser(); let writes = 0;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (path === "/api/agent-models/mini") {
      if (init.method === "PUT") { writes++; return Response.json({ detail: "Agent changed." }, { status: 409 }); }
      return Response.json(agentReply);
    }
    if (path.endsWith("/providers")) return Response.json(registry);
    return Response.json({ models: ["another-model"] });
  });
  try {
    await act(async () => view.root.render(createElement(AgentModelDialog, { agent: "mini", token: "token", close() {}, changed() {} })));
    await act(async () => view.container.querySelector<HTMLButtonElement>(".model-catalog-list button")!.click());
    await act(async () => view.button("Save agent model").click());
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.value, "another-model");
    assert.match(view.container.textContent!, /Your draft is kept/);
    assert.equal(view.button("Save agent model").disabled, true);
    assert.equal(writes, 1);
    await act(async () => view.button("Reload saved settings").click());
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.value, "definition-model");
    assert.doesNotMatch(view.container.textContent!, /Agent changed/);
  } finally { await view.close(); }
});

test("settings treat provider-only changes as dirty and conflict on a changed pair even with the same model ID", async t => {
  const view = browser(); t.mock.timers.enable({ apis: ["setInterval"] });
  const values = settingsValues({ model: "same-model" });
  let reply: SettingsReply = { values, defaults: values, profiles: [{ name: "assistant", mode: "build", model: "" }],
    model_binding: { provider: "first", id: values.model }, revision: "same-revision", persisted: true, effective_mode: "build", providers: [], apis: [], auths: [],
    connection: { provider: "openai", api: "auto", auth: "codex", base_url: "", api_key_env: "OPENAI_API_KEY", key_configured: false, auth_status: "configured" } };
  let saved: { model_binding: { provider: string; id: string }; values: { model: string } } | undefined;
  t.mock.method(globalThis, "fetch", async (_path: string, init: RequestInit) => {
    if (init.method === "POST") { saved = JSON.parse(String(init.body)); reply = { ...reply, model_binding: saved!.model_binding }; }
    return Response.json(reply);
  });
  let settings!: ReturnType<typeof useSettings>;
  function View() { settings = useSettings({ token: "token", blocked: false, operate: async action => { await action(); return true; }, accept() {} }); return null; }
  try {
    await act(async () => view.root.render(createElement(View)));
    await act(async () => settings.show());
    await act(async () => settings.updateModel({ provider: "second", id: "same-model" }));
    assert.equal(settings.dirty, true);
    await act(async () => settings.save());
    assert.deepEqual(saved?.model_binding, { provider: "second", id: "same-model" });
    assert.equal(saved?.values.model, "same-model");
    assert.equal(settings.dirty, false);
    await act(async () => settings.updateModel({ provider: "third", id: "same-model" }));
    reply = { ...reply, model_binding: { provider: "external-change", id: "same-model" } };
    await act(async () => { t.mock.timers.tick(4000); });
    assert.equal(settings.needsRefresh, true);
    assert.equal(settings.draft?.model_provider, "third");
  } finally { t.mock.timers.reset(); await view.close(); }
});

test("embedded model refreshes clean snapshots after another section saves and preserves dirty model drafts", async t => {
  const view = browser(); let reads = 0; let reply = agentReply;
  t.mock.method(globalThis, "fetch", async (path: string) => {
    if (path === "/api/agent-models/mini") { reads++; return Response.json(reply); }
    if (path.endsWith("/providers")) return Response.json(registry);
    return Response.json({ models: ["another-model"] });
  });
  try {
    await act(async () => view.root.render(createElement(AgentModelDialog, { embedded: true, agent: "mini", token: "token", close() {}, changed() {} })));
    reply = { ...reply, revision: "after-voice" };
    await act(async () => window.dispatchEvent(new view.dom.window.Event("ngn:agent-settings-saved")));
    assert.equal(reads, 2, "Clean model snapshots refresh after other preference writes");
    await act(async () => view.container.querySelector<HTMLButtonElement>(".model-catalog-list button")!.click());
    reply = { ...reply, revision: "after-instructions" };
    await act(async () => window.dispatchEvent(new view.dom.window.Event("ngn:agent-settings-saved")));
    assert.equal(reads, 2, "Dirty model keeps its original revision for explicit conflict handling");
    assert.equal(view.container.querySelector<HTMLInputElement>('.settings-custom-model input')!.value, "another-model");
    assert.equal(view.button("Save agent model").disabled, false);
  } finally { await view.close(); }
});
