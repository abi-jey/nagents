import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { dictationConfig } from "../dictation/testFixtures.js";
import { SettingsDialog } from "./SettingsDialog.js";
import { settingsValues } from "./testFixtures.js";
import type { SettingsReply } from "./types.js";
import { useSettings } from "./useSettings.js";

function reply(revision: string, model = "original"): SettingsReply {
  const values = settingsValues({ model });
  return {
    values, defaults: values, revision, persisted: true,
    profiles: [{ name: "assistant", mode: "build", model: "" }],
    effective_mode: "build", providers: ["mock"], apis: ["auto"], auths: ["auto"],
    connection: { provider: "mock", api: "auto", auth: "auto", base_url: "", api_key_env: "MOCK_API_KEY", key_configured: false, auth_status: "configured" },
    dictation: dictationConfig,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>((done) => { resolve = done; }), resolve };
}

test("polls both settings scopes only while open, updates clean drafts without loading, and ignores late responses", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  t.mock.timers.enable({ apis: ["setInterval"] });
  const requests: { path: string; signal: AbortSignal; response: ReturnType<typeof deferred<Response>> }[] = [];
  t.mock.method(globalThis, "fetch", (path: string, init: RequestInit) => {
    const response = deferred<Response>();
    requests.push({ path, signal: init.signal!, response });
    return response.promise;
  });
  const root = createRoot(dom.window.document.getElementById("root")!);
  let mounted = true;
  let settings!: ReturnType<typeof useSettings>;
  function View() {
    settings = useSettings({ token: "token", blocked: false, operate: async (run) => { await run(); return true; }, accept: () => {} });
    return null;
  }
  const tick = async () => act(async () => { t.mock.timers.tick(4000); });
  const respond = async (index: number, value: SettingsReply) => act(async () => { requests[index].response.resolve(Response.json(value)); });
  try {
    await act(async () => root.render(createElement(View)));
    await tick();
    assert.equal(requests.length, 0);
    await act(async () => settings.show());
    assert.equal(requests[0].path, "/api/settings");
    await respond(0, reply("workspace-1"));
    assert.equal(settings.loading, false);

    await tick();
    assert.equal(requests[1].path, "/api/settings");
    assert.equal(settings.loading, false);
    await tick();
    assert.equal(requests.length, 2, "an in-flight poll must not overlap another read");
    await respond(1, reply("workspace-2", "updated"));
    assert.equal(settings.draft?.model, "updated");
    assert.equal(settings.snapshot?.revision, "workspace-2");
    assert.equal(settings.loading, false);

    await tick();
    assert.equal(requests.length, 3);
    await act(async () => settings.close());
    assert.equal(requests[2].signal.aborted, true);
    await respond(2, reply("late-workspace", "stale"));
    await tick();
    assert.equal(requests.length, 3, "closing removes the polling timer");
    assert.equal(settings.snapshot?.revision, "workspace-2");

    await act(async () => settings.showGlobal());
    assert.equal(requests[3].path, "/api/settings/global");
    await respond(3, reply("global-1"));
    await tick();
    assert.equal(requests[4].path, "/api/settings/global");
    await respond(4, reply("global-2", "global-update"));
    assert.equal(settings.draft?.model, "global-update");
    await tick();
    assert.equal(requests[5].path, "/api/settings/global");
    await act(async () => settings.switchScope("workspace"));
    assert.equal(requests[5].signal.aborted, true, "switching aborts an in-flight poll");
    assert.equal(requests[6].path, "/api/settings");
    await respond(6, reply("workspace-3"));
    await respond(5, reply("late-global", "stale"));
    assert.equal(settings.scope, "workspace");
    assert.equal(settings.snapshot?.revision, "workspace-3");
    assert.equal(settings.draft?.model, "original");
    await act(async () => root.unmount());
    mounted = false;
    await tick();
    assert.equal(requests.length, 7, "unmount removes the polling timer");
  } finally {
    if (mounted) await act(async () => root.unmount());
    t.mock.timers.reset();
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});

