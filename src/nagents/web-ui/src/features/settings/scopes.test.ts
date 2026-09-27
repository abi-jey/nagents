import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { SessionSidebar } from "../sessions/SessionSidebar.js";
import { SettingsDialog } from "./SettingsDialog.js";
import { settingsValues } from "./testFixtures.js";
import type { SettingsReply } from "./types.js";
import { useSettings } from "./useSettings.js";

function browser() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  Object.assign(dom.window, {
    matchMedia: (media: string) => ({ media, matches: false, addEventListener() {}, removeEventListener() {} }),
  });
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, {
    window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, requestAnimationFrame: (callback: FrameRequestCallback) => callback(0),
  });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  return {
    dom, container, root,
    async cleanup() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "requestAnimationFrame"]) {
        if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
        else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

const startup = settingsValues();
const shared = settingsValues({ max_output: 25000 });
const local = settingsValues({ max_output: 18000 });
const reply: SettingsReply = {
  values: local, defaults: shared, revision: "workspace-revision", persisted: true,
  profiles: [{ name: "assistant", mode: "build", model: "" }], effective_mode: "build",
  providers: ["openai"], apis: ["auto", "responses"], auths: ["auto", "api-key"],
  connection: { provider: "openai", api: "auto", auth: "auto", base_url: "", api_key_env: "OPENAI_API_KEY", key_configured: false, auth_status: "configured" },
};
const connection = {
  kind: "openai", model: "gpt-4.1", auth: "auto", api: "auto", base_url: "", api_key_env: "OPENAI_API_KEY",
  api_version: "", scope: "https://ai.azure.com/.default",
  live: { enabled: false, model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin", backend_mode: "hosted" },
};
const registry = {
  revision: "provider-revision", active: "main", providers: { main: connection },
  kinds: { openai: { label: "OpenAI", auth: ["auto", "api-key"], apis: ["auto", "responses"], env: "OPENAI_API_KEY", endpoint_required: false, version_required: false, live: true } },
};

test("one sidebar Settings entry opens both scopes without nesting settings under workspace information", async () => {
  const view = browser();
  let opened = 0;
  const noop = () => {};
  try {
    await act(async () => view.root.render(createElement(SessionSidebar, {
      workspace: "/home/me/project", sessions: [], selected: "", disabled: false, open: true,
      select: noop, remove: noop, canDelete: () => true, permanent: noop, trash: noop,
      trashDisabled: false, settings: () => { opened++; }, settingsDisabled: false,
      channels: noop, channelsDisabled: false, demo: false, close: noop,
    })));
    assert.equal(view.container.querySelectorAll(".settings-trigger").length, 1);
    assert.equal(view.container.querySelector<HTMLButtonElement>(".settings-trigger")?.textContent?.includes("Settings"), true);
    await act(async () => view.container.querySelector<HTMLButtonElement>(".settings-trigger")!.click());
    assert.equal(opened, 1);
    await act(async () => view.container.querySelector<HTMLButtonElement>(".workspace-trigger")!.click());
    assert.match(view.container.querySelector(".workspace-dialog")!.textContent!, /Workspace folder/);
    assert.doesNotMatch(view.container.querySelector(".workspace-dialog")!.textContent!, /settings/i);
  } finally { await view.cleanup(); }
});

test("scope switch keeps conflicted drafts until confirmed, uses scoped saves and returns keyboard focus", async (t) => {
  const view = browser();
  const paths: string[] = [];
  let globalReply: SettingsReply = { ...reply, values: shared, defaults: startup, revision: "global-revision" };
  t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    paths.push(`${init.method} ${input}`);
    if (input === "/api/settings" && init.method === "GET") return Response.json(reply);
    if (input === "/api/settings" && init.method === "POST") {
      assert.equal(JSON.parse(String(init.body)).revision, reply.revision);
      return Response.json({ detail: "Settings changed in another connection." }, { status: 409 });
    }
    if (input === "/api/settings/global" && init.method === "GET") return Response.json(globalReply);
    if (input === "/api/settings/global" && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      assert.equal(body.revision, "global-revision");
      assert.equal(body.values.submit_mode, "interrupt");
      globalReply = { ...globalReply, values: body.values, revision: "global-saved" };
      return Response.json(globalReply);
    }
    if (input.startsWith("/api/provider-scopes/")) return Response.json({ ...registry, scope: input.includes("global") ? "global" : "workspace", path: "/providers.yaml", origins: { main: "global" }, inherited_active: true });
    throw new Error(`Unexpected request: ${init.method} ${input}`);
  });
  const accepted: string[] = [];
  function Harness() {
    const settings = useSettings({ token: "token", blocked: false, operate: async (action) => { await action(); return true; }, accept: (value) => { accepted.push(value.revision); } });
    return createElement("div", null,
      createElement("button", { id: "open-settings", onClick: settings.show }, "Settings"),
      settings.open && createElement(SettingsDialog, { settings }),
    );
  }
  const scope = (name: "workspace" | "global") => view.container.querySelector<HTMLButtonElement>(`[data-settings-scope="${name}"]`)!;
  const click = async (selector: string) => act(async () => view.container.querySelector<HTMLButtonElement>(selector)!.click());
  try {
    await act(async () => view.root.render(createElement(Harness)));
    view.container.querySelector<HTMLButtonElement>("#open-settings")!.focus();
    await click("#open-settings");
    assert.equal(view.dom.window.document.activeElement?.id, "settings-title");
    assert.equal(scope("workspace").getAttribute("aria-pressed"), "true");
    assert.match(view.container.querySelector("#settings-max_output-help")!.textContent!, /Workspace override · Global default: 25000/);
    assert.match(view.container.querySelector("#settings-submit-help")!.textContent!, /Inherited from Global/);
    const submit = view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!;
    await act(async () => { submit.value = "interrupt"; submit.dispatchEvent(new view.dom.window.Event("change", { bubbles: true })); });
    assert.match(view.container.textContent!, /Unsaved workspace settings/);
    scope("global").focus();
    await act(async () => scope("global").click());
    assert.match(view.container.textContent!, /Discard changes and switch to Global\?/);
    assert.equal(view.dom.window.document.activeElement?.id, "settings-confirmation-title");
    await act(async () => view.container.querySelector(".settings-dialog")!.dispatchEvent(new view.dom.window.Event("cancel", { bubbles: true, cancelable: true })));
    assert.equal(view.dom.window.document.activeElement, scope("workspace"));
    assert.equal(view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!.value, "interrupt");
    await click('button[type="submit"]');
    assert.match(view.container.textContent!, /Your draft is kept/);
    assert.match(view.container.textContent!, /Reload to check saved settings before saving or resetting/);
    assert.equal(view.container.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true);
    assert.equal(view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!.value, "interrupt");
    await act(async () => scope("global").click());
    await click(".settings-confirmation-actions button:first-child");
    assert.equal(scope("global").getAttribute("aria-pressed"), "true");
    assert.equal(view.dom.window.document.activeElement, scope("global"));
    assert.match(view.container.textContent!, /Global defaults and provider connections are shared/);
    assert.doesNotMatch(view.container.textContent!, /Workspace override · Global default/);
    const globalSubmit = view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!;
    await act(async () => { globalSubmit.value = "interrupt"; globalSubmit.dispatchEvent(new view.dom.window.Event("change", { bubbles: true })); });
    await click('button[type="submit"]');
    assert.ok(paths.includes("POST /api/settings/global"));
    assert.ok(paths.includes("GET /api/settings")); // Global save also refreshes the effective workspace snapshot.
    assert.ok(accepted.includes("workspace-revision"));
    await act(async () => scope("workspace").click());
    assert.equal(scope("workspace").getAttribute("aria-pressed"), "true");
    assert.equal(view.container.querySelector<HTMLSelectElement>("#settings-submit_mode")!.value, "queue");
    await click(".settings-actions button[type=button]");
    assert.equal(view.dom.window.document.activeElement?.id, "open-settings");
  } finally { await view.cleanup(); }
});

