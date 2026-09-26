import assert from "node:assert/strict";
import test from "node:test";
import { RequestError } from "../../api/client.js";
import { liveApi } from "./api.js";
import { connectionChanged, LiveSettingsController, settingsApi, settingsValidation } from "./settings.js";
import type { LiveSettingsInput, LiveSettingsSnapshot } from "./types.js";

const settings: LiveSettingsSnapshot = {
  values: { enabled: false, provider: "openai", model: "gpt-live-1", backend_model: "gpt-5.6-luna", voice: "marin", base_url: "" },
  revision: "a".repeat(64), key_configured: false, providers: ["openai", "openai_compatible", "azure_openai_compatible_v1"], voices: ["marin", "cedar"],
};
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("UI settings save the exact revision and write-only key, then clear the secret from the form", async () => {
  const writes: LiveSettingsInput[] = [];
  const controller = new LiveSettingsController({ read: async () => settings, save: async (body) => {
    writes.push(body); return { ...settings, values: body.values, revision: "b".repeat(64), key_configured: true };
  } });
  await controller.load();
  controller.edit({ enabled: true, model: "my-live-deployment", voice: "cedar" }); controller.key("test-only-key");
  const saved = await controller.save();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].api_key, "test-only-key"); assert.equal(writes[0].revision, settings.revision);
  assert.equal(saved?.values.enabled, true); assert.equal(saved?.values.voice, "cedar");
  assert.equal(controller.getSnapshot().apiKey, ""); assert.equal(controller.getSnapshot().clearKey, false);
  assert.equal(JSON.stringify(saved).includes("test-only-key"), false);
  controller.dispose();
});

test("retaining, replacing and removing saved credentials are distinct explicit requests", async () => {
  const writes: LiveSettingsInput[] = [];
  const controller = new LiveSettingsController({ read: async () => ({ ...settings, key_configured: true }), save: async (body) => {
    writes.push(body); return { ...settings, values: body.values, key_configured: !body.clear_api_key };
  } });
  await controller.load(); await controller.save();
  controller.key("replacement-key"); await controller.save();
  controller.key("abandoned-draft-key"); controller.clearKey(true); await controller.save();
  assert.deepEqual(writes.map((body) => [body.api_key, body.clear_api_key]), [["", false], ["replacement-key", false], ["", true]]);
  assert.equal(controller.getSnapshot().snapshot?.key_configured, false); controller.dispose();
});

test("stale revisions and uncertain writes cannot be silently retried or overwrite another tab", async () => {
  for (const error of [new RequestError("Settings changed. Reload.", 409), new Error("Network failed")]) {
    let writes = 0;
    const controller = new LiveSettingsController({ read: async () => settings, save: async () => { writes++; throw error; } });
    await controller.load(); controller.key("retain-draft-key"); await controller.save();
    assert.equal(controller.getSnapshot().needsRefresh, true); assert.equal(controller.getSnapshot().apiKey, "retain-draft-key");
    await controller.save(); assert.equal(writes, 1);
    await controller.load(); assert.equal(controller.getSnapshot().needsRefresh, false); assert.equal(controller.getSnapshot().apiKey, "");
    controller.dispose();
  }
});

test("a pending save is single-flight and cannot be overwritten by edits or a concurrent reload", async () => {
  const pending = deferred<LiveSettingsSnapshot>(); let writes = 0, reads = 0;
  const controller = new LiveSettingsController({ read: async () => { reads++; return settings; }, save: async () => { writes++; return pending.promise; } });
  await controller.load(); controller.edit({ enabled: true });
  const saved = controller.save(); await controller.save(); await controller.load(); controller.edit({ enabled: false }); controller.key("ignored");
  assert.equal(reads, 1); assert.equal(writes, 1); assert.equal(controller.getSnapshot().values?.enabled, true); assert.equal(controller.getSnapshot().apiKey, "");
  pending.resolve({ ...settings, values: { ...settings.values, enabled: true } }); await saved; controller.dispose();
});

test("closing the settings pane drops credentials and ignores late reads and writes", async () => {
  const pending = deferred<LiveSettingsSnapshot>();
  const controller = new LiveSettingsController({ read: async () => settings, save: async () => pending.promise });
  await controller.load(); controller.key("temporary-key"); const saved = controller.save(); controller.dispose();
  assert.equal(controller.getSnapshot().apiKey, ""); pending.resolve(settings); assert.equal(await saved, undefined);
  const late = deferred<LiveSettingsSnapshot>();
  const loading = new LiveSettingsController({ read: async () => late.promise, save: async () => settings });
  const read = loading.load(); loading.dispose(); late.resolve(settings); await read;
  assert.equal(loading.getSnapshot().snapshot, undefined);
});

test("provider and endpoint identity decide whether a saved key belongs to the edited connection", () => {
  assert.equal(connectionChanged(settings.values, { ...settings.values, model: "different" }), false);
  assert.equal(connectionChanged(settings.values, { ...settings.values, base_url: "https://api.openai.com/v1/" }), false);
  assert.equal(connectionChanged(settings.values, { ...settings.values, provider: "openai_compatible" }), true);
  assert.equal(connectionChanged(settings.values, { ...settings.values, base_url: "https://another.example/v1" }), true);
  const azure = { ...settings.values, provider: "azure_openai_compatible_v1", base_url: "https://azure.example/openai" };
  assert.equal(connectionChanged(azure, { ...azure, base_url: "https://azure.example:443/openai/v1/" }), false);
});

test("invalid key/model/endpoints stop before a settings write and Azure requires a prefix", async () => {
  assert.match(settingsValidation({ ...settings.values, provider: "azure_openai_compatible_v1" }, ""), /Azure/);
  for (const base_url of ["http://remote.example/v1", "https://user:secret@host.example/v1", "https://host.example/v1?secret=x"])
    assert.notEqual(settingsValidation({ ...settings.values, base_url }, ""), "");
  assert.equal(settingsValidation({ ...settings.values, base_url: "http://127.0.0.1:9999/v1" }, ""), "");
  let writes = 0;
  const controller = new LiveSettingsController({ read: async () => settings, save: async () => { writes++; return settings; } });
  await controller.load(); controller.key("invalid key"); await controller.save(); assert.equal(writes, 0);
  controller.key(""); controller.edit({ model: "bad model" }); await controller.save(); assert.equal(writes, 0);
  controller.dispose();
});

test("settings and session transport use same-origin authenticated routes with a captured revision", async () => {
  const original = globalThis.fetch;
  const requests: { path: string; init?: RequestInit }[] = [];
  globalThis.fetch = async (input, init) => { requests.push({ path: String(input), init }); return Response.json(settings); };
  try {
    const api = settingsApi("local-web-token"); const signal = new AbortController().signal;
    await api.read(signal);
    const body: LiveSettingsInput = { revision: settings.revision, values: settings.values, api_key: "test-key", clear_api_key: false };
    await api.save(body); await liveApi("local-web-token").create("offer", "marin", signal, settings.revision);
    assert.deepEqual(requests.map((r) => r.path), ["/api/live/settings", "/api/live/settings", "/api/live/sessions"]);
    for (const { init } of requests) { assert.equal(new Headers(init?.headers).get("X-Ngn-Token"), "local-web-token"); assert.equal(init?.credentials, "same-origin"); }
    assert.deepEqual(JSON.parse(String(requests[1].init?.body)), body);
    assert.deepEqual(JSON.parse(String(requests[2].init?.body)), { sdp: "offer", voice: "marin", revision: settings.revision });
  } finally { globalThis.fetch = original; }
});
