import assert from "node:assert/strict";
import { test } from "node:test";
import { applyLaunchSettings, copyRuntimeSettings, createRuntimeSettings, resolveAgentRuntime, resolveRuntimeSettings, validateRuntimeSettings } from "./agentRuntime";
import type { RuntimeSettings, RuntimeSettingsMap } from "./agentRuntime";
import { advanceTo, queueMessage, startDemo } from "./engine";

test("omitted settings inherit the original profile and explicit provider/model choices resolve together", () => {
  const initial = startDemo();
  assert.deepEqual(createRuntimeSettings(), {});
  const inherited = resolveRuntimeSettings(initial.agents.builder);
  assert.equal(inherited.provider, "openai");
  assert.equal(inherited.model, "gpt-6.1-sol");
  assert.equal(inherited.compact_trigger, "auto");
  assert.equal(inherited.compact_tokens, 200_000);
  assert.equal(inherited.compact_messages, 100);
  const changed = resolveRuntimeSettings(initial.agents.builder, { model: "gpt-6-astra", compact_trigger: "off" });
  assert.equal(changed.provider, "openai");
  assert.equal(changed.model, "gpt-6-astra");
  assert.equal(changed.compact_trigger, "off");
  const otherProvider = resolveRuntimeSettings(initial.agents.builder, { provider: "gemini" });
  assert.equal(otherProvider.providerLabel, "Google Gemini");
  assert.equal(otherProvider.model, "demo-gemini");
  assert.equal(otherProvider.modelContextLimit, 128_000);
});

test("validation rejects provider/model mismatches and matches framework compaction field boundaries", () => {
  const agent = startDemo().agents.builder;
  assert.equal(validateRuntimeSettings(agent, { provider: "anthropic", model: "gpt-6-luna" }).valid, false);
  assert.equal(validateRuntimeSettings(agent, { provider: "unknown" } as unknown as RuntimeSettings).valid, false);
  assert.equal(validateRuntimeSettings(agent, { compact_trigger: "immediate" } as unknown as RuntimeSettings).valid, false);
  for (const compact_tokens of [0, 1023, 10_000_001, 1024.5, NaN, Infinity]) {
    assert.equal(validateRuntimeSettings(agent, { compact_tokens }).valid, false, String(compact_tokens));
  }
  for (const compact_tokens of [1024, 200_000, 10_000_000]) assert.equal(validateRuntimeSettings(agent, { compact_tokens }).valid, true);
  for (const compact_messages of [0, 10_001, 1.5, NaN, Infinity]) assert.equal(validateRuntimeSettings(agent, { compact_messages }).valid, false);
  for (const compact_messages of [1, 100, 10_000]) assert.equal(validateRuntimeSettings(agent, { compact_messages }).valid, true);
  assert.throws(() => resolveRuntimeSettings(agent, { model: "" }), /Choose a model/);
});

test("saving next-run settings cannot change the accepted run, and replay applies an isolated snapshot", () => {
  const initial = startDemo();
  const saved: RuntimeSettingsMap = { builder: { model: "gpt-6-astra", compact_trigger: "tokens", compact_tokens: 100_000 } };
  const accepted = copyRuntimeSettings(saved);
  const launched = applyLaunchSettings(initial, accepted);
  assert.equal(initial.agents.builder.definition.model, "gpt-6.1-sol");
  assert.equal(launched.agents.builder.definition.model, "gpt-6-astra");
  assert.notEqual(launched.agents.builder.definition, initial.agents.builder.definition);
  const running = advanceTo(launched, 8);
  saved.builder.model = "gpt-6-luna";
  saved.builder.compact_tokens = 50_000;
  const current = resolveAgentRuntime(running.agents.builder, running, accepted);
  assert.equal(current.model, "gpt-6-astra");
  assert.equal(current.compactThresholdTokens, 70_000);
  assert.equal(resolveRuntimeSettings(running.agents.builder, saved.builder).model, "gpt-6-luna");
  const replayAccepted = copyRuntimeSettings(saved);
  const replay = applyLaunchSettings(startDemo(), replayAccepted);
  assert.equal(replay.agents.builder.definition.model, "gpt-6-luna");
  assert.equal(resolveAgentRuntime(replay.agents.builder, replay, replayAccepted).compactThresholdTokens, 35_000);
  assert.throws(() => applyLaunchSettings(running, saved), /fresh scenario/);
  assert.equal(resolveRuntimeSettings(launched.agents.builder, {}).model, "gpt-6.1-sol", "reset inheritance resolves the original profile, not the running override");
});

