import assert from "node:assert/strict";
import { test } from "node:test";
import { acceptEvent, eventsUrl, type Event } from "./transport.js";
import { NativeTranscript } from "./transcript.js";

type Fields<T> = T extends Event ? Omit<T, "schema_version" | "sequence" | "run_id" | "session_id" | "timestamp_ms"> : never;

function event(sequence: number, event: Fields<Event>, run_id = "root", session_id = "session"): Event {
  return { schema_version: 1, sequence, run_id, session_id, timestamp_ms: sequence, ...event };
}

test("Rust journal text replays once, completion replaces deltas, and queued input is not executed history", () => {
  const feed = new NativeTranscript("session");
  const records = [
    event(1, { event: "run_queued", prompt: "Not yet run" }, "queued"),
    event(2, { event: "run_started", prompt: "Hello" }),
    event(3, { event: "text_delta", text: "**H" }),
    event(4, { event: "text_delta", text: "ello**" }),
    event(5, { event: "text_completed", text: "**Hello**" }),
    event(6, { event: "run_finished", status: "completed", error: "" }),
    event(7, { event: "run_finished", status: "cancelled", error: "" }, "queued"),
  ];
  for (const record of [...records, ...records]) feed.apply(record);
  assert.deepEqual(feed.snapshot().map(entry => [entry.kind, entry.text, entry.streaming]), [
    ["user", "Hello", undefined], ["assistant", "**Hello**", false],
  ]);
});

test("Tool results and frozen approval timing are scoped to the exact invocation and stream", () => {
  const feed = new NativeTranscript("session", "child");
  const call = { call_id: "provider-tool-id", name: "write", arguments: { path: "hello.txt" } };
  feed.apply(event(1, { event: "run_started", prompt: "Inspect" }, "child"));
  feed.apply(event(2, { event: "tool_started", call }, "child"));
  feed.apply(event(3, { event: "tool_approval_timing", call_id: call.call_id, elapsed_ms: 350, waiting: true }, "child"));
  feed.apply(event(9, { event: "run_finished", status: "failed", error: "foreign" }, "root"));
  assert.equal(feed.snapshot().at(-1)?.state, "Waiting for approval");
  assert.equal(feed.snapshot().at(-1)?.durationMs, 350);
  feed.apply(event(4, { event: "approval_requested", approval_id: "decision", call, preview: "diff" }, "child"));
  feed.apply(event(5, { event: "approval_resolved", approval_id: "decision", allowed: false }, "child"));
  feed.apply(event(6, { event: "tool_completed", result: { ...call, result: null, error: "Denied" }, duration_ms: 370 }, "child"));
  assert.equal(feed.snapshot().at(-1)?.state, "Approval denied");
  assert.equal(feed.snapshot().at(-1)?.approval, "Denied");
  assert.equal(feed.snapshot().at(-1)?.error, "Denied");
  assert.equal(feed.snapshot().at(-1)?.durationMs, 370);
  assert.equal(feed.snapshot().length, 2);
});

test("an interrupted run settles only its unfinished tools without replaying an approval", () => {
  const feed = new NativeTranscript("session");
  const call = { call_id: "same-id", name: "write", arguments: { path: "hello.txt" } };
  feed.apply(event(1, { event: "run_started", prompt: "Edit" }, "old"));
  feed.apply(event(2, { event: "tool_started", call }, "old"));
  feed.apply(event(3, { event: "approval_requested", approval_id: "decision", call, preview: "diff" }, "old"));
  feed.apply(event(4, { event: "tool_started", call }, "new"));
  feed.apply(event(5, { event: "run_finished", status: "interrupted", error: "Host stopped" }, "old"));
  feed.apply(event(6, { event: "approval_resolved", approval_id: "decision", allowed: true }, "old"));
  assert.deepEqual(feed.snapshot().filter(entry => entry.kind === "tool").map(entry => [entry.runId, entry.state, entry.approval]), [
    ["old", "Interrupted", undefined], ["new", "Running", undefined],
  ]);
});

test("Only the rendered Python archive format projects labelled notices into chat messages", () => {
  const id = `python-archive-${"a".repeat(64)}`;
  const imported = new NativeTranscript(id);
  imported.apply(event(1, { event: "notice", text: "Read-only Python archive" }, "", id));
  imported.apply(event(2, { event: "notice", text: "assistant\nAn old answer" }, "", id));
  assert.deepEqual(imported.snapshot().map(entry => entry.kind), ["status", "assistant"]);
  const live = new NativeTranscript("session");
  live.apply(event(1, { event: "notice", text: "assistant\nAn ordinary notice" }));
  assert.equal(live.snapshot()[0].kind, "status");
});

test("Event cursors exclude foreign sessions, unsupported protocol versions and duplicate sequences", () => {
  const record = event(8, { event: "notice", text: "Hello" });
  assert(acceptEvent(record, "session", 7));
  assert.equal(acceptEvent(record, "session", 8), false);
  assert.equal(acceptEvent(record, "other", 0), false);
  assert.equal(acceptEvent({ ...record, schema_version: 2 }, "session", 0), false);
  const url = new URL(eventsUrl({ protocol: "http:", host: "127.0.0.1:8765" }, "a&b", 7));
  assert.equal(url.protocol, "ws:");
  assert.equal(url.searchParams.get("session_id"), "a&b");
  assert.equal(url.searchParams.get("after"), "7");
  assert.deepEqual([...url.searchParams.keys()], ["session_id", "after"]);
});
