import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ProvidersPanel } from "./ProvidersPanel.js";

test("global and workspace provider editors use their scoped YAML and the mounted model picker", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const profile = {
    kind: "openai", model: "gpt-4.1", auth: "codex", base_url: "", api: "auto", api_key_env: "OPENAI_API_KEY",
    api_version: "", scope: "https://ai.azure.com/.default",
    live: { enabled: false, model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin", backend_mode: "hosted" },
  };
  const kinds = { openai: { label: "OpenAI", auth: ["auto", "api-key", "chatgpt", "codex"], apis: ["auto", "responses"], env: "OPENAI_API_KEY", endpoint_required: false, version_required: false, live: true } };
  const registry = { revision: "a".repeat(64), active: "openai", providers: { openai: profile }, kinds };
  const paths: string[] = [];
  let dirty = false;
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    paths.push(path);
    if (path === "/api/provider-scopes/global/providers") return Response.json({ ...registry, scope: "global", path: "/home/user/.config/ngn/providers.yaml" });
    if (path === "/api/provider-scopes/workspace/providers") return Response.json({ ...registry, scope: "workspace", path: "/home/user/.config/ngn/workspaces/work/providers.yaml", origins: { openai: "global" }, inherited_active: true, global_active: "openai" });
    if (path === "/api/providers/openai/models") return Response.json({ models: ["gpt-new"], source: "openai" });
    if (path === "/api/provider-scopes/global/providers/openai" && init.method === "PUT") {
      const body = JSON.parse(String(init.body));
      assert.equal(body.revision, registry.revision);
      assert.deepEqual(Object.keys(body.profile).sort(), ["kind", "model", "auth", "base_url", "api", "api_key_env", "api_version", "scope"].sort());
      assert.equal(body.profile.model, "gpt-new");
      return Response.json({ ...registry, revision: "b".repeat(64), providers: { openai: { ...profile, model: "gpt-new" } } });
    }
    if (path === "/api/provider-scopes/workspace/providers/openai/activate") {
      assert.equal(init.method, "POST");
      assert.deepEqual(JSON.parse(String(init.body)), { revision: registry.revision });
      return Response.json({ ...registry, scope: "workspace", path: "/home/user/.config/ngn/workspaces/work/providers.yaml", origins: { openai: "global" }, inherited_active: false, global_active: "openai" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });
  const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find((node) => node.textContent?.includes(text))!;
  try {
    await act(async () => root.render(createElement(ProvidersPanel, { token: "token", blocked: false, initialModel: "gpt-4.1", scope: "global", applied: () => {}, onDraftChange: (value) => { dirty = value; } })));
    assert.match(container.textContent!, /Global connections are available/);
    assert.match(container.textContent!, /\.config\/ngn\/providers.yaml/);
    await act(async () => button("openai (active)").click());
    assert.equal(container.querySelector<HTMLInputElement>('input[type="url"]'), null);
    await act(async () => button("Fetch models").click());
    await act(async () => button("gpt-new").click());
    assert.equal([...container.querySelectorAll<HTMLInputElement>("input")].find((input) => input.value === "gpt-new")?.value, "gpt-new");
    assert.equal(dirty, true);
    assert.doesNotMatch(container.textContent!, /GPT-Live settings for this provider/);
    await act(async () => button("Save connection").click());
    assert.equal(dirty, false, "a saved connection no longer blocks scope switching");
    await act(async () => root.render(createElement(ProvidersPanel, { token: "token", blocked: false, initialModel: "gpt-4.1", scope: "workspace", applied: () => {} })));
    await act(async () => button("openai (active)").click());
    assert.match(container.textContent!, /Edit this connection in Global settings/);
    assert.equal(container.querySelector<HTMLFieldSetElement>("fieldset.provider-fields")?.disabled, true);
    await act(async () => button("Make active").click());
    assert.match(container.textContent!, /Using openai in this workspace/);
    assert.deepEqual(paths, [
      "/api/provider-scopes/global/providers", "/api/providers/openai/models",
      "/api/provider-scopes/global/providers/openai",
      "/api/provider-scopes/workspace/providers", "/api/provider-scopes/workspace/providers/openai/activate",
    ]);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
