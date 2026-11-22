import assert from "node:assert/strict";
import { test } from "node:test";
import { engineRequest, readEngineEvent } from "./engine-transport.js";
import type { Transport } from "./transport.js";

test("the engine transport forwards exact scope and uses only the browser token", async () => {
  const sent: { path: string; token?: string; body?: object }[] = [];
  const post: Transport["request"] = async <T>(path: string, token?: string, body?: object) => {
    sent.push({ path, token, body });
    return { request_id: "input", session_id: "session", run_id: null, status: "queued", warning: "" } as T;
  };
  const request = engineRequest(post);
  await request("/api/sessions/session/runs", "browser", { request_id: "input", prompt: "Check tests" });
  await request("/api/approvals", "browser", {
    approval_id: "approval", run_id: "run", call_id: "call", allow: false,
  });
  assert.deepEqual(sent, [
    { path: "/api/v1/sessions/session/runs", token: "browser", body: { request_id: "input", prompt: "Check tests", interactive: true } },
    { path: "/api/v1/approvals/approval", token: "browser", body: { run_id: "run", call_id: "call", allow: false } },
  ]);
  await assert.rejects(request("/api/approvals", "browser", { approval_id: "approval", allow: true }), /exact tool call/);
  assert.equal(sent.length, 2);
});

test("pending input without an activation ID is shown without inventing a cancellable run", async () => {
  const queue = { session_id: "session", paused: true, limit: 16, entries: [{ request_id: "input", run_id: null, prompt: "hello" }] };
  const request = engineRequest(async <T>() => queue as T);
  assert.deepEqual(await request("/api/sessions/session/queue"), {
    queue: { session_id: "session", paused: true, entries: [{ request_id: "input", prompt: "hello" }] }, limit: 16,
  });
});

test("versioned events preserve envelope scope and the resumable cursor", () => {
  const wire = { schema_version: 1, session_id: "session", run_id: "run", sequence: 13, timestamp_ms: 1,
    event: { type: "text.delta", text: "Hello", run_id: "not-the-envelope-run" } };
  assert.deepEqual(readEngineEvent(wire, "session", 12),
    { schema_version: 1, session_id: "session", run_id: "run", sequence: 13, timestamp_ms: 1,
      event: "text_delta", text: "Hello" });
  assert.equal(readEngineEvent(wire, "session", 13), undefined);
  assert.equal(readEngineEvent(wire, "another-session", 0), undefined);
  assert.equal(readEngineEvent({ ...wire, schema_version: 2 }, "session", 0), undefined);
  assert.equal(readEngineEvent({ ...wire, event: { type: "approval_requested" } }, "session", 12)?.event, "unknown");
});
