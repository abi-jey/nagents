import assert from "node:assert/strict";
import { test } from "node:test";
import { applyLaunchSettings, copyRuntimeSettings, resolveRuntimeSettings, validateRuntimeSettings } from "./agentRuntime";
import type { RuntimeSettingsMap } from "./agentRuntime";
import { advanceTo, queueMainMessage, queueMessage, startDemo } from "./engine";
import { DEMO_END } from "./fixtures";
import { MAIN_AGENT_ID, mainAgentSnapshot, resolveMainRuntime } from "./mainRuntime";

test("main overview uses the same provider and context policy without entering child execution", () => {
  const state = startDemo();
  const main = mainAgentSnapshot(state, {});
  const runtime = resolveMainRuntime(state, {});
  assert.equal(MAIN_AGENT_ID, "assistant");
  assert.equal(main.definition.name, "Assistant");
  assert.equal(main.activity, "Ready");
  assert.equal(runtime.providerLabel, "OpenAI");
  assert.equal(runtime.model, "gpt-6.1-sol");
  assert.equal(runtime.promptBaselineTokens, 18_000);
  assert.equal(runtime.compactThresholdTokens, 140_000);
  assert.equal(runtime.messageCount, 2, "main instructions and the initial user message are accepted context");
  assert.equal(runtime.toolCount, 0);
  assert.equal(state.agents.assistant, undefined);
  assert.equal(applyLaunchSettings(state, { assistant: { model: "gpt-6-astra" } }).agents.assistant, undefined);
  assert.equal(queueMessage(state, MAIN_AGENT_ID, "Cannot enqueue through a child adapter"), state);
});

test("main settings inherit the canonical profile even after accepted provider overrides", () => {
  const state = advanceTo(startDemo(), 8);
  const saved: RuntimeSettingsMap = { assistant: { provider: "gemini", compact_trigger: "messages", compact_messages: 8 } };
  const accepted = copyRuntimeSettings(saved);
  const snapshot = mainAgentSnapshot(state, accepted);
  assert.equal(snapshot.definition.provider, "Google Gemini");
  assert.equal(snapshot.definition.model, "demo-gemini");
  assert.equal(resolveMainRuntime(state, accepted).modelContextLimit, 128_000);
  assert.equal(resolveMainRuntime(state, accepted).compactThresholdMessages, 8);
  assert.equal(resolveRuntimeSettings(snapshot).providerLabel, "OpenAI");
  assert.equal(resolveRuntimeSettings(snapshot).model, "gpt-6.1-sol");
  assert.equal(validateRuntimeSettings(snapshot, { model: "gpt-6-astra" }).valid, true);
  saved.assistant = { model: "gpt-6-astra", compact_trigger: "off" };
  assert.equal(resolveMainRuntime(state, accepted).model, "demo-gemini", "saved next-run settings cannot rewrite the accepted root profile");
  const replay = copyRuntimeSettings(saved);
  assert.equal(resolveMainRuntime(startDemo(), replay).model, "gpt-6-astra");
  assert.equal(resolveMainRuntime(startDemo(), replay).compactionStatus, "disabled");
});

test("main pending inputs are counted separately until their own response and never include child queues", () => {
  const state = advanceTo(startDemo(), 8);
  const before = resolveMainRuntime(state, {});
  const childQueued = queueMessage(state, "builder", "A child-only note.");
  assert.deepEqual(resolveMainRuntime(childQueued, {}), before);
  const queued = queueMainMessage(queueMainMessage(childQueued, "One main note."), "Another main note.");
  const waiting = resolveMainRuntime(queued, {});
  assert.equal(waiting.queuedMessages, 2);
  assert.equal(waiting.estimatedTokens, before.estimatedTokens);
  assert.equal(waiting.messageCount, before.messageCount);
  assert.equal(mainAgentSnapshot(queued, {}).activity, "Reply queued");
  const replied = advanceTo(queued, 9);
  const accepted = resolveMainRuntime(replied, {});
  assert.equal(accepted.queuedMessages, 0);
  assert.equal(accepted.messageCount, before.messageCount + 4);
  assert.ok(accepted.estimatedTokens > before.estimatedTokens);
  assert.match(mainAgentSnapshot(replied, {}).activity, /^Coordinating/);
  assert.match(accepted.estimateDetail, /Inputs waiting for the next main response are excluded/);
  assert.equal(replied.agents.builder.records.filter(record => record.kind === "message" && record.message.delivery === "queued").length, 1);
});

test("main context receives returned summaries without importing private child transcripts", () => {
  const before = advanceTo(startDemo(), 25);
  const after = advanceTo(before, 26);
  const first = resolveMainRuntime(before, {});
  const returned = resolveMainRuntime(after, {});
  assert.equal(returned.messageCount, first.messageCount + 1);
  assert.ok(returned.estimatedTokens > first.estimatedTokens);
  assert.equal(returned.toolCount, 0);
  assert.equal(mainAgentSnapshot(after, {}).records.some(record => record.kind === "tool"), false);
  assert.equal(mainAgentSnapshot(advanceTo(after, DEMO_END), {}).activity, "Ready");
  const failedOnly = { ...after, agents: { research: { ...after.agents.research, status: "failed" as const } } };
  assert.equal(mainAgentSnapshot(failedOnly, {}).activity, "1 agent needs attention");
});
