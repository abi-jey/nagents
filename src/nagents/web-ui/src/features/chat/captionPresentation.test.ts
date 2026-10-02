import assert from "node:assert/strict";
import test from "node:test";
import { conversationEntries } from "./captionPresentation.js";
import type { Entry } from "./transcript.js";

test("voice captions replace only the proven duplicate caller display, retaining typed messages and assistant work", () => {
  const caption: Entry = { id: "live-caption:1", historyId: "live-caption:1", kind: "live_caption", text: "Check the file", liveCaption: { sessionId: "call", speaker: "user", seq: 1, start: 0, end: 100, anchorHistoryId: "" } };
  const entries: Entry[] = [caption,
    { id: "delegated", kind: "user", text: "Check the file", voice: true, voiceSessionId: "call" },
    { id: "typed", kind: "user", text: "Check the file" },
    { id: "unverified", kind: "user", text: "Check the file", voiceSessionId: "call" },
    { id: "answer", kind: "assistant", text: "The file is ready." },
    { id: "older-voice", kind: "user", text: "Earlier voice request", voice: true, voiceSessionId: "another-call" },
  ];
  assert.deepEqual(conversationEntries(entries).map(entry => entry.id), ["live-caption:1", "typed", "unverified", "answer", "older-voice"]);
  assert.equal(entries.length, 6, "the stored/model history is never removed");
});
