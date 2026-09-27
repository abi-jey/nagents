import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ProvidersPanel } from "./ProvidersPanel.js";

test("provider editors expose scoped credential/endpoint metadata without a chat model", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const profile = {
    kind: "openai", auth: "codex", base_url: "", api: "auto", api_key_env: "OPENAI_API_KEY",
    api_version: "", scope: "https://ai.azure.com/.default",
    live: { enabled: false, model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin", backend_mode: "hosted" },
    credential_source: "Local Codex discovery; Live key $OPENAI_API_KEY or Codex discovery",
    effective_endpoint: "https://api.openai.com/v1",
    key_configured: false,
  };
  const kinds = { openai: { label: "OpenAI", auth: ["auto", "api-key", "chatgpt", "codex"], apis: ["auto", "responses"], env: "OPENAI_API_KEY", endpoint_required: false, version_required: false, live: true } };
  const registry = { revision: "a".repeat(64), active: "openai", providers: { openai: profile }, kinds };
  const paths: string[] = [];
  const dirty: boolean[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    paths.push(path);
    if (path === "/api/provider-scopes/global/providers") return Response.json({ ...registry, scope: "global", path: "/home/user/.config/ngn/providers.yaml" });
    if (path === "/api/provider-scopes/workspace/providers") return Response.json({ ...registry, scope: "workspace", path: "/home/user/.config/ngn/workspaces/work/providers.yaml", origins: { openai: "global" }, inherited_active: true, global_active: "openai" });
    if (path === "/api/provider-scopes/global/providers/openai") {
      assert.equal(init.method, "PUT");
      const body = JSON.parse(String(init.body));
      assert.deepEqual(body.profile, {
        kind: "openai", auth: "codex", base_url: "", api: "auto", api_key_env: "OPENAI_API_KEY",
        api_version: "", scope: "https://ai.azure.com/.default",
      });
      return Response.json({ ...registry, scope: "global", path: "/home/user/.config/ngn/providers.yaml" });
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
    await act(async () => root.render(createElement(ProvidersPanel, { token: "token", blocked: false, scope: "global", applied: () => {}, onDraftChange: value => dirty.push(value) })));
    assert.match(container.textContent!, /Global connections are available/);
    assert.match(container.textContent!, /\.config\/ngn\/providers.yaml/);
    await act(async () => button("openai (active)").click());
    assert.equal(container.querySelector<HTMLInputElement>('input[type="url"]'), null);
    assert.doesNotMatch(container.textContent!, /Model ID|Fetch models/);
    assert.match(container.textContent!, /Local Codex discovery/);
    assert.match(container.textContent!, /api.openai.com\/v1/);
    assert.doesNotMatch(container.textContent!, /GPT-Live settings/);
    await act(async () => button("Save connection").click());
    assert.equal(dirty.at(-1), false);
    await act(async () => root.render(createElement(ProvidersPanel, { token: "token", blocked: false, scope: "workspace", applied: () => {} })));
    await act(async () => button("openai (active)").click());
    assert.match(container.textContent!, /Edit this connection in Global settings/);
    assert.equal(container.querySelector<HTMLFieldSetElement>("fieldset.provider-fields")?.disabled, true);
    await act(async () => button("Make active").click());
    assert.match(container.textContent!, /Using openai in this workspace/);
    assert.deepEqual(paths, [
      "/api/provider-scopes/global/providers", "/api/provider-scopes/global/providers/openai",
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
