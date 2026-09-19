import assert from "node:assert/strict";
import test from "node:test";
import { appendTrace, newAgent, removeAgent, traceMatches } from "./types.js";
import type { Design, TraceRecord } from "./types.js";

test("removing an agent repairs delegation edges, layout and entrypoint", () => {
  const design: Design = {
    version: 1, id: "team", entrypoint: "child", defaults: { provider: "primary", max_subagent_depth: 2 },
    providers: {}, secrets: {}, mcp_servers: {}, layout: { child: { x: 50, y: 100 } },
    agents: { parent: { ...newAgent(), invokes: [{ agent: "child", description: "Research" }] }, child: newAgent() },
    channels: { telegram: "child", support: "parent" },
  };
  const result = removeAgent(design, "child");
  assert.equal(result.entrypoint, "parent");
  assert.deepEqual(result.agents.parent.invokes, []);
  assert.deepEqual(result.layout, {});
  assert.deepEqual(result.channels, { support: "parent" });
  assert.equal(design.agents.parent.invokes.length, 1);
  assert.equal(removeAgent(result, "parent"), result);
});

test("incremental trace reads do not duplicate overlapping evidence", () => {
  const record = (sequence: number): TraceRecord => ({ sequence, kind: "http_request", timestamp: "now", data: { attempt_id: `attempt-${sequence}` } });
  const records = appendTrace([record(1), record(2)], [record(2), record(3)]);
  assert.deepEqual(records.map((entry) => entry.sequence), [1, 2, 3]);
  assert.equal(records[2].data.attempt_id, "attempt-3");
});

test("compact execution steps retain requests and task boundaries without token noise", () => {
  const record = (kind: string, data: Record<string, unknown> = {}): TraceRecord => ({ sequence: 1, kind, timestamp: "now", data });
  assert.equal(traceMatches(record("http_request_body"), "steps"), true);
  assert.equal(traceMatches(record("http_stream"), "steps"), false);
  assert.equal(traceMatches(record("http_stream"), "http_stream"), true);
  assert.equal(traceMatches(record("execution_event", { event: "task_completed" }), "steps"), true);
  assert.equal(traceMatches(record("agent_event", { event: { type: "text_chunk" } }), "steps"), false);
  assert.equal(traceMatches(record("agent_event", { event: { type: "text_done" } }), "steps"), true);
});