test("provider connections stay accessible in both scopes and unsaved connection edits require confirmation", async (t) => {
  const view = browser();
  const paths: string[] = [];
  t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    paths.push(input);
    if (input === "/api/settings") return Response.json(reply);
    if (input === "/api/settings/global") return Response.json({ ...reply, values: shared, defaults: startup });
    if (input.startsWith("/api/provider-scopes/")) return Response.json({ ...registry, scope: input.includes("global") ? "global" : "workspace", path: "/providers.yaml", origins: { main: "global" }, inherited_active: true });
    throw new Error(`Unexpected request: ${init.method} ${input}`);
  });
  function Harness() {
    const settings = useSettings({ token: "token", blocked: false, operate: async (action) => { await action(); return true; }, accept: () => {} });
    return createElement("div", null, createElement("button", { id: "open-settings", onClick: settings.show }, "Settings"),
      settings.open && createElement(SettingsDialog, { settings }));
  }
  const button = (text: string) => [...view.container.querySelectorAll<HTMLButtonElement>("button")].find((item) => item.textContent?.includes(text))!;
  try {
    await act(async () => view.root.render(createElement(Harness)));
    await act(async () => button("Settings").click());
    await act(async () => button("main (active)").click());
    assert.match(view.container.textContent!, /Edit this connection in Global settings/);
    await act(async () => button("Switch to Global settings").click());
    assert.equal(view.container.querySelector('[data-settings-scope="global"]')?.getAttribute("aria-pressed"), "true");
    assert.ok(paths.includes("/api/provider-scopes/global/providers"));
    await act(async () => button("Add connection").click());
    assert.match(view.container.textContent!, /Unsaved connection edits/);
    await act(async () => view.container.querySelector<HTMLButtonElement>('[data-settings-scope="workspace"]')!.click());
    assert.match(view.container.textContent!, /Discard changes and switch to Workspace\?/);
    await act(async () => button("Keep editing").click());
    assert.equal(view.container.querySelector('[data-settings-scope="global"]')?.getAttribute("aria-pressed"), "true");
    await act(async () => view.container.querySelector<HTMLButtonElement>('[data-settings-scope="workspace"]')!.click());
    await act(async () => button("Discard and switch").click());
    assert.equal(view.container.querySelector('[data-settings-scope="workspace"]')?.getAttribute("aria-pressed"), "true");
    assert.doesNotMatch(view.container.textContent!, /Unsaved connection edits/);
  } finally { await view.cleanup(); }
});

