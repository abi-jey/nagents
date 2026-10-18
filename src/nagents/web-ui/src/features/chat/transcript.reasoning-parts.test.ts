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

const render = (entries: Entry[]) => new JSDOM(renderToStaticMarkup(createElement(TranscriptItems, {
  items: groupTranscript(entries), names: new Map(), disclosures: new Map(), toggle() {},
}))).window.document;

test("reconciled heading and body render as a normal reasoning summary", () => {
  let entries = appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("## Plan") });
  entries = appendEvent(entries, { event: "response_metadata", run_id: "run", ...metadata("## Plan\n\nInspect input.\nThen answer.", { revision: 2 }) });

  const doc = render(entries);
  assert.equal(doc.querySelector(".response-reasoning > summary > span:first-child")?.textContent, "Reasoning summary");
  assert.match(doc.querySelector(".response-reasoning-content")?.textContent || "", /Inspect input\./);
  assert.match(doc.querySelector(".response-reasoning-content")?.textContent || "", /Then answer\./);
  assert.doesNotMatch(doc.querySelector(".response-reasoning-content")?.textContent || "", /without further summary text/);
});

test("a heading-only summary is labeled honestly and usage-only metadata leaves no disclosure", () => {
  const heading = render(appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("## Plan") }));
  assert.equal(heading.querySelector(".response-reasoning > summary > span:first-child")?.textContent, "Summary heading");
  assert.match(heading.querySelector(".response-reasoning-content")?.textContent || "", /without further summary text/);

  const usageOnly = render(appendEvent([], { event: "response_metadata", run_id: "run", ...metadata("", {
    usage_reported: true,
    usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16, reasoning_tokens: 0,
      prompt_reported: true, completion_reported: true, total_reported: true,
      reasoning_reported: false, reasoning_in_output: "unknown" },
  }) }));
  assert.equal(usageOnly.querySelector(".response-reasoning"), null);
  assert.equal(usageOnly.querySelector("article"), null);
});
