import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { dictationConfig } from "../dictation/testFixtures.js";
import { createDraft } from "./draft.js";
import { SettingsDialog } from "./SettingsDialog.js";
import type { SettingsReply } from "./types.js";
import type { useSettings } from "./useSettings.js";

test("compact settings keep provider and preference fields behind native advanced disclosures", () => {
  const values = {
    model: "chat-model", agent: "build",
    provider: "openrouter", base_url: "", api: "auto", auth: "api-key", api_key_env: "OPENROUTER_API_KEY",
    shell_timeout: 30, max_output: 16384, max_file_bytes: 1048576, max_tool_rounds: 100,
    max_subagent_depth: 2, dictation_enabled: true, dictation_model: "gpt-4o-mini-transcribe",
    dictation_language: "", dictation_max_seconds: 60,
  };
  const snapshot: SettingsReply = {
    values, defaults: values, profiles: [{ name: "build", mode: "build", model: "" }],
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
    disabled: false, blocked: false,
    show: unexpected, close: unexpected, refresh: async () => unexpected(), update: unexpected,
    updateKey: unexpected, updateClearKey: unexpected,
    save: async () => unexpected(), reset: async () => unexpected(),
  };
  const html = renderToStaticMarkup(createElement(SettingsDialog, { settings }));
  assert.ok(html.indexOf("<legend>Dictation</legend>") < html.indexOf("<summary>Execution limits</summary>"));
  assert.match(html, /<details class="settings-disclosure"><summary>Execution limits<\/summary>/);
  assert.match(html, /<details class="settings-disclosure"><summary>Connection and startup defaults<\/summary>[\s\S]*Reset to startup defaults[\s\S]*<\/details>/);
  assert.match(html, /<details class="settings-help settings-connection"><summary>Dictation details/);
  for (const key of Object.keys(values)) assert.ok(html.includes(`id="settings-${key}"`), key);
  for (const constraint of ["600 seconds", "300 seconds", "two lowercase", "API key environment name", "Provider default"])
    assert.ok(html.toLowerCase().includes(constraint.toLowerCase()), constraint);
  assert.match(html, /id="settings-provider-key"[^>]*type="password"/);
  assert.doesNotMatch(html, /id="settings-dictation[^"]*key[^"]*"[^>]*type="password"/);
  assert.match(html, /A key is stored in this workspace/);
});
