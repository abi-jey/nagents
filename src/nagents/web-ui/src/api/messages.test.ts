import assert from "node:assert/strict";
import test from "node:test";
import { MessageQueue, queueMessage, queuedMessageFailure, MESSAGE_ID_RESERVE } from "./messages.js";

test("queued send uses a durable UUID and preserves it when an acknowledgement is lost", async (context) => {
  const queue = new MessageQueue(); let calls = 0;
  const first = queue.prepare("telegram-root", "same prompt", () => "uuid-1");
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    assert.equal(url, "/api/messages"); assert.equal(options.method, "POST");
    assert.equal(options.signal, undefined, "WS disconnect must not abort admission");
    assert.deepEqual(options.headers, { "X-Ngn-Token": "token", "Content-Type": "application/json" });
    assert.deepEqual(JSON.parse(options.body as string), first);
    if (!calls++) throw new Error("connection lost after admission");
    return Response.json({ session_id: first.session_id, message_id: first.message_id, status: "queued" });
  });
  await assert.rejects(queueMessage("token", first));
  const retry = queue.prepare("telegram-root", "same prompt", () => "uuid-2");
  assert.equal(first, retry); await queueMessage("token", retry); queue.confirmed(retry);
  assert.equal(queue.prepare("telegram-root", "same prompt", () => "uuid-3").message_id, "uuid-3");
  assert.equal(queue.prepare("other-root", "same prompt", () => "uuid-4").message_id, "uuid-4");
});
test("queued acknowledgement must match both root and message identity", async (context) => {
  context.mock.method(globalThis, "fetch", async () => Response.json({ session_id: "other", message_id: "uuid", status: "queued" }));
  await assert.rejects(queueMessage("token", { session_id: "root", message_id: "uuid", prompt: "text" }), /not confirmed/);
});
test("queued messages reserve the UUID's bytes in the existing 64 KiB JSON limit", () => {
  const base = "界".repeat(21800);
  const size = new TextEncoder().encode(JSON.stringify({ session_id: "root", prompt: base, message_id: MESSAGE_ID_RESERVE })).byteLength;
  const exact = base + "x".repeat(65536 - size);
  assert.equal(queuedMessageFailure(exact, "root"), "");
  assert.match(queuedMessageFailure(exact + "x", "root"), /64 KiB/);
});
