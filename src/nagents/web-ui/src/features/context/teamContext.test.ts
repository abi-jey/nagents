import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentRow } from "../team/TeamTree.js";
import { compactLabel, compactionWarning } from "../team/context.js";
import { taskAgent, type TeamAgent } from "../team/model.js";
import type { ContextStats, RetainedTask } from "../../types.js";

const context: ContextStats = {
  components: [], total_tokens: 144875, context_window: 1000000, context_window_source: "provider",
  remaining_tokens: 751221, observed_prompt_tokens: 248779, observed_completion_tokens: 1200,
  provider: "litellm-opus", model: "eu.anthropic.claude-opus-5-5", estimate_method: "chars/4 heuristic",
  compaction: { trigger: "tokens", token_threshold: 420000, token_budget: 600000 },
};
const agent: TeamAgent = { id: "", name: "Assistant", parentId: "", prompt: "", phase: "ready", reason: "",
  provider: context.provider, providerKind: "litellm", model: context.model, startedAt: 0, updatedAt: 0,
  context, compactTrigger: "tokens", compactTokens: 600000, compactThreshold: 420000, canMessage: true, messageReason: "" };

test("main and child rows show their own usage and window without the compaction detail line", () => {
  const root = renderToStaticMarkup(createElement(AgentRow, { agent, selected: true, inspect() {}, settings() {} }));
  assert.match(root, /249k \/ 1M/);
  assert.doesNotMatch(root, /Compact at/);
  assert.match(root, /width:25%/);
  assert.doesNotMatch(root, /~249k|100k|exceeds/);
  const child = taskAgent({ id: "child", name: "Research", status: "running", context: { ...context, context_window: 200000,
    observed_prompt_tokens: 110000, compaction: { trigger: "tokens", token_threshold: 105000, token_budget: 150000 } },
    compact_tokens: 900000, compaction: { trigger: "tokens", token_threshold: 630000, token_budget: 900000 },
  } as RetainedTask);
  assert.equal(child.compactTokens, 150000, "Accepted context policy wins over a stale task snapshot");
  const html = renderToStaticMarkup(createElement(AgentRow, { agent: child, selected: false, inspect() {}, settings() {} }));
  assert.match(html, /110k \/ 200k/);
  assert.doesNotMatch(html, /Compact at/);
  assert.doesNotMatch(html, /900k|600k/);
});

test("over-limit budgets warn only against a known window, and off/messages ignore token budgets", () => {
  const over = { ...agent, context: { ...context, context_window: 500000 } };
  assert.equal(compactionWarning(over), "600k budget exceeds the 500k model limit.");
  const html = renderToStaticMarkup(createElement(AgentRow, { agent: over, selected: false, inspect() {}, settings() {} }));
  assert.match(html, /600k budget exceeds the 500k model limit/);
  assert.equal(compactionWarning({ ...over, context: { ...over.context, context_window_source: "heuristic" } }), "");
  for (const compactTrigger of ["off", "messages"]) assert.equal(compactionWarning({ ...over, compactTrigger }), "");
  assert.equal(compactLabel({ ...agent, compactTrigger: "off" }), "Compaction off");
  assert.equal(compactLabel({ ...agent, compactTrigger: "messages", compactMessages: 50 }), "Compact at 50 messages");
});
