import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ContextIndicator } from "./ContextIndicator.js";
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
  context_window_source: "provider",
  remaining_tokens: 120500,
  observed_prompt_tokens: 7100,
  observed_completion_tokens: 220,
  observed_reasoning_tokens: 40,
  observed_reasoning_in_output: "included",
  observed_context_tokens: 7320,
  pending_context_tokens: 0,
  context_tokens: 7320,
  context_tokens_source: "provider",
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
  assert.equal(contextSummary(stats), "7.3k / 128k");
  assert.equal(contextPercent(stats), 6);
  const unknown = { ...stats, context_window: null, remaining_tokens: null };
  assert.equal(contextSummary(unknown), "7.3k");
  assert.equal(contextPercent(unknown), 0);
});

test("context displays backend accounting and keeps estimated input breakdown separate", () => {
  const actual: ContextStats = {
    ...stats,
    total_tokens: 144875,
    observed_prompt_tokens: 248779,
    observed_completion_tokens: 1200,
    observed_reasoning_tokens: 80,
    observed_context_tokens: 249979,
    pending_context_tokens: 50,
    context_tokens: 250029,
    context_tokens_source: "mixed",
    remaining_tokens: 749971,
    context_window: 1_000_000,
  };
  assert.equal(contextSummary(actual), "~250k / 1M");
  assert.equal(contextPercent(actual), 25);
  const html = renderToStaticMarkup(createElement(ContextIndicator, { stats: actual, error: "", open: true, setOpen() {} }));
  assert.match(html, /Context with estimated additions/);
  assert.match(html, /250,029/);
  assert.match(html, /reported reasoning is counted once/);
  assert.match(html, /Estimated next request/);
  assert.match(html, /144,875/);
  assert.match(html, /provider metadata/);
  assert.doesNotMatch(html, /Last provider-reported input/);
});

test("heuristic limits and usage estimates are marked separately; zero measured context is valid", () => {
  const estimated: ContextStats = {
    ...stats,
    observed_prompt_tokens: null,
    observed_completion_tokens: null,
    observed_reasoning_tokens: null,
    observed_context_tokens: null,
    pending_context_tokens: 0,
    context_tokens: 7500,
    context_tokens_source: "estimated",
    remaining_tokens: 92500,
    context_window: 100000,
    context_window_source: "heuristic",
  };
  assert.equal(contextSummary(estimated), "~7.5k / ~100k (guessed)");
  assert.equal(contextSummary(estimated, true), "~7.5k / ~100k");
  const html = renderToStaticMarkup(createElement(ContextIndicator, { stats: estimated, error: "", open: false, setOpen() {} }));
  assert.match(html, /aria-label="Context ~7.5k \/ ~100k \(guessed\)"/);
  assert.match(html, /model-name guess/);
  assert.equal(contextSummary({ ...estimated, observed_context_tokens: 0, context_tokens: 0, context_tokens_source: "provider" }), "0 / ~100k (guessed)");
  assert.equal(contextPercent({ ...stats, context_tokens: 300000 }), 100);
  for (const value of [-1, NaN, Infinity, 1.5]) {
    assert.equal(validContextStats({ ...stats, observed_prompt_tokens: value }), false);
    assert.equal(validContextStats({ ...stats, observed_reasoning_tokens: value }), false);
    assert.equal(validContextStats({ ...stats, observed_context_tokens: value }), false);
    assert.equal(validContextStats({ ...stats, pending_context_tokens: value }), false);
    assert.equal(validContextStats({ ...stats, context_tokens: value }), false);
    assert.equal(validContextStats({ ...stats, context_window: value }), false);
  }
  assert.equal(validContextStats({ ...stats, context_window_source: "unknown" }), false);
  assert.equal(validContextStats({ ...stats, context_tokens_source: "unknown" }), false);
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
