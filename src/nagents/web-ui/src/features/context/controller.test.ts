import assert from "node:assert/strict";
import test from "node:test";
import { validContextStats } from "../../api/context.js";
import type { ContextStats } from "../../types.js";
import { changesContext, ContextRefresh, contextPercent, contextRows, contextSummary, formatTokens } from "./controller.js";

const stats: ContextStats = {
  components: [
    { key: "system_prompt", label: "System prompt", tokens: 1200 },
    { key: "instructions", label: "Request instructions", tokens: 0 },
    { key: "tools", label: "Tool definitions", tokens: 3400 },
    { key: "skills", label: "Skills", tokens: 200 },
    { key: "history_user", label: "Conversation history (user)", tokens: 800 },
    { key: "history_assistant", label: "Conversation history (assistant)", tokens: 400 },
    { key: "history_tool", label: "Conversation history (tool)", tokens: 0 },
    { key: "history_other", label: "Conversation history (other)", tokens: 0 },
    { key: "media", label: "Media and attachments", tokens: 1500 },
  ],
  total_tokens: 7500,
  context_window: 128000,
  remaining_tokens: 120500,
  observed_prompt_tokens: 7100,
  observed_completion_tokens: 220,
  provider: "openai_compatible",
  model: "gpt-4o",
  estimate_method: "chars/4 heuristic",
};

test("validContextStats accepts a component breakdown and rejects malformed rows", () => {
  assert.equal(validContextStats(stats), true);
  assert.equal(validContextStats({ ...stats, total_tokens: "many" }), false);
  assert.equal(validContextStats({ ...stats, components: [{ key: "tools", label: "Tools" }] }), false);
  assert.equal(validContextStats({ ...stats, components: "tools" }), false);
  assert.equal(validContextStats(null), false);
});

test("contextRows keeps the tool-definition row and drops empty components", () => {
  const rows = contextRows(stats);
  assert.deepEqual(rows.map((row) => row.key), [
    "system_prompt",
    "tools",
    "skills",
    "history_user",
    "history_assistant",
    "media",
  ]);
  assert.equal(rows.find((row) => row.key === "tools")?.tokens, 3400);
});

test("summary and percent distinguish known and unknown context windows", () => {
  assert.equal(formatTokens(999), "999");
  assert.equal(formatTokens(7500), "7.5k");
  assert.equal(formatTokens(128000), "128k");
  assert.equal(contextSummary(stats), "~7.5k / 128k");
  assert.equal(contextPercent(stats), 6);
  const unknown = { ...stats, context_window: null, remaining_tokens: null };
  assert.equal(contextSummary(unknown), "~7.5k");
  assert.equal(contextPercent(unknown), 0);
});

function clock() {
  const jobs: { action: () => void; cancelled: boolean }[] = [];
  return {
    jobs,
    schedule: (action: () => void) => { const job = { action, cancelled: false }; jobs.push(job); return () => { job.cancelled = true; }; },
    step: () => { const job = jobs.shift(); if (job && !job.cancelled) job.action(); },
  };
}

test("context refresh coalesces streaming bursts and follows up changes arriving during a read", async () => {
  const timer = clock();
  const reads: { signal: AbortSignal; resolve: (value: ContextStats) => void }[] = [];
  const received: ContextStats[] = [];
  const refresh = new ContextRefresh({
    read: (signal) => new Promise<ContextStats>((resolve) => reads.push({ signal, resolve })),
    accept: (value) => received.push(value), error: () => undefined, loading: () => undefined, schedule: timer.schedule,
  });
  refresh.refresh(true); refresh.refresh(); refresh.refresh();
  assert.equal(timer.jobs.length, 1);
  timer.step();
  assert.equal(reads.length, 1);
  refresh.refresh(); refresh.refresh();
  assert.equal(timer.jobs.length, 0);
  reads[0].resolve(stats);
  await Promise.resolve();
  assert.equal(timer.jobs.length, 1);
  timer.step();
  assert.equal(reads.length, 2);
  reads[1].resolve({ ...stats, model: "updated" });
  await Promise.resolve();
  assert.deepEqual(received.map((item) => item.model), ["gpt-4o", "updated"]);
  refresh.dispose();
});

test("context refresh aborts on navigation and never publishes a late previous-session response", async () => {
  const timer = clock();
  let complete: (value: ContextStats) => void = () => assert.fail("Read was not started");
  let signal: AbortSignal | undefined;
  let received = 0;
  const refresh = new ContextRefresh({
    read: (value) => { signal = value; return new Promise<ContextStats>((resolve) => { complete = resolve; }); },
    accept: () => { received++; }, error: () => assert.fail("Aborted requests must not publish errors"), loading: () => undefined, schedule: timer.schedule,
  });
  refresh.refresh(); timer.step(); refresh.dispose();
  assert.equal(signal?.aborted, true);
  complete(stats); await Promise.resolve();
  assert.equal(received, 0);
  refresh.refresh(); assert.equal(timer.jobs.length, 0);
});

test("turn, tool, streaming, and compaction events refresh context without heartbeat traffic", () => {
  for (const event of ["run_started", "user_message", "text_chunk", "text_done", "tool_call", "tool_result", "compaction_done", "task_completed", "run_finished"]) assert.equal(changesContext(event), true, event);
  assert.equal(changesContext("heartbeat"), false);
  assert.equal(changesContext("approval"), false);
});
