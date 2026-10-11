import assert from "node:assert/strict";
import { test } from "node:test";
import { advanceTo, applyEvent, nextEventTime, queueMessage, queuedCount, startDemo } from "./engine";
import type { ActivityState, Message } from "./types";

function userMessages(state: ActivityState, agentId: string): Message[] {
  return state.agents[agentId].records.flatMap(record => record.kind === "message" && record.message.role === "user" ? [record.message] : []);
}

test("notes wait through tool completion and are picked up only at the addressed agent's response boundary", () => {
  const initial = startDemo();
  const queued = queueMessage(initial, "research", "Check the download authorization too.");
  assert.equal(queuedCount(queued.agents.research), 1);
  assert.equal(userMessages(initial, "research").length, 0, "queueing must not mutate the previous state");
  const toolDone = advanceTo(queued, 12);
  assert.equal(userMessages(toolDone, "research")[0].delivery, "queued");
  const pickedUp = advanceTo(toolDone, 13);
  assert.equal(userMessages(pickedUp, "research")[0].delivery, "picked_up");
  assert.equal(userMessages(pickedUp, "research")[0].pickedUpAt, 13);
  assert.equal(userMessages(pickedUp, "builder").length, 0);
  assert.equal(queuedCount(pickedUp.agents.research), 0);
});

test("multiple queued messages preserve order and do not interrupt a running tool", () => {
  let state = queueMessage(startDemo(), "builder", "Preserve the original file.");
  state = queueMessage(state, "builder", "Keep the download action visible on mobile.");
  const current = state.agents.builder.records.find(record => record.kind === "tool");
  assert.equal(current?.kind === "tool" && current.tool.status, "running");
  assert.equal(queuedCount(state.agents.builder), 2);
  state = advanceTo(state, 13);
  assert.equal(queuedCount(state.agents.builder), 2, "another agent's response must not consume these messages");
  state = advanceTo(state, 17);
  assert.deepEqual(userMessages(state, "builder").map(message => [message.text, message.delivery]), [
    ["Preserve the original file.", "picked_up"],
    ["Keep the download action visible on mobile.", "picked_up"],
  ]);
});

test("approval and tool execution remain separate from message delivery", () => {
  let state = queueMessage(startDemo(), "tests", "Please report the exact failure.");
  assert.equal(state.agents.tests.status, "waiting");
  state = advanceTo(state, 19);
  assert.equal(state.agents.tests.status, "running");
  assert.equal(queuedCount(state.agents.tests), 1);
  state = advanceTo(state, 29);
  assert.equal(queuedCount(state.agents.tests), 1);
  state = advanceTo(state, 30);
  assert.equal(queuedCount(state.agents.tests), 0);
  assert.equal(state.agents.tests.status, "failed");
});

test("events are idempotent and cannot mutate an unrelated agent", () => {
  const initial = startDemo();
  const event = { id: "custom-message", at: 9, agentId: "research", type: "message" as const, role: "assistant" as const, text: "One message." };
  const once = applyEvent(initial, event);
  assert.equal(applyEvent(once, event), once);
  assert.equal(once.agents.builder, initial.agents.builder);
  assert.equal(once.agents.research.records.length, initial.agents.research.records.length + 1);
});

test("completed agents can pick up a follow-up without replaying tools", () => {
  const initial = advanceTo(startDemo(), 48);
  const before = initial.agents.layout.records.length;
  const queued = queueMessage(initial, "layout", "Also check landscape orientation.");
  assert.equal(nextEventTime(queued), 51);
  const delivered = advanceTo(queued, 51);
  assert.equal(userMessages(delivered, "layout")[0].delivery, "picked_up");
  assert.equal(delivered.agents.layout.records.length, before + 2);
  assert.deepEqual(delivered.followUps, {});
});

test("a message arriving just after the final boundary gets a follow-up turn", () => {
  let state = advanceTo(startDemo(), 25);
  state = queueMessage(state, "research", "One last check.");
  state = advanceTo(state, 26);
  assert.equal(state.agents.research.status, "completed");
  assert.equal(queuedCount(state.agents.research), 1);
  assert.equal(state.followUps.research, 29);
  state = advanceTo(state, 29);
  assert.equal(queuedCount(state.agents.research), 0);
});

test("empty notes, unknown agents, and backwards time do not change the state", () => {
  const state = startDemo();
  assert.equal(queueMessage(state, "research", "  "), state);
  assert.equal(queueMessage(state, "missing", "Hello"), state);
  assert.equal(advanceTo(state, 0), state);
});
