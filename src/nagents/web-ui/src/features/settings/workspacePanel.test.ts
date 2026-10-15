import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ModelSelector } from "./ModelSelector.js";
import { ProvidersPanel } from "./ProvidersPanel.js";
import { SettingsDialog } from "./SettingsDialog.js";
import { settingsValues } from "./testFixtures.js";
import { useSettings } from "./useSettings.js";

function browser() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: (callback: FrameRequestCallback) => callback(0) });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  return { dom, container, root,
    button: (label: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === label)!,
    async cleanup() {
      await act(async () => root.unmount()); dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "requestAnimationFrame"]) {
        if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]); else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

const profile = { kind: "openai", auth: "codex", base_url: "", api: "auto", api_key_env: "OPENAI_API_KEY", api_version: "", scope: "https://ai.azure.com/.default" };
const registry = { revision: "connections-v1", active: "primary", providers: { primary: profile, second: { ...profile, kind: "anthropic", auth: "api-key" } },
  kinds: { openai: { label: "OpenAI", auth: ["codex", "api-key"], apis: ["auto"], env: "OPENAI_API_KEY", endpoint_required: false, version_required: false, live: true },
    anthropic: { label: "Anthropic", auth: ["api-key"], apis: ["auto"], env: "ANTHROPIC_API_KEY", endpoint_required: false, version_required: false, live: false } },
  scope: "workspace", path: "/config/providers.yaml", origins: { primary: "global", second: "workspace" }, inherited_active: true,
};
const values = settingsValues({ model: "old-model" });
const settingsReply = { values, defaults: values, revision: "settings-v1", persisted: false,
  profiles: [{ name: "assistant", mode: "build" as const, model: "" }], effective_mode: "build" as const,
  providers: ["openai", "anthropic"], apis: ["auto"], auths: ["codex", "api-key"],
  connection: { provider_id: "primary", provider: "openai", api: "auto", auth: "codex", base_url: "", api_key_env: "OPENAI_API_KEY", key_configured: false, auth_status: "configured" },
};

test("workspace panel previews a connection without mutation and guards activation against an unsaved settings draft", async t => {
  const view = browser(), writes: string[] = [];
  let active = "primary", revision = "connections-v1";
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (init.method !== "GET") writes.push(path);
    if (path === "/api/settings") return Response.json({ ...settingsReply, connection: { ...settingsReply.connection, provider_id: active } });
    if (path === "/api/provider-scopes/workspace/providers") return Response.json({ ...registry, active, revision });
    if (path.endsWith("/models")) return Response.json({ models: [path.includes("second") ? "second-model" : "old-model"], metadata: {} });
    if (path.startsWith("/api/reasoning-options")) return Response.json({ options: [] });
    if (path === "/api/provider-scopes/workspace/providers/second/activate") {
      assert.deepEqual(JSON.parse(String(init.body)), { revision: "connections-v1" });
      active = "second"; revision = "connections-v2";
      return Response.json({ ...registry, active, revision });
    }
    throw new Error(`Unexpected request: ${path}`);
  });
  function Harness() {
    const settings = useSettings({ token: "token", blocked: false, operate: async action => { await action(); return true; }, accept: () => {} });
    return createElement("div", null, createElement("button", { onClick: settings.show }, "Open"), settings.open && createElement(SettingsDialog, { settings }));
  }
  try {
    await act(async () => view.root.render(createElement(Harness)));
    await act(async () => view.button("Open").click());
    const mode = view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!;
    await act(async () => { mode.value = "interrupt"; mode.dispatchEvent(new view.dom.window.Event("change", { bubbles: true })); });
    await act(async () => [...view.container.querySelectorAll<HTMLButtonElement>(".provider-connection-select")].find(item => item.textContent?.includes("second"))!.click());
    assert.deepEqual(writes, [], "browsing never activates a provider");
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.disabled, true);
    assert.equal(view.container.querySelector<HTMLButtonElement>(".settings-actions [type=submit]")!.disabled, true);
    assert.match(view.container.textContent!, /second-model/);
    await act(async () => view.button("Use connection").click());
    assert.match(view.container.textContent!, /Discard settings draft and use connection/);
    await act(async () => view.button("Keep editing").click());
    assert.deepEqual(writes, []);
    assert.equal(view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!.value, "interrupt");
    await act(async () => view.button("Use connection").click());
    await act(async () => view.button("Discard draft and use connection").click());
    assert.deepEqual(writes, ["/api/provider-scopes/workspace/providers/second/activate"]);
    assert.equal(view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!.value, "queue");
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.disabled, false);
    assert.match(view.container.textContent!, /Using second in this workspace/);
  } finally { await view.cleanup(); }
});

