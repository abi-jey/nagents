import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { AgentRow } from "../team/TeamTree.js";
import { compactLabel, compactionMarker, compactionWarning } from "../team/context.js";
import { taskAgent, type TeamAgent } from "../team/model.js";
import type { ContextStats, RetainedTask } from "../../types.js";

const context: ContextStats = {
  components: [], total_tokens: 144875, context_window: 1000000, context_window_source: "provider",
  remaining_tokens: 751221, observed_prompt_tokens: 248779, observed_completion_tokens: 1200,
  provider: "litellm-opus", model: "eu.anthropic.claude-opus-5-5", estimate_method: "chars/4 heuristic",
  compaction: { trigger: "tokens", token_threshold: 420000, token_budget: 600000 },
};
const agent: TeamAgent = { id: "", name: "Assistant", profile: "assistant", parentId: "", prompt: "", phase: "ready", reason: "",
  provider: context.provider, providerKind: "litellm", model: context.model, startedAt: 0, updatedAt: 0,
  context, compactTrigger: "tokens", compactTokens: 600000, compactThreshold: 420000, canMessage: true, messageReason: "" };
const render = (value: TeamAgent) => new JSDOM(renderToStaticMarkup(createElement(AgentRow, {
  agent: value, selected: false, inspect() {}, settings() {},
}))).window.document;

test("main and child rows keep one context line and place the compaction tick against their own model window", () => {
  const root = render(agent);
  assert.equal(root.querySelector(".team-agent-context")?.textContent, "249k / 1M");
  assert.equal(root.querySelector(".team-agent-meter i")?.getAttribute("style"), "width:25%");
  assert.equal(root.querySelector(".team-agent-threshold")?.getAttribute("style"), "left:42%");
  assert.equal(root.querySelector(".team-agent-open")?.getAttribute("aria-description"), "Compaction at 420,000 tokens (42% of context window)");
  assert.doesNotMatch(root.body.textContent || "", /Compact at|Compaction at|~249k|100k|exceeds/);
  const child = taskAgent({ id: "child", name: "Research", status: "running", context: { ...context, context_window: 200000,
    observed_prompt_tokens: 110000, compaction: { trigger: "tokens", token_threshold: 105000, token_budget: 150000 } },
    compact_tokens: 900000, compaction: { trigger: "tokens", token_threshold: 630000, token_budget: 900000 },
  } as RetainedTask);
  assert.equal(child.compactTokens, 150000, "Accepted context policy wins over a stale task snapshot");
  const doc = render(child);
  assert.equal(doc.querySelector(".team-agent-context")?.textContent, "110k / 200k");
  assert.equal(doc.querySelector(".team-agent-threshold")?.getAttribute("style"), "left:52.5%");
  assert.equal(doc.querySelector(".team-agent-threshold")?.getAttribute("title"), "Compaction at 105,000 tokens (52.5% of context window)");
  assert.doesNotMatch(doc.body.textContent || "", /Compact at|Compaction at|900k|600k/);
});

test("automatic thresholds are host supplied and absent or non-token policies never invent a marker", () => {
  const automatic = { ...agent, compactTrigger: "auto", compactTokens: 500000, compactThreshold: 350000,
    context: { ...context, context_window: 500000 } };
  assert.equal(render(automatic).querySelector(".team-agent-threshold")?.getAttribute("style"), "left:70%");
  for (const compactThreshold of [undefined, 0, -1, NaN, Infinity]) {
    assert.equal(compactionMarker({ ...automatic, compactThreshold }), undefined);
  }
  for (const context_window of [null, 0, -1, NaN, Infinity]) {
    assert.equal(compactionMarker({ ...automatic, context: { ...context, context_window } }), undefined);
  }
  for (const compactTrigger of ["off", "messages", undefined]) {
    assert.equal(render({ ...automatic, compactTrigger }).querySelector(".team-agent-threshold"), null);
  }
  assert.equal(render({ ...automatic, compactThreshold: undefined }).querySelector(".team-agent-threshold"), null);
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
  const beyond = { ...over, compactTokens: 900000, compactThreshold: 630000 };
  const doc = render(beyond);
  assert.equal(doc.querySelector(".team-agent-threshold")?.getAttribute("style"), "left:100%");
  assert.match(doc.querySelector(".team-agent-threshold")?.getAttribute("title") || "", /630,000 tokens \(126% of context window\)/);
  assert.match(doc.querySelector(".team-context-warning")?.textContent || "", /900k budget exceeds the 500k model limit/);
});
