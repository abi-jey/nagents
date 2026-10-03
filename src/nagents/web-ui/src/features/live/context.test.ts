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

test("context details explain omissions and summary fallback without exposing a raw payload", () => {
  const document = new JSDOM(renderToStaticMarkup(createElement(VoiceContextDetails, { context, close() {} }))).window.document;
  assert.match(document.body.textContent || "", /Prepared brief and recent conversation/);
  assert.match(document.body.textContent || "", /Some history or non-text content was left out/);
  assert.match(document.body.textContent || "", /main assistant uses this chat’s saved history and summaries/);
  assert.equal(document.querySelector("pre, textarea"), null);
  assert.equal(contextLabel(context), "Chat brief");
  assert.equal(contextLabel({ ...context, method: "recent_fallback" }), "Chat context");
  assert.equal(contextLabel({ ...context, method: "none", mode: "none" }), "Fresh voice");
});
