import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { dictationConfig } from "../dictation/testFixtures.js";
import { createDraft } from "./draft.js";
import { SettingsDialog } from "./SettingsDialog.js";
import type { SettingsReply } from "./types.js";
import type { useSettings } from "./useSettings.js";
import { settingsValues } from "./testFixtures.js";

test("compact settings keep provider and preference fields behind native advanced disclosures", () => {
  const values = settingsValues({ model: "chat-model", provider: "openrouter", auth: "api-key", api_key_env: "OPENROUTER_API_KEY", dictation_enabled: true, compact_trigger: "tokens", compact_tokens: 120000, compact_messages: 80 });
  const snapshot: SettingsReply = {
    values, defaults: values, profiles: [{ name: "assistant", mode: "build", model: "" }],
    revision: "opaque", persisted: false, effective_mode: "build",
    providers: ["openai", "openrouter"], apis: ["auto", "chat_completions", "responses", "messages"],
    auths: ["auto", "api-key", "chatgpt"],
    connection: {
      provider: "openrouter", api: "auto", auth: "api-key", base_url: "",
      api_key_env: "OPENROUTER_API_KEY", key_configured: true, auth_status: "configured",
    },
    dictation: dictationConfig,
  };
  const unexpected = () => assert.fail("Rendering cannot mutate settings");
  const settings: ReturnType<typeof useSettings> = {
    token: "test-token", open: true, snapshot, draft: createDraft(values), errors: {}, error: "", notice: "",
    apiKey: "", clearKey: false, needsRefresh: false, loading: false, pending: false, dirty: false,
    disabled: false, blocked: false, scope: "workspace", showGlobal: unexpected,
    show: unexpected, close: unexpected, refresh: async () => unexpected(), update: unexpected,
    updateKey: unexpected, updateClearKey: unexpected,
    save: async () => unexpected(), reset: async () => unexpected(),
  };
  const html = renderToStaticMarkup(createElement(SettingsDialog, { settings }));
  assert.ok(html.indexOf("<legend>Dictation</legend>") < html.indexOf("<summary>Execution limits</summary>"));
  assert.match(html, /<details class="settings-disclosure"><summary>Execution limits<\/summary>/);
  assert.match(html, /<details class="settings-disclosure"><summary>Connection and startup defaults<\/summary>[\s\S]*Use global defaults[\s\S]*<\/details>/);
  assert.match(html, /<details class="settings-help settings-connection"><summary>Dictation details/);
  assert.match(html, /<details class="settings-disclosure"><summary>Context compaction<\/summary>/);
  assert.match(html, /id="settings-compact_trigger"[\s\S]*?<option value="off">Off<\/option>/);
  assert.match(html, /id="settings-compact_tokens"[^>]*inputmode="numeric"/i);
  assert.match(html, /id="settings-compact_messages"[^>]*inputmode="numeric"/i);
  assert.ok(html.indexOf("<summary>Execution limits</summary>") < html.indexOf("<summary>Context compaction</summary>"));
  assert.ok(html.indexOf("<summary>Context compaction</summary>") < html.indexOf("<summary>Connection and startup defaults</summary>"));
  for (const key of Object.keys(values)) assert.ok(html.includes(`id="settings-${key}"`), key);
  for (const constraint of ["600 seconds", "300 seconds", "two lowercase", "API key environment name", "Provider default", "10,000,000 tokens", "1 to 10,000 messages"])
    assert.ok(html.toLowerCase().includes(constraint.toLowerCase()), constraint);
  assert.match(html, /id="settings-provider-key"[^>]*type="password"/);
  assert.doesNotMatch(html, /id="settings-dictation[^"]*key[^"]*"[^>]*type="password"/);
  assert.match(html, /A key is stored in this workspace/);
  const global = renderToStaticMarkup(createElement(SettingsDialog, { settings: { ...settings, scope: "global" } }));
  assert.match(global, /Global settings/);
  assert.doesNotMatch(global, /id="settings-provider-key"/);
  assert.match(global, /id="settings-agent"[^>]*disabled/);
  assert.match(global, /id="global-model"/);
});
