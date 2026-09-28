import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createDraft } from "./draft.js";
import { SettingsDialog } from "./SettingsDialog.js";
import type { SettingsReply } from "./types.js";
import type { useSettings } from "./useSettings.js";
import { settingsValues } from "./testFixtures.js";

test("settings route provider configuration through the shared connection manager", () => {
  const values = settingsValues({ model: "chat-model", provider: "openrouter", auth: "api-key", api_key_env: "OPENROUTER_API_KEY", compact_trigger: "tokens", compact_tokens: 120000, compact_messages: 80 });
  const snapshot: SettingsReply = {
    values, defaults: values, profiles: [{ name: "assistant", mode: "build", model: "" }],
    revision: "opaque", persisted: false, effective_mode: "build",
    providers: ["openai", "openrouter"], apis: ["auto", "chat_completions", "responses", "messages"],
    auths: ["auto", "api-key", "chatgpt"],
    connection: {
      provider: "openrouter", api: "auto", auth: "api-key", base_url: "",
      api_key_env: "OPENROUTER_API_KEY", key_configured: true, auth_status: "configured",
    },
  };
  const unexpected = () => assert.fail("Rendering cannot mutate settings");
  const settings: ReturnType<typeof useSettings> = {
    token: "test-token", open: true, snapshot, draft: createDraft(values), errors: {}, error: "", readError: "", notice: "",
    needsRefresh: false, changedElsewhere: false, loading: false, pending: false, dirty: false,
    disabled: false, blocked: false, scope: "workspace", showGlobal: unexpected, switchScope: unexpected,
    show: unexpected, close: unexpected, refresh: async () => unexpected(), reload: async () => unexpected(), update: unexpected,
    save: async () => unexpected(), reset: async () => unexpected(),
  };
  const html = renderToStaticMarkup(createElement(SettingsDialog, { settings }));
  assert.match(html, /<details class="settings-disclosure"><summary>Execution limits<\/summary>/);
  assert.match(html, /<details class="settings-disclosure"><summary>Settings defaults<\/summary>[\s\S]*Use global defaults[\s\S]*<\/details>/);
  assert.match(html, /<details class="settings-disclosure"><summary>Context compaction<\/summary>/);
  assert.match(html, /id="settings-compact_trigger"[\s\S]*?<option value="off">Off<\/option>/);
  assert.match(html, /id="settings-compact_tokens"[^>]*inputmode="numeric"/i);
  assert.match(html, /id="settings-compact_messages"[^>]*inputmode="numeric"/i);
  assert.ok(html.indexOf("<summary>Execution limits</summary>") < html.indexOf("<summary>Context compaction</summary>"));
  assert.ok(html.indexOf("<summary>Context compaction</summary>") < html.indexOf("<summary>Settings defaults</summary>"));
  for (const key of Object.keys(values).filter((key) => !["provider", "model", "api", "auth", "api_key_env", "base_url"].includes(key)))
    assert.ok(html.includes(`id="settings-${key}"`), key);
  for (const constraint of ["600 seconds", "10,000,000 tokens", "1 to 10,000 messages"])
    assert.ok(html.toLowerCase().includes(constraint.toLowerCase()), constraint);
  assert.match(html, /Provider connections/);
  assert.match(html, /aria-label="Settings scope"/);
  assert.match(html, /data-settings-scope="workspace" aria-pressed="true"/);
  assert.match(html, /data-settings-scope="global" aria-pressed="false"/);
  assert.match(html, /Inherited from Global/);
  assert.match(html, /Save workspace settings/);
  assert.match(html, /id="settings-model"/);
  assert.doesNotMatch(html, /Legacy provider override|id="settings-provider-key"|id="settings-provider"|type="password"/);
  const global = renderToStaticMarkup(createElement(SettingsDialog, { settings: { ...settings, scope: "global" } }));
  assert.match(global, /data-settings-scope="global" aria-pressed="true"/);
  assert.match(global, /Global defaults and provider connections are shared/);
  assert.match(global, /Save global settings/);
  assert.match(global, /Provider connections/);
  assert.doesNotMatch(global, /Legacy provider override|id="settings-provider-key"|id="global-model"/);
  assert.match(global, /id="settings-agent"[^>]*disabled/);

  const named = renderToStaticMarkup(createElement(SettingsDialog, {
    settings: { ...settings, snapshot: { ...snapshot, connection: { ...snapshot.connection, provider_id: "work" } } },
  }));
  assert.match(named, /Provider connections/);
  assert.doesNotMatch(named, /Legacy provider override|id="settings-provider-key"|type="password"/);
});
