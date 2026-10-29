import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { ProvidersPanel } from "./ProvidersPanel.js";

async function editor(t: TestContext, auth: string, requestTimeout?: number, limits = { context_window: 0, model_context_windows: {} as Record<string, number> }, sessionHeader?: string) {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const profile = {
    kind: "openai", auth, base_url: "", api: "auto", api_key_env: "OPENAI_API_KEY",
    api_version: "", scope: "https://ai.azure.com/.default",
    ...limits,
    ...(requestTimeout === undefined ? {} : { request_timeout: requestTimeout }),
    ...(sessionHeader === undefined ? {} : { session_id_header: sessionHeader }),
  };
  const kinds = {
    openai: { label: "OpenAI", auth: ["auto", "api-key", "chatgpt", "codex"], apis: ["auto", "responses"], env: "OPENAI_API_KEY", endpoint_required: false, version_required: false, live: true },
    anthropic: { label: "Anthropic", auth: ["api-key"], apis: ["auto"], env: "ANTHROPIC_API_KEY", endpoint_required: false, version_required: false, live: false },
  };
  const registry = { revision: "a".repeat(64), active: "main", providers: { main: profile }, kinds, scope: "global", path: "/config/providers.yaml" };
  const saved: typeof profile[] = [];
  const dirty: boolean[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (path === "/api/provider-scopes/global/providers") return Response.json(registry);
    assert.equal(path, "/api/provider-scopes/global/providers/main");
    assert.equal(init.method, "PUT");
    const body = JSON.parse(String(init.body));
    assert.equal(body.revision, registry.revision);
    saved.push(body.profile);
    return Response.json({ ...registry, providers: { main: body.profile } });
  });
  const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(node => node.textContent?.includes(text))!;
  t.after(async () => {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  });
  await act(async () => root.render(createElement(ProvidersPanel, { token: "token", blocked: false, scope: "global", applied: () => {}, onDraftChange: value => dirty.push(value) })));
  await act(async () => button("main (active)").click());
  const input = () => container.querySelector<HTMLInputElement>('input[type="number"]')!;
  return {
    container, button, saved, dirty, input,
    async type(value: string) {
      await act(async () => {
        input().value = value;
        input().dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      });
    },
    async field(label: string, value: string, index = 0) {
      const input = [...container.querySelectorAll("label")].filter(node => node.textContent?.startsWith(label))[index].querySelector("input")!;
      await act(async () => {
        input.value = value;
        input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      });
    },
    async select(label: string, value: string) {
      const select = [...container.querySelectorAll("label")].find(node => node.textContent?.startsWith(label))!.querySelector("select")!;
      await act(async () => {
        select.value = value;
        select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      });
    },
  };
}

for (const auth of ["codex", "api-key"]) {
  for (const timeout of [undefined, 900.5]) {
    test(`${auth} connection preserves ${timeout === undefined ? "legacy default" : "explicit"} request timeout on save`, async t => {
      const view = await editor(t, auth, timeout);
      assert.equal(view.input().value, String(timeout ?? 120));
      assert.equal(view.dirty.at(-1), false);
      assert.equal(view.input().max, "", "request timeouts have no arbitrary upper cap");
      assert.equal(view.input().step, "any");
      assert.match(view.container.textContent!, /model HTTP request/);
      assert.match(view.container.textContent!, /Shell commands use their own timeout/);
      await act(async () => view.button("Save connection").click());
      assert.equal(view.saved.length, 1);
      assert.equal(view.saved[0].request_timeout, timeout ?? 120);
      assert.equal(view.saved[0].auth, auth);
      assert.equal(view.dirty.at(-1), false);
    });
  }
}

test("request timeout rejects empty, nonpositive and nonfinite input without sending a save", async t => {
  const view = await editor(t, "codex", 300);
  for (const value of ["", "0", "-1", "Infinity", "NaN", "1e309"]) {
    await view.type(value);
    assert.equal(view.input().getAttribute("aria-invalid"), "true", value);
    assert.equal(view.button("Save connection").disabled, true, value);
    const descriptionIds = view.input().getAttribute("aria-describedby")!.split(" ");
    assert.equal(descriptionIds.length, 2);
    assert.match(view.container.ownerDocument.getElementById(descriptionIds[1])!.textContent!, /greater than zero/);
    await act(async () => view.button("Save connection").click());
    assert.equal(view.saved.length, 0);
  }
  for (const value of ["0.25", "10000000000"]) {
    await view.type(value);
    assert.equal(view.input().getAttribute("aria-invalid"), "false");
    assert.equal(view.button("Save connection").disabled, false);
    assert.equal(view.dirty.at(-1), true);
    await act(async () => view.button("Save connection").click());
    assert.equal(view.saved.at(-1)!.request_timeout, Number(value));
    assert.equal(view.input().value, value);
    assert.equal(view.dirty.at(-1), false);
  }
});

