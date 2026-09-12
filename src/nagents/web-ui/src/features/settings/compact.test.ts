import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { dictationConfig } from "../dictation/testFixtures.js";
import { createDraft } from "./draft.js";
import { SettingsDialog } from "./SettingsDialog.js";
import type { SettingsReply } from "./types.js";
import type { useSettings } from "./useSettings.js";

test("compact settings keep eleven fields and default-reset controls behind native advanced disclosures", () => {
  const values = {
    model: "chat-model", agent: "build", shell_timeout: 30, max_output: 16384,
    max_file_bytes: 1048576, max_tool_rounds: 100, max_subagent_depth: 2,
    dictation_enabled: true, dictation_model: "gpt-4o-mini-transcribe", dictation_language: "", dictation_max_seconds: 60,
  };
  const snapshot: SettingsReply = {
    values, defaults: values, profiles: [{ name: "build", mode: "build", model: "" }],
    revision: "opaque", persisted: false, effective_mode: "build",
    connection: { provider: "openai", api: "codex", auth_status: "configured" },
    dictation: dictationConfig,
  };
  const unexpected = () => assert.fail("Rendering cannot mutate settings");
  const settings: ReturnType<typeof useSettings> = {
    token: "test-token", open: true, snapshot, draft: createDraft(values), errors: {}, error: "", notice: "",
    needsRefresh: false, loading: false, pending: false, dirty: false, disabled: false, blocked: false,
    show: unexpected, close: unexpected, refresh: async () => unexpected(), update: unexpected,
    save: async () => unexpected(), reset: async () => unexpected(),
  };
  const html = renderToStaticMarkup(createElement(SettingsDialog, { settings }));
  assert.ok(html.indexOf("<legend>Dictation</legend>") < html.indexOf("<summary>Execution limits</summary>"));
  assert.match(html, /<details class="settings-disclosure"><summary>Execution limits<\/summary>/);
  assert.match(html, /<details class="settings-disclosure"><summary>Connection and startup defaults<\/summary>[\s\S]*Reset to startup defaults[\s\S]*<\/details>/);
  assert.match(html, /<details class="settings-help settings-connection"><summary>Dictation details/);
  for (const key of Object.keys(values)) assert.ok(html.includes(`id="settings-${key}"`), key);
  for (const constraint of ["600 seconds", "300 seconds", "two lowercase", "API key environment name"])
    assert.ok(html.toLowerCase().includes(constraint.toLowerCase()), constraint);
  assert.doesNotMatch(html, /type="password"/);
});
