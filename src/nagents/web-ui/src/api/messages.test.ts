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

test("command admission keeps a distinct retry identity from identical literal chat text", () => {
  const queue = new MessageQueue();
  const text = queue.prepare("root", "/compact", () => "text");
  const command = queue.prepare("root", "/compact", () => "command", [], "compact");
  assert.notEqual(command.message_id, text.message_id);
  assert.equal(command.command, "compact");
  assert.equal(queue.prepare("root", "/compact", () => "unused", [], "compact"), command);
  queue.confirmed(command);
  assert.equal(queue.prepare("root", "/compact", () => "unused"), text);
  assert.equal(queue.prepare("root", "/compact", () => "next", [], "compact").message_id, "next");
});

test("delivery choices have separate retry identities and confirmation only clears its choice", async (context) => {
  const queue = new MessageQueue();
  const automatic = queue.prepare("root", "same", () => "automatic", ["file"]);
  const steer = queue.prepare("root", "same", () => "steer", ["file"], undefined, "steer");
  const queued = queue.prepare("root", "same", () => "queue", ["file"], undefined, "queue");
  assert.equal(Object.hasOwn(automatic, "delivery"), false);
  assert.equal(steer.delivery, "steer");
  assert.equal(queued.delivery, "queue");
  assert.equal(new Set([automatic.message_id, steer.message_id, queued.message_id]).size, 3);
  context.mock.method(globalThis, "fetch", async (_url: string, options: RequestInit) => {
    assert.deepEqual(JSON.parse(options.body as string), steer);
    throw new Error("lost acknowledgement");
  });
  await assert.rejects(queueMessage("token", steer));
  assert.equal(queue.prepare("root", "same", () => "unused", ["file"], undefined, "steer"), steer);
  queue.confirmed(steer);
  assert.equal(queue.prepare("root", "same", () => "new", ["file"], undefined, "steer").message_id, "new");
  assert.equal(queue.prepare("root", "same", () => "unused", ["file"]), automatic);
  assert.equal(queue.prepare("root", "same", () => "unused", ["file"], undefined, "queue"), queued);
  queue.forget("root");
  assert.equal(queue.prepare("root", "same", () => "forgotten", ["file"], undefined, "queue").message_id, "forgotten");
});

test("request size includes delivery, attachments and command bytes at the exact boundary", () => {
  for (const delivery of ["steer", "queue"] as const) {
    const attachments = ["file-界"];
    const envelope = { session_id: "root", prompt: "", message_id: MESSAGE_ID_RESERVE, attachments, delivery, command: "skill" };
    const overhead = new TextEncoder().encode(JSON.stringify(envelope)).byteLength;
    const exact = "x".repeat(65536 - overhead);
    assert.equal(queuedMessageFailure(exact, "root", attachments, delivery, "skill"), "");
    assert.match(queuedMessageFailure(exact + "x", "root", attachments, delivery, "skill"), /64 KiB/);
    assert.equal(queuedMessageFailure(exact + "x", "root", attachments), "");
  }
});
