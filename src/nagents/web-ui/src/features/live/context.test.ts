import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { contextLabel, readVoiceContext } from "./context.js";
import { VoiceContextDetails } from "./VoiceContextDetails.js";
import type { VoiceContext } from "./types.js";

const context: VoiceContext = { mode: "summary", method: "generated_summary", chat_session_id: "ngn-chat", fingerprint: "a".repeat(64), message_count: 6, characters: 2300, bytes: 2700, summary_included: true, omitted_messages: 8, omitted_content: true, notice: "A brief and recent messages were prepared." };

test("context metadata remains bound to the selected chat and the advertised byte budget", () => {
  assert.deepEqual(readVoiceContext(context, "ngn-chat"), context);
  assert.equal(readVoiceContext(context, "ngn-other"), undefined);
  for (const patch of [{ bytes: 7001 }, { bytes: 2200 }, { message_count: 15 }, { characters: -1 }, { omitted_messages: 1.5 }, { fingerprint: "not-a-digest" }, { mode: "all" }, { method: "guessed" }, { notice: "x".repeat(513) }])
    assert.equal(readVoiceContext({ ...context, ...patch }, "ngn-chat"), undefined);
  assert.equal(readVoiceContext({ ...context, mode: "none", method: "none", chat_session_id: "", fingerprint: "", bytes: 0, characters: 0, message_count: 0 }, "ngn-chat")?.mode, "none");
});

test("context stays compact with seed explanations collapsed and raw payloads fetched on demand", () => {
  const document = new JSDOM(renderToStaticMarkup(createElement(VoiceContextDetails, { context, token: "fixture-token", sessionId: "voice-one", close() {} }))).window.document;
  assert.equal(document.querySelector(".voice-context-summary")?.textContent, "Chat brief + recent messages · Partial history");
  const seedDetails = document.querySelector("details")!;
  assert.equal(seedDetails.open, false);
  assert.match(seedDetails.querySelector("summary")?.textContent || "", /6 text items · 2.7 KB/);
  assert.match(seedDetails.textContent || "", /not the model’s context window/);
  assert.match(seedDetails.textContent || "", /main assistant retains the saved conversation/);
  assert.match(seedDetails.textContent || "", /A brief and recent messages were prepared/);
  assert.equal(document.querySelector("pre, textarea"), null);
  assert.equal(contextLabel(context), "Chat brief");
  assert.equal(contextLabel({ ...context, method: "recent_fallback" }), "Chat context");
  assert.equal(contextLabel({ ...context, method: "none", mode: "none" }), "Fresh voice");
});