test("catalog uses scoped connection metadata, keeps manual unknown IDs, and ignores late results for another route", async t => {
  const view = browser(), requested: string[] = [], chosen: string[] = [];
  let finishOld: (value: Response) => void = () => assert.fail("old request missing");
  t.mock.method(globalThis, "fetch", async (path: string) => {
    requested.push(path);
    if (path.includes("/old/")) return new Promise<Response>(resolve => { finishOld = resolve; });
    return Response.json({ models: ["new-model"], metadata: { "new-model": { context_window: 654321, context_window_source: "provider", reasoning_efforts: ["high"] } } });
  });
  const render = (provider: string) => act(async () => view.root.render(createElement(ModelSelector, { token: "token", scope: "global", provider, model: "custom-id", disabled: false, autoLoad: true, update: model => chosen.push(model) })));
  try {
    await render("old"); await render("new");
    assert.match(view.container.textContent!, /654,321 context · provider/);
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.value, "custom-id");
    await act(async () => { finishOld(Response.json({ models: ["wrong-route-model"] })); });
    assert.doesNotMatch(view.container.textContent!, /wrong-route-model/);
    await act(async () => view.container.querySelector<HTMLButtonElement>(".model-catalog-list button")!.click());
    assert.deepEqual(chosen, ["new-model"]);
    assert.deepEqual(requested, ["/api/provider-scopes/global/providers/old/models", "/api/provider-scopes/global/providers/new/models"]);
  } finally { await view.cleanup(); }
});

test("empty global connection never discovers the workspace catalog", async t => {
  const view = browser(), requested: string[] = [];
  t.mock.method(globalThis, "fetch", async (path: string) => { requested.push(path); return Response.json({ active: "" }); });
  try {
    await act(async () => view.root.render(createElement(ModelSelector, { token: "token", scope: "global", model: "manual", disabled: false, autoLoad: true, update: () => {} })));
    assert.deepEqual(requested, ["/api/provider-scopes/global/providers"]);
    assert.match(view.container.textContent!, /Select a global connection/);
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.disabled, false);
  } finally { await view.cleanup(); }
});

test("reloading a changed inherited connection clears its old capabilities even when the local revision stays the same", async t => {
  const view = browser();
  let changed = false, catalogs = 0;
  t.mock.method(globalThis, "fetch", async (path: string) => {
    if (path === "/api/settings") return Response.json(settingsReply);
    if (path === "/api/provider-scopes/workspace/providers") return Response.json({
      ...registry,
      providers: { ...registry.providers, primary: { ...profile, base_url: changed ? "https://new.example/v1" : "" } },
    });
    if (path.endsWith("/models")) {
      catalogs++;
      if (changed) throw new Error("New connection unavailable");
      return Response.json({ models: ["old-model"], metadata: { "old-model": {
        context_window: 654321, context_window_source: "provider", reasoning_efforts: ["high"],
      } } });
    }
    if (path.startsWith("/api/reasoning-options")) return Response.json({ options: [] });
    throw new Error(`Unexpected request: ${path}`);
  });
  function Harness() {
    const settings = useSettings({ token: "token", blocked: false, operate: async action => { await action(); return true; }, accept: () => {} });
    return createElement("div", null, createElement("button", { onClick: settings.show }, "Open"), settings.open && createElement(SettingsDialog, { settings }));
  }
  try {
    await act(async () => view.root.render(createElement(Harness)));
    await act(async () => view.button("Open").click());
    assert.match(view.container.querySelector(".settings-selection-summary")!.textContent!, /654,321 context · provider/);
    assert.match(view.container.querySelector("#settings-reasoning_effort")!.textContent!, /High/);
    changed = true;
    await act(async () => view.container.querySelector<HTMLButtonElement>('[aria-label="Reload connections"]')!.click());
    assert.equal(catalogs, 2);
    assert.match(view.container.textContent!, /New connection unavailable/);
    assert.doesNotMatch(view.container.textContent!, /654,321/);
    assert.doesNotMatch(view.container.querySelector("#settings-reasoning_effort")!.textContent!, /High/);
    assert.equal(view.container.querySelector<HTMLInputElement>("#settings-model")!.value, "old-model");
  } finally { await view.cleanup(); }
});

test("sign-in is offered only for the saved active workspace connection, including inherited connections", async t => {
  const view = browser(); let logins = 0;
  t.mock.method(globalThis, "fetch", async () => Response.json(registry));
  const render = (scope: "workspace" | "global") => act(async () => view.root.render(createElement(ProvidersPanel, { token: "token", scope, blocked: false, applied: () => {}, loginConnection: "primary", login: () => { logins++; } })));
  try {
    await render("workspace");
    await act(async () => [...view.container.querySelectorAll<HTMLButtonElement>(".provider-connection-select")].find(item => item.textContent?.includes("primary"))!.click());
    assert.equal(view.button("Sign in with ChatGPT").disabled, false);
    await act(async () => view.button("Sign in with ChatGPT").click());
    assert.equal(logins, 1);
    await act(async () => view.button("Add connection").click());
    assert.equal(view.button("Sign in with ChatGPT"), undefined);
    await render("global");
    await act(async () => [...view.container.querySelectorAll<HTMLButtonElement>(".provider-connection-select")].find(item => item.textContent?.includes("primary"))!.click());
    assert.equal(view.button("Sign in with ChatGPT"), undefined);
  } finally { await view.cleanup(); }
});
