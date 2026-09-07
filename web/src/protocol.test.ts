import assert from "node:assert/strict";
import test from "node:test";
import { appendEvent, readEvents, type WireEvent } from "./protocol.js";

test("NDJSON tolerates arbitrary UTF-8/chunk boundaries", async () => {
  const bytes = new TextEncoder().encode(
    '{"event":"text_chunk","chunk":"\u00e9"}\n{"event":"done"}\n',
  );
  const events: WireEvent[] = [];
  await readEvents(
    new ReadableStream({
      start(controller) {
        for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
        controller.close();
      },
    }),
    (event) => events.push(event),
  );
  assert.equal(events[0].chunk, "\u00e9");
  assert.equal(events[1].event, "done");
});

test("partial events fail instead of silently succeeding", async () => {
  await assert.rejects(
    readEvents(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"event":"done"}'));
          controller.close();
        },
      }),
      () => undefined,
    ),
    /partway/,
  );
});

test("text finalization does not duplicate streamed text and errors retain it", () => {
  let entries = appendEvent([], { event: "text_chunk", chunk: "partial" });
  entries = appendEvent(entries, { event: "text_done", text: "partial text" });
  entries = appendEvent(entries, { event: "error", message: "disconnected" });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].text, "partial text");
});

test("tool output is matched by call ID rather than tool name", () => {
  let entries = appendEvent([], {
    event: "tool_call",
    id: "one",
    name: "read_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "tool_call",
    id: "two",
    name: "read_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "tool_output",
    call_id: "one",
    text: "first result",
  });
  assert.match(entries[0].text, /first result/);
  assert.doesNotMatch(entries[1].text, /first result/);
});

test("background notices do not split or duplicate an assistant message", () => {
  let entries = appendEvent([], { event: "text_chunk", chunk: "part" });
  entries = appendEvent(entries, {
    event: "notice",
    text: "background task completed",
  });
  entries = appendEvent(entries, { event: "text_chunk", chunk: " two" });
  entries = appendEvent(entries, { event: "text_done", text: "part two" });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].text, "part two");
});

test("a new run never appends to a cancelled partial response", () => {
  let entries = appendEvent([], { event: "text_chunk", chunk: "partial" });
  entries = appendEvent(entries, {
    event: "run_finished",
    status: "cancelled",
  });
  entries = appendEvent(entries, {
    event: "text_chunk",
    chunk: "new response",
  });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].text, "partial");
});
