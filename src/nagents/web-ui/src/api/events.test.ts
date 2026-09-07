import assert from "node:assert/strict";
import test from "node:test";
import { readEvents } from "./events.js";
import type { WireEvent } from "../types.js";

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

test("malformed events cancel their reader instead of retrying", async () => {
  let cancelled = false;
  await assert.rejects(
    readEvents(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"unexpected":true}\n'));
        },
        cancel() {
          cancelled = true;
        },
      }),
      () => undefined,
    ),
    /Invalid event/,
  );
  assert.equal(cancelled, true);
});
