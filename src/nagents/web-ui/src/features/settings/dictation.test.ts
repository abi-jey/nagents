import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { dictationConfig } from "../dictation/testFixtures.js";
import { DictationSettings } from "./DictationSettings.js";
import { createDraft, parseDraft, selectProfile, type SettingsDraft } from "./draft.js";
import type { SettingsValues } from "./types.js";

const values: SettingsValues = {
  model: "codex-chat-model", agent: "build", shell_timeout: 30, max_output: 16384,
  max_file_bytes: 1048576, max_tool_rounds: 100, max_subagent_depth: 2,
  dictation_enabled: true, dictation_model: "gpt-4o-mini-transcribe", dictation_language: "", dictation_max_seconds: 60,
};
const profiles = [{ name: "build", mode: "build" as const, model: "" }, { name: "review", mode: "reviewer" as const, model: "another-chat-model" }];

test("all eleven settings round-trip with actual booleans and independent chat/transcription models", () => {
  assert.equal(Object.keys(values).length, 11);
  for (const enabled of [true, false]) {
    const current = { ...values, dictation_enabled: enabled };
    const draft = createDraft(current);
    assert.equal(typeof draft.dictation_enabled, "boolean");
    assert.deepEqual(parseDraft(draft, profiles), { ok: true, values: current });
    const selected = selectProfile(draft, "review", profiles);
    assert.equal(selected.model, "another-chat-model");
    assert.equal(selected.dictation_model, "gpt-4o-mini-transcribe");
    assert.equal(selected.dictation_enabled, enabled);
  }
});

test("dictation validates printable trimmed model, exact language, boolean, and bounded integer seconds", () => {
  const draft = createDraft(values);
  const trimmed = parseDraft({ ...draft, dictation_model: " vendor/transcribe " }, profiles);
  assert.ok(trimmed.ok);
  assert.equal(trimmed.values.dictation_model, "vendor/transcribe");
  for (const model of ["", " ", "a".repeat(201), "model\n", "mo\u0085del", "mo\u200bdel", "mo\u00a0del"])
    assert.equal(parseDraft({ ...draft, dictation_model: model }, profiles).ok, false);
  for (const language of ["", "en", "fr"])
    assert.equal(parseDraft({ ...draft, dictation_language: language }, profiles).ok, true);
  for (const language of ["EN", "eng", " en", "en-US", "1a"])
    assert.equal(parseDraft({ ...draft, dictation_language: language }, profiles).ok, false);
  for (const seconds of ["1", "300"])
    assert.equal(parseDraft({ ...draft, dictation_max_seconds: seconds }, profiles).ok, true);
  for (const seconds of ["", "0", "301", "1.5", "1e2", "Infinity"])
    assert.equal(parseDraft({ ...draft, dictation_max_seconds: seconds }, profiles).ok, false);
  assert.equal(parseDraft({ ...draft, dictation_enabled: "false" } as unknown as SettingsDraft, profiles).ok, false);
});

test("settings render effective administrator status and key name read-only without provider discovery or credential inputs", (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => assert.fail("No discovery or transcription on settings mount"));
  const html = renderToStaticMarkup(createElement(DictationSettings, {
    config: {
      ...dictationConfig, admin_enabled: false, available: false,
      api_key_env: "NGN_TRANSCRIPTION_API_KEY", status: "Disabled by administrator.",
    },
    draft: createDraft(values), errors: {}, disabled: false,
    update: () => assert.fail("Rendering must not change preferences"),
  }));
  assert.match(html, /<legend>Dictation<\/legend>/);
  assert.match(html, /type="checkbox"[^>]*checked=""/);
  assert.match(html, /Disabled by administrator/);
  assert.match(html, /Effective recording ceiling/);
  assert.match(html, /NGN_TRANSCRIPTION_API_KEY/);
  assert.match(html, /separate from your chat model and Codex login/);
  assert.doesNotMatch(html, /type="password"|Fetch models|<input[^>]*(?:api_key_env|endpoint)/);
  assert.equal(fetch.mock.callCount(), 0);
});