test("background reads retain drafts and errors, require explicit recovery on conflicts, and never clear an uncertain write", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  t.mock.timers.enable({ apis: ["setInterval"] });
  const requests: { path: string; method: string; signal?: AbortSignal; response: ReturnType<typeof deferred<Response>> }[] = [];
  t.mock.method(globalThis, "fetch", (path: string, init: RequestInit) => {
    const response = deferred<Response>();
    requests.push({ path, method: init.method!, signal: init.signal ?? undefined, response });
    return response.promise;
  });
  const root = createRoot(dom.window.document.getElementById("root")!);
  let settings!: ReturnType<typeof useSettings>;
  function View() {
    settings = useSettings({ token: "token", blocked: false, operate: async (run) => { await run(); return true; }, accept: () => {} });
    return null;
  }
  const tick = async () => act(async () => { t.mock.timers.tick(4000); });
  const respond = async (index: number, value: Response) => act(async () => { requests[index].response.resolve(value); });
  try {
    await act(async () => root.render(createElement(View)));
    await act(async () => settings.showGlobal());
    await respond(0, Response.json(reply("one")));
    await act(async () => settings.update("model", ""));
    await act(async () => { await settings.save(); });
    assert.ok(settings.errors.model);
    await tick();
    await respond(1, Response.json(reply("one")));
    assert.ok(settings.errors.model, "a background read must not erase validation errors");
    assert.equal(settings.draft?.model, "");
    await act(async () => settings.update("model", "my draft"));
    await tick();
    await respond(2, Response.json(reply("one")));
    assert.equal(settings.draft?.model, "my draft");
    assert.equal(settings.needsRefresh, false);

    await tick();
    await respond(3, Response.json(reply("one", "their edit")));
    assert.equal(settings.draft?.model, "my draft");
    assert.equal(settings.snapshot?.revision, "one");
    assert.equal(settings.changedElsewhere, true, "projected provider values can change without a revision bump");
    assert.equal(settings.needsRefresh, true);
    await tick();
    assert.equal(requests.length, 4, "recovery waits for an explicit reload");
    let reloading!: Promise<void>;
    await act(async () => { reloading = settings.reload(); });
    assert.equal(settings.draft?.model, "my draft", "reload keeps the draft until the read succeeds");
    await respond(4, Response.json(reply("two", "their edit")));
    await reloading;
    assert.equal(settings.draft?.model, "their edit");
    assert.equal(settings.needsRefresh, false);

    await tick();
    assert.equal(requests[5].method, "GET");
    await act(async () => settings.update("model", "next draft"));
    let saving!: Promise<void>;
    await act(async () => { saving = settings.save(); });
    assert.equal(requests[5].signal?.aborted, true, "a write invalidates an in-flight background read");
    assert.equal(requests[6].method, "POST");
    await respond(5, Response.json(reply("stale", "must not replace draft")));
    assert.equal(settings.draft?.model, "next draft");
    await tick();
    assert.equal(requests.length, 7, "polling skips pending writes");
    await respond(6, Response.json({ detail: "Settings changed." }, { status: 409 }));
    await saving;
    assert.equal(settings.draft?.model, "next draft");
    assert.equal(settings.needsRefresh, true);
    assert.match(settings.error, /draft is kept/);
    await tick();
    assert.equal(requests.length, 7);
    await act(async () => { reloading = settings.reload(); });
    await respond(7, Response.json(reply("three", "saved externally")));
    await reloading;
    assert.equal(settings.draft?.model, "saved externally");
    assert.equal(settings.error, "");

    await act(async () => settings.update("model", "unknown write"));
    await act(async () => { saving = settings.save(); });
    await act(async () => { requests[8].response.resolve(new Response("failure", { status: 503 })); });
    await saving;
    assert.match(settings.error, /outcome is unknown/);
    assert.equal(settings.draft?.model, "unknown write");
    await tick();
    assert.equal(requests.length, 9, "uncertain writes must not be auto-acknowledged");
  } finally {
    await act(async () => root.unmount());
    t.mock.timers.reset();
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});

test("the dialog offers a reload only for recovery and confirms before discarding a draft", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, {
    window: dom.window, document: dom.window.document,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true,
  });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  t.mock.timers.enable({ apis: ["setInterval"] });
  let saved = reply("first");
  t.mock.method(globalThis, "fetch", async (path: string) => {
    if (path === "/api/settings") return Response.json(saved);
    if (path === "/api/provider-scopes/workspace/providers") return Response.json({
      revision: "registry", active: "", providers: {}, kinds: {}, scope: "workspace", path: "providers.yaml",
    });
    throw new Error(`Unexpected request: ${path}`);
  });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  let settings!: ReturnType<typeof useSettings>;
  function View() {
    settings = useSettings({ token: "token", blocked: false, operate: async (run) => { await run(); return true; }, accept: () => {} });
    return settings.open ? createElement(SettingsDialog, { settings }) : null;
  }
  const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find((element) => element.textContent?.trim() === label);
  try {
    await act(async () => root.render(createElement(View)));
    await act(async () => settings.show());
    assert.equal(button("Reload saved settings"), undefined);
    assert.doesNotMatch(container.textContent!, /Saved workspace overrides are active|Refresh settings/);

    const field = container.querySelector<HTMLSelectElement>("#settings-submit_mode")!;
    field.focus();
    await act(async () => settings.update("submit_mode", "interrupt"));
    saved = reply("second", "changed elsewhere");
    await act(async () => { t.mock.timers.tick(4000); });
    assert.equal(dom.window.document.activeElement, field, "background checks do not steal editor focus");
    assert.ok(button("Reload saved settings"));
    assert.equal(settings.draft?.submit_mode, "interrupt");

    await act(async () => button("Reload saved settings")!.click());
    assert.match(container.textContent!, /Discard draft and reload\?/);
    assert.equal(dom.window.document.activeElement?.textContent, "Discard draft and reload?");
    await act(async () => button("Keep draft")!.click());
    assert.equal(settings.draft?.submit_mode, "interrupt");
    await act(async () => button("Reload saved settings")!.click());
    await act(async () => button("Discard draft and reload")!.click());
    assert.equal(settings.draft?.model, "changed elsewhere");
    assert.equal(button("Reload saved settings"), undefined);
  } finally {
    await act(async () => root.unmount());
    t.mock.timers.reset();
    dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