test("connection and exact-model context overrides survive unrelated settings changes and save numeric values", async t => {
  const view = await editor(t, "api-key", 120, { context_window: 200000, model_context_windows: { "eu.anthropic.claude-opus-5-5": 1000000 } });
  assert.equal(view.dirty.at(-1), false);
  await view.type("180");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved[0].context_window, 200000);
  assert.deepEqual(view.saved[0].model_context_windows, { "eu.anthropic.claude-opus-5-5": 1000000 });
  await view.field("Connection input limit", "");
  await view.field("Input limit", "1200000");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.at(-1)!.context_window, 0);
  assert.deepEqual(view.saved.at(-1)!.model_context_windows, { "eu.anthropic.claude-opus-5-5": 1200000 });
});

test("context overrides reject invalid tokens and duplicate model IDs instead of silently overwriting a limit", async t => {
  const view = await editor(t, "api-key", 120);
  for (const invalid of ["-", "1e", "-1", "1.5", "1e100", "1000000001"]) {
    await view.field("Connection input limit", invalid);
    assert.equal(view.button("Save connection").disabled, true, invalid);
  }
  await view.field("Connection input limit", "0");
  await act(async () => view.button("Add model limit").click());
  assert.equal(view.button("Save connection").disabled, true);
  await view.field("Model ID", "model-a");
  await view.field("Input limit", "0");
  assert.equal(view.button("Save connection").disabled, true);
  await view.field("Input limit", "1000000");
  assert.equal(view.button("Save connection").disabled, false);
  await act(async () => view.button("Add model limit").click());
  await view.field("Model ID", "model-a", 1);
  await view.field("Input limit", "200000", 1);
  assert.equal(view.button("Save connection").disabled, true);
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.length, 0);
  await view.field("Model ID", "model-b", 1);
  await act(async () => view.button("Save connection").click());
  assert.deepEqual(view.saved[0].model_context_windows, { "model-a": 1000000, "model-b": 200000 });
});

test("authentication and provider changes retain timeout while new connections start at 120 seconds", async t => {
  const view = await editor(t, "codex", 450.5);
  await view.select("Authentication", "api-key");
  assert.equal(view.input().value, "450.5");
  await view.select("Provider type", "anthropic");
  assert.equal(view.input().value, "450.5");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved[0].kind, "anthropic");
  assert.equal(view.saved[0].request_timeout, 450.5);
  await act(async () => view.button("Add connection").click());
  assert.equal(view.input().value, "120");
  assert.equal(view.input().getAttribute("aria-invalid"), "false");
});

test("optional conversation header persists through unrelated edits, provider changes, save and clearing", async t => {
  const view = await editor(t, "api-key", 120, undefined, "x-opencode-session");
  const field = () => [...view.container.querySelectorAll("label")].find(node => node.textContent?.startsWith("Session ID header"))!.querySelector("input")!;
  assert.equal(field().value, "x-opencode-session");
  assert.equal(view.dirty.at(-1), false);
  assert.match(view.container.textContent!, /current conversation ID/);
  assert.match(view.container.textContent!, /stable across tool rounds/);
  await view.type("240");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.at(-1)!.session_id_header, "x-opencode-session");
  assert.equal(view.dirty.at(-1), false);
  await view.select("Provider type", "anthropic");
  assert.equal(field().value, "x-opencode-session");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.at(-1)!.session_id_header, "x-opencode-session");
  await view.field("Session ID header", "x-conversation-id");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.at(-1)!.session_id_header, "x-conversation-id");
  assert.equal(field().value, "x-conversation-id");
  await view.field("Session ID header", "");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.at(-1)!.session_id_header, "");
  assert.equal(view.dirty.at(-1), false);
});

test("missing conversation header defaults to empty and malformed names prevent saving", async t => {
  const view = await editor(t, "api-key");
  const field = () => [...view.container.querySelectorAll("label")].find(node => node.textContent?.startsWith("Session ID header"))!.querySelector("input")!;
  assert.equal(field().value, "");
  await act(async () => view.button("Save connection").click());
  assert.equal(view.saved.at(-1)!.session_id_header, "");
  for (const value of ["bad name", "bad:name", "x-ümlaut", "x".repeat(129)]) {
    await view.field("Session ID header", value);
    assert.equal(field().getAttribute("aria-invalid"), "true", value);
    assert.equal(view.button("Save connection").disabled, true);
  }
  assert.equal(view.saved.length, 1);
});
