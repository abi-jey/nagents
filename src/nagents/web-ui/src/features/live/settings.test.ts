import assert from "node:assert/strict";
import test from "node:test";
import { RequestError } from "../../api/client.js";
import { liveApi } from "./api.js";
import { LiveSettingsController, settingsApi, settingsValidation } from "./settings.js";
import type { LiveSettingsInput, LiveSettingsSnapshot } from "./types.js";

const settings: LiveSettingsSnapshot = {
  values: { enabled: false, backend_mode: "assistant", provider: "openai", model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin", base_url: "" },
  revision: "a".repeat(64), key_configured: false, providers: ["openai", "openai_compatible", "azure_openai_compatible_v1"], voices: ["marin", "cedar"],
  scope: "workspace", global_preferences: { enabled: false, backend_mode: "assistant", model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin" },
  overrides: {}, origins: { enabled: "global", backend_mode: "global", model: "global", backend_model: "global", voice: "global" },
};
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("UI settings save the exact revision and voice backend without sending credentials", async () => {
  const writes: LiveSettingsInput[] = [];
  const controller = new LiveSettingsController({ read: async () => settings, save: async (body) => {
    writes.push(body); return { ...settings, values: { ...settings.values, ...("overrides" in body ? body.overrides : body.preferences) }, revision: "b".repeat(64), key_configured: true };
  } });
  await controller.load();
  controller.override("enabled", true); controller.override("model", true); controller.override("voice", true); controller.override("backend_mode", true);
  controller.edit({ enabled: true, model: "my-live-deployment", voice: "cedar", backend_mode: "hosted" });
  const saved = await controller.save();
  assert.equal(writes.length, 1);
  assert.deepEqual(Object.keys(writes[0]).sort(), ["overrides", "revision", "scope"]);
  assert.equal(writes[0].revision, settings.revision);
  assert.deepEqual("overrides" in writes[0] && writes[0].overrides, { enabled: true, model: "my-live-deployment", voice: "cedar", backend_mode: "hosted" });
  assert.equal(saved?.values.enabled, true); assert.equal(saved?.values.voice, "cedar");
  assert.equal(saved?.values.backend_mode, "hosted");
  controller.dispose();
});

test("global and workspace drafts select independent scopes and inherit unselected fields", async () => {
  const global: LiveSettingsSnapshot = { ...settings, scope: "global", overrides: {}, values: { ...settings.values, model: "global-model" }, global_preferences: { ...settings.global_preferences, model: "global-model" } };
  const writes: LiveSettingsInput[] = [];
  const controller = new LiveSettingsController({ read: async scope => scope === "global" ? global : settings, save: async body => { writes.push(body); return body.scope === "global" ? global : settings; } });
  await controller.load();
  controller.override("model", true); controller.edit({ model: "local-model" });
  assert.equal(controller.getSnapshot().overrides.model, "local-model");
  controller.setScope("global"); assert.equal(controller.getSnapshot().scope, "workspace");
  controller.override("model", false); assert.equal(controller.getSnapshot().values?.model, "gpt-live-1");
  await controller.save(); assert.deepEqual(writes[0], { scope: "workspace", revision: settings.revision, overrides: {} });
  controller.setScope("global"); await new Promise(resolve => setTimeout(resolve, 0));
  controller.edit({ model: "new-global-model" }); await controller.save();
  assert.deepEqual(writes[1], { scope: "global", revision: global.revision, preferences: { ...global.global_preferences, model: "new-global-model" } });
  controller.dispose();
});

test("stale revisions and uncertain writes cannot be silently retried or overwrite another tab", async () => {
  for (const error of [new RequestError("Settings changed. Reload.", 409), new Error("Network failed")]) {
    let writes = 0;
    const controller = new LiveSettingsController({ read: async () => settings, save: async () => { writes++; throw error; } });
    await controller.load(); controller.override("voice", true); controller.edit({ voice: "cedar" }); await controller.save();
    assert.equal(controller.getSnapshot().needsRefresh, true); assert.equal(controller.getSnapshot().values?.voice, "cedar");
    await controller.save(); assert.equal(writes, 1);
    await controller.load(); assert.equal(controller.getSnapshot().needsRefresh, false); assert.equal(controller.getSnapshot().values?.voice, "marin");
    controller.dispose();
  }
});

test("a pending save is single-flight and cannot be overwritten by edits or a concurrent reload", async () => {
  const pending = deferred<LiveSettingsSnapshot>(); let writes = 0, reads = 0;
  const controller = new LiveSettingsController({ read: async () => { reads++; return settings; }, save: async () => { writes++; return pending.promise; } });
  await controller.load(); controller.override("enabled", true); controller.edit({ enabled: true });
  const saved = controller.save(); await controller.save(); await controller.load(); controller.edit({ enabled: false });
  assert.equal(reads, 1); assert.equal(writes, 1); assert.equal(controller.getSnapshot().values?.enabled, true);
  pending.resolve({ ...settings, values: { ...settings.values, enabled: true } }); await saved; controller.dispose();
});

test("closing the settings pane ignores late reads and writes", async () => {
  const pending = deferred<LiveSettingsSnapshot>();
  const controller = new LiveSettingsController({ read: async () => settings, save: async () => pending.promise });
  await controller.load(); const saved = controller.save(); controller.dispose();
  pending.resolve(settings); assert.equal(await saved, undefined);
  const late = deferred<LiveSettingsSnapshot>();
  const loading = new LiveSettingsController({ read: async () => late.promise, save: async () => settings });
  const read = loading.load(); loading.dispose(); late.resolve(settings); await read;
  assert.equal(loading.getSnapshot().snapshot, undefined);
});

test("invalid voice model stops before a settings write", async () => {
  let writes = 0;
  const controller = new LiveSettingsController({ read: async () => settings, save: async () => { writes++; return settings; } });
  await controller.load(); controller.override("model", true); controller.edit({ model: "bad model" }); await controller.save(); assert.equal(writes, 0);
  controller.dispose();
});

test("settings and session transport use same-origin authenticated routes with a captured revision", async () => {
  const original = globalThis.fetch;
  const requests: { path: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (input, init) => { requests.push({ path: String(input), init }); return Response.json(settings); };
  try {
    const api = settingsApi("local-web-token"); const signal = new AbortController().signal;
    await api.read("workspace", signal);
    const body: LiveSettingsInput = { scope: "workspace", revision: settings.revision, overrides: {} };
    await api.save(body); await liveApi("local-web-token").create("marin", signal, settings.revision, "ngn-chat-root");
    assert.deepEqual(requests.map((r) => r.path), ["/api/live/settings?scope=workspace", "/api/live/settings", "/api/live/sessions"]);
    for (const { init } of requests) { assert.equal(new Headers(init?.headers).get("X-Ngn-Token"), "local-web-token"); assert.equal(init?.credentials, "same-origin"); }
    assert.deepEqual(JSON.parse(String(requests[1].init?.body)), body);
    assert.deepEqual(JSON.parse(String(requests[2].init?.body)), { voice: "marin", revision: settings.revision, session_id: "ngn-chat-root" });
    await liveApi("local-web-token").create("marin", signal, settings.revision, "ngn-chat-root", "offer-sdp");
    assert.equal(requests[3].path, "/api/live/sessions");
    assert.deepEqual(JSON.parse(String(requests[3].init?.body)), { voice: "marin", revision: settings.revision, session_id: "ngn-chat-root", sdp: "offer-sdp" });
  } finally { globalThis.fetch = original; }
});