test("token budgets use a 70% threshold without rewriting the model context window", () => {
  const state = advanceTo(startDemo(), 8);
  const inherited = resolveAgentRuntime(state.agents.builder, state, {});
  assert.equal(inherited.compactThresholdTokens, 140_000);
  assert.match(inherited.compactionDetail, /70% of the illustrative model context window/);
  const custom = resolveAgentRuntime(state.agents.builder, state, { builder: { compact_trigger: "tokens", compact_tokens: 1025 } });
  assert.equal(custom.compactThresholdTokens, 717);
  assert.equal(custom.modelContextLimit, inherited.modelContextLimit);
  assert.equal(custom.compactionDue, true);
  assert.equal(custom.compactionStatus, "due_next_response");
  assert.match(custom.compactionDetail, /after the active tool batch/);
  assert.equal(state.agents.builder.records.some(record => record.kind === "tool" && record.tool.status === "running"), true, "the helper must not interrupt a tool or pretend it compacted the context");
  const larger = resolveAgentRuntime(state.agents.builder, state, { builder: { compact_trigger: "tokens", compact_tokens: 500_000 } });
  assert.equal(larger.compactThresholdTokens, 350_000, "a user threshold must not be silently clamped to the model window");
  assert.match(larger.compactionDetail, /model window; that window is reached first/);
});

test("message-count and disabled policies do not fabricate a token threshold", () => {
  const state = advanceTo(startDemo(), 8);
  const messages = resolveAgentRuntime(state.agents.builder, state, { builder: { compact_trigger: "messages", compact_messages: 2 } });
  assert.equal(messages.compactThresholdTokens, undefined);
  assert.equal(messages.compactThresholdMessages, 2);
  assert.equal(messages.compactionDue, true);
  const off = resolveAgentRuntime(state.agents.builder, state, { builder: { compact_trigger: "off", compact_tokens: 1024 } });
  assert.equal(off.compactThresholdTokens, undefined);
  assert.equal(off.compactThresholdMessages, undefined);
  assert.equal(off.compactionDue, false);
  assert.equal(off.compactionStatus, "disabled");
});

test("context estimates count accepted records while queued notes and partial tool output remain separate", () => {
  const initial = startDemo();
  assert.equal(resolveAgentRuntime(initial.agents.layout, initial, {}).estimatedTokens, 0);
  const state = advanceTo(initial, 8);
  const before = resolveAgentRuntime(state.agents.builder, state, {});
  const queued = queueMessage(state, "builder", "Preserve this long filename and check keyboard focus.");
  const waiting = resolveAgentRuntime(queued.agents.builder, queued, {});
  assert.equal(waiting.estimatedTokens, before.estimatedTokens);
  assert.equal(waiting.messageCount, before.messageCount);
  assert.equal(waiting.queuedMessages, 1);
  const partialTool = structuredClone(queued.agents.builder);
  for (const record of partialTool.records) if (record.kind === "tool") record.tool.output += "An unaccepted streamed result".repeat(200);
  assert.equal(resolveAgentRuntime(partialTool, queued, {}).estimatedTokens, before.estimatedTokens);
  const complete = advanceTo(queued, 16);
  const withResult = resolveAgentRuntime(complete.agents.builder, complete, {});
  assert.ok(withResult.estimatedTokens > before.estimatedTokens);
  assert.equal(withResult.queuedMessages, 1);
  const delivered = advanceTo(complete, 17);
  const after = resolveAgentRuntime(delivered.agents.builder, delivered, {});
  assert.equal(after.queuedMessages, 0);
  assert.ok(after.estimatedTokens > withResult.estimatedTokens);
  assert.equal(after.contextFraction, after.estimatedTokens / after.modelContextLimit);
  assert.match(after.estimateDetail, /Demo estimate/);
  assert.match(after.estimateDetail, /Queued notes and partial tool output are excluded/);
});

test("an estimate beyond the illustrative window has a bounded meter and an explicit overflow flag", () => {
  const state = advanceTo(startDemo(), 8);
  const agent = structuredClone(state.agents.builder);
  agent.records.push({ kind: "message", message: { id: "large-demo-record", at: 8, role: "assistant", text: "x".repeat(900_000) } });
  const context = resolveAgentRuntime(agent, state, {});
  assert.ok(context.estimatedTokens > context.modelContextLimit, "the absolute estimate remains visible above the bounded meter");
  assert.equal(context.contextFraction, 1);
  assert.equal(context.contextOverflow, true);
});
