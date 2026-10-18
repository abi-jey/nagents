import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { appendEvent, groupTranscript, type Entry } from "./transcript.js";
import { TranscriptItems } from "./Conversation.js";
import type { ResponseMetadata } from "./responseMetadata.js";

const metadata = (summary: string, extra: Partial<ResponseMetadata> = {}): ResponseMetadata => ({
  response_id: "summary-response", response_session_id: "root", run_id: "run", revision: 1, round_number: 0,
  history_id: "", status: "completed", summary, summary_truncated: false, summary_expired: false,
  usage_reported: false,
  usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, reasoning_tokens: 0,
    prompt_reported: false, completion_reported: false, total_reported: false,
    reasoning_reported: false, reasoning_in_output: "unknown" },
  ...extra,
});

const render = (entries: Entry[], disclosures = new Map<string, boolean>()) => new JSDOM(renderToStaticMarkup(createElement(TranscriptItems, {
  items: groupTranscript(entries), names: new Map(), disclosures, toggle() {},
}))).window.document;

test("reconciled heading and body render as a normal reasoning summary", () => {
  let entries = appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("## Plan") });
  entries = appendEvent(entries, { event: "response_metadata", run_id: "run", ...metadata("## Plan\n\nInspect input.\nThen answer.", { revision: 2 }) });

  const doc = render(entries);
  assert.equal(doc.querySelector(".response-reasoning-title")?.textContent, "Plan");
  assert.equal(doc.querySelector(".response-reasoning-preview")?.textContent, "Inspect input.");
  assert.equal(doc.querySelector(".response-reasoning > summary")?.getAttribute("aria-label"), "Show reasoning: Plan");
  assert.equal(doc.querySelector(".response-reasoning")?.hasAttribute("open"), false);
  const expanded = render(entries, new Map([["reasoning:summary-response", true]]));
  assert.equal(expanded.querySelector(".response-reasoning")?.hasAttribute("open"), true);
  assert.equal(expanded.querySelectorAll(".response-reasoning-title").length, 1, "the opening heading is not repeated in the expanded body");
  assert.match(expanded.querySelector(".response-reasoning-content")?.textContent || "", /Inspect input\./);
  assert.match(expanded.querySelector(".response-reasoning-content")?.textContent || "", /Then answer\./);
  assert.doesNotMatch(expanded.querySelector(".response-reasoning-content")?.textContent || "", /without further summary text/);
});

test("a heading-only summary is labeled honestly and usage-only metadata leaves no disclosure", () => {
  const heading = render(appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("## Plan") }));
  assert.equal(heading.querySelector(".response-reasoning-title")?.textContent, "Plan");
  assert.equal(heading.querySelector(".response-reasoning-preview")?.textContent, "Plan");
  assert.equal(heading.querySelector(".response-reasoning-content")?.textContent?.includes("Plan"), false);
  const expandedHeading = render(appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("## Plan") }),
    new Map([["reasoning:summary-response", true]]));
  assert.equal(expandedHeading.querySelector(".response-reasoning-title")?.textContent, "Plan");
  assert.match(expandedHeading.querySelector(".response-reasoning-content")?.textContent || "", /without further summary text/);
  assert.equal(expandedHeading.querySelector(".response-reasoning-content")?.textContent?.includes("## Plan"), false);

  const usageOnly = render(appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("", {
    usage_reported: true,
    usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16, reasoning_tokens: 0,
      prompt_reported: true, completion_reported: true, total_reported: true,
      reasoning_reported: false, reasoning_in_output: "unknown" },
  }) }));
  assert.equal(usageOnly.querySelector(".response-reasoning"), null);
  assert.equal(usageOnly.querySelector("article"), null);
});