test("switching while workspace settings load aborts and ignores a late workspace response", async (t) => {
  const view = browser();
  let completeWorkspace: (response: Response) => void = () => assert.fail("Workspace read did not begin");
  let previousSignal: AbortSignal | undefined;
  t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    if (input === "/api/settings") {
      previousSignal = init.signal as AbortSignal;
      return new Promise<Response>((resolve) => { completeWorkspace = resolve; }); // Simulate a server ignoring abort.
    }
    if (input === "/api/settings/global") return Response.json({ ...reply, values: shared, defaults: startup, revision: "global-only" });
    if (input.startsWith("/api/provider-scopes/")) return Response.json({ ...registry, scope: "global", path: "/providers.yaml" });
    throw new Error(`Unexpected request: ${init.method} ${input}`);
  });
  const accepted: string[] = [];
  function Harness() {
    const settings = useSettings({ token: "token", blocked: false, operate: async (action) => { await action(); return true; }, accept: (value) => { accepted.push(value.revision); } });
    return createElement("div", null, createElement("button", { onClick: settings.show }, "Settings"),
      settings.open && createElement(SettingsDialog, { settings }));
  }
  try {
    await act(async () => view.root.render(createElement(Harness)));
    await act(async () => view.container.querySelector("button")!.click());
    assert.match(view.container.textContent!, /Loading current settings/);
    await act(async () => view.container.querySelector<HTMLButtonElement>('[data-settings-scope="global"]')!.click());
    assert.equal(previousSignal?.aborted, true);
    assert.match(view.container.textContent!, /Global defaults and provider connections are shared/);
    assert.doesNotMatch(view.container.textContent!, /Saved global defaults are active/);
    await act(async () => { completeWorkspace(Response.json(reply)); });
    assert.equal(view.container.querySelector('[data-settings-scope="global"]')?.getAttribute("aria-pressed"), "true");
    assert.doesNotMatch(view.container.textContent!, /Workspace override · Global default/);
    assert.deepEqual(accepted, []);
  } finally { await view.cleanup(); }
});
