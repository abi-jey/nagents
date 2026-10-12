import assert from "node:assert/strict";
import { test } from "node:test";
import { advanceTo, applyEvent, nextEventTime, queueMainMessage, queueMessage, queuedCount, startDemo, visibleAgents } from "./engine";
import { DEMO_END, FIRST_SCENARIO_END } from "./fixtures";
import type { ActivityState, Message } from "./types";

function userMessages(state: ActivityState, agentId: string): Message[] {
  return state.agents[agentId].records.flatMap(record => record.kind === "message" && record.message.role === "user" ? [record.message] : []);
}

function activeDemo(): ActivityState { return advanceTo(startDemo(), 8); }

test("notes wait through tool completion and are picked up only at the addressed agent's response boundary", () => {
  const initial = activeDemo();
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
  let state = queueMessage(activeDemo(), "builder", "Preserve the original file.");
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
  let state = queueMessage(activeDemo(), "tests", "Please report the exact failure.");
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
  const initial = activeDemo();
  const event = { id: "custom-message", at: 9, agentId: "research", type: "message" as const, role: "assistant" as const, text: "One message." };
  const once = applyEvent(initial, event);
  assert.equal(applyEvent(once, event), once);
  assert.equal(once.agents.builder, initial.agents.builder);
  assert.equal(once.agents.research.records.length, initial.agents.research.records.length + 1);
});

test("completed agents can pick up a follow-up without replaying tools", () => {
  const initial = advanceTo(startDemo(), DEMO_END);
  const before = initial.agents.layout.records.length;
  const queued = queueMessage(initial, "layout", "Also check landscape orientation.");
  assert.equal(nextEventTime(queued), DEMO_END + 3);
  const delivered = advanceTo(queued, DEMO_END + 3);
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

test("a resumed tool cancels an idle reply timer and notes wait for the real response", () => {
  let state = advanceTo(startDemo(), 36);
  assert.equal(state.agents.tests.status, "failed");
  state = queueMessage(state, "tests", "Please include the rerun results.");
  assert.equal(state.followUps.tests, 39);
  state = advanceTo(state, 37);
  assert.equal(state.followUps.tests, undefined);
  assert.equal(queuedCount(state.agents.tests), 1);
  state = advanceTo(state, 39);
  const retest = state.agents.tests.records.find(record => record.kind === "tool" && record.tool.id === "retest");
  assert.equal(retest?.kind === "tool" && retest.tool.status, "running");
  assert.equal(queuedCount(state.agents.tests), 1, "an obsolete idle timer must not interrupt the running tool");
  state = advanceTo(state, 44);
  assert.equal(queuedCount(state.agents.tests), 1, "tool completion alone is not a response boundary");
  state = advanceTo(state, 45);
  assert.equal(userMessages(state, "tests")[0].pickedUpAt, 45);
  assert.equal(queuedCount(state.agents.tests), 0);
  assert.equal(state.followUps.tests, undefined);
});

test("an explicit response clears the pending idle timer without a second acknowledgement", () => {
  let state = queueMessage(advanceTo(startDemo(), 48), "layout", "One final layout note.");
  assert.equal(state.followUps.layout, 51);
  state = applyEvent(state, { id: "layout-early-reply", at: 49, agentId: "layout", type: "response_boundary" });
  assert.equal(state.followUps.layout, undefined);
  assert.equal(userMessages(state, "layout")[0].pickedUpAt, 49);
  const before = state.agents.layout.records.length;
  state = advanceTo(state, 51);
  assert.equal(state.agents.layout.records.length, before);
});

test("empty notes, unknown agents, and backwards time do not change the state", () => {
  const state = activeDemo();
  assert.equal(queueMessage(state, "research", "  "), state);
  assert.equal(queueMessage(state, "missing", "Hello"), state);
  assert.equal(advanceTo(state, 0), state);
});

test("the original request comes before delegation, and each agent appears only when spawned", () => {
  const initial = startDemo();
  assert.equal(initial.now, 0);
  assert.deepEqual(initial.mainMessages.map(message => [message.id, message.role]), [["main-request", "user"]]);
  assert.deepEqual(visibleAgents(initial), []);
  assert.equal(queueMessage(initial, "builder", "Too early"), initial);
  assert.equal(nextEventTime(initial), 1);
  const announced = advanceTo(initial, 1);
  assert.equal(announced.mainMessages[1].originMessageId, "main-request");
  assert.deepEqual(visibleAgents(announced), []);
  const roots = advanceTo(announced, 2);
  assert.deepEqual(visibleAgents(roots).map(agent => agent.definition.id), ["research", "builder", "tests"]);
  assert.deepEqual(roots.agents.layout.records, []);
  assert.ok(visibleAgents(roots).every(agent => agent.startedAt === 2));
  assert.equal(nextEventTime(advanceTo(roots, 4)), 5);
  const nested = advanceTo(roots, 5);
  assert.equal(visibleAgents(nested).length, 4);
  assert.equal(nested.agents.layout.startedAt, 5);
  assert.equal(nested.agents.layout.status, "running");
  assert.equal(nested.agents.layout.records[0].kind, "message");
});

test("results reach the correct parent and original request only after completion", () => {
  const before = advanceTo(startDemo(), 5);
  assert.equal(before.mainMessages.some(message => message.returnedAgentId), false);
  assert.equal(before.agents.builder.records.some(record => record.kind === "message" && record.message.returnedAgentId), false);
  const nestedDone = advanceTo(before, 6);
  const nestedReturn = nestedDone.agents.builder.records.flatMap(record => record.kind === "message" && record.message.returnedAgentId ? [record.message] : []);
  assert.deepEqual(nestedReturn.map(message => [message.at, message.returnedAgentId, message.originMessageId]), [[6, "layout", "main-request"]]);
  assert.equal(nestedDone.mainMessages.some(message => message.returnedAgentId), false);
  const beforeResearch = advanceTo(nestedDone, 25);
  const premature = { id: "premature-return", at: 25, agentId: "research", type: "return" as const, text: "Not done yet" };
  assert.equal(applyEvent(beforeResearch, premature), beforeResearch);
  const researchDone = advanceTo(beforeResearch, 26);
  assert.deepEqual(researchDone.mainMessages.filter(message => message.returnedAgentId).map(message => message.returnedAgentId), ["research"]);
  const done = advanceTo(researchDone, 48);
  assert.deepEqual(done.mainMessages.filter(message => message.returnedAgentId).map(message => [message.at, message.returnedAgentId, message.originMessageId]), [
    [26, "research", "main-request"], [35, "builder", "main-request"], [46, "tests", "main-request"],
  ]);
  assert.deepEqual(advanceTo(done, 48).mainMessages, done.mainMessages, "revisiting the clock must not duplicate returns");
});

test("talking to the main assistant leaves child tools and queued notes untouched", () => {
  const initial = queueMessage(activeDemo(), "research", "Check the download authorization.");
  const sent = queueMainMessage(initial, "Can you explain the plan while they work?");
  assert.equal(sent.agents, initial.agents);
  assert.equal(sent.followUps, initial.followUps);
  assert.equal(initial.pendingMainReplies.length, 0, "queueing preserves the previous snapshot");
  assert.equal(sent.mainMessages.at(-1)?.role, "user");
  assert.equal(sent.pendingMainReplies.length, 1);
  assert.equal(nextEventTime(sent), 9);
  const replied = advanceTo(sent, 9);
  assert.equal(replied.agents, initial.agents);
  assert.equal(queuedCount(replied.agents.research), 1);
  assert.equal(replied.mainMessages.at(-1)?.originMessageId, sent.mainMessages.at(-1)?.id);
  assert.match(replied.mainMessages.at(-1)?.text ?? "", /Simulated response/);
  assert.equal(replied.pendingMainReplies.length, 0);
  assert.equal(queuedCount(advanceTo(replied, 13).agents.research), 0);
});

test("main replies preserve order when several messages arrive between timeline ticks", () => {
  let state = advanceTo(startDemo(), DEMO_END);
  state = queueMainMessage(state, "First follow-up");
  state = queueMainMessage(state, "Second follow-up");
  const pending = state.pendingMainReplies;
  assert.equal(nextEventTime(state), DEMO_END + 1);
  assert.equal(new Set(pending.map(reply => reply.id)).size, 2);
  const done = advanceTo(state, DEMO_END + 7);
  assert.deepEqual(done.mainMessages.slice(-2).map(message => [message.id, message.at, message.originMessageId]), pending.map(reply => [reply.id, DEMO_END + 1, reply.originMessageId]));
  assert.equal(done.mainMessages.filter(message => message.role === "user").slice(-2)[0].text, "First follow-up");
  assert.deepEqual(advanceTo(done, DEMO_END + 8).mainMessages, done.mainMessages);
  assert.equal(done.agents, state.agents);
});

test("jumping playback keeps main replies, returns, and late child notes in chronological order", () => {
  let queued = advanceTo(startDemo(), 25);
  queued = queueMessage(queued, "research", "A late child note");
  queued = queueMainMessage(queued, "A main follow-up");
  const jumped = advanceTo(queued, 48);
  let stepped = queued;
  for (let at = 26; at <= 48; at += 1) stepped = advanceTo(stepped, at);
  assert.deepEqual(jumped, stepped);
  assert.equal(userMessages(jumped, "research")[0].pickedUpAt, 29);
  assert.deepEqual(jumped.mainMessages.map(message => message.at), [...jumped.mainMessages.map(message => message.at)].sort((a, b) => a - b));
});

test("replay clears interactive messages, pending replies, and all previous child activity", () => {
  let finished = advanceTo(startDemo(), 48);
  finished = queueMainMessage(finished, "One more question");
  finished = queueMessage(finished, "builder", "A new change");
  const replay = startDemo();
  assert.equal(replay.now, 0);
  assert.deepEqual(replay.pendingMainReplies, []);
  assert.deepEqual(replay.followUps, {});
  assert.equal(replay.mainMessages.length, 1);
  assert.ok(Object.values(replay.agents).every(agent => agent.records.length === 0));
  assert.equal(finished.pendingMainReplies.length, 1);
  assert.equal(queuedCount(finished.agents.builder), 1);
  assert.equal(queueMainMessage(replay, "  "), replay);
});

test("next-event playback reaches a second delegation wave and preserves chronological results", () => {
  const firstDone = advanceTo(startDemo(), FIRST_SCENARIO_END);
  assert.equal(visibleAgents(firstDone).length, 4);
  assert.equal(nextEventTime(firstDone), 50);
  const request = advanceTo(firstDone, 50);
  assert.equal(request.mainMessages.at(-1)?.id, "main-followup-request");
  assert.equal(nextEventTime(request), 51);
  assert.equal(nextEventTime(advanceTo(request, 51)), 52);
  const roots = advanceTo(request, 52);
  assert.equal(visibleAgents(roots).length, 7);
  assert.equal(nextEventTime(advanceTo(roots, 54)), 55);
  const finished = advanceTo(firstDone, DEMO_END);
  let stepped = firstDone;
  for (let at = FIRST_SCENARIO_END + 1; at <= DEMO_END; at += 1) stepped = advanceTo(stepped, at);
  assert.deepEqual(finished, stepped);
  assert.equal(visibleAgents(finished).length, 9);
  assert.ok(visibleAgents(finished).every(agent => agent.status === "completed"));
  assert.equal(finished.mainMessages.some(message => message.returnedAgentId === "security"), false);
  assert.ok(finished.agents.reviewer.records.some(record => record.kind === "message" && record.message.returnedAgentId === "security"));
  assert.equal(finished.mainMessages.some(message => message.returnedAgentId === "access-audit"), false);
  assert.equal(finished.agents.reviewer.records.some(record => record.kind === "message" && record.message.returnedAgentId === "access-audit"), false);
  assert.ok(finished.agents.security.records.some(record => record.kind === "message" && record.message.returnedAgentId === "access-audit"));
  assert.deepEqual(finished.mainMessages.map(message => message.at), [...finished.mainMessages.map(message => message.at)].sort((a, b) => a - b));
});

test("a depth-three delegation runs a read-only tool and returns only to its direct parent", () => {
  const before = advanceTo(startDemo(), 56);
  assert.equal(visibleAgents(before).some(agent => agent.definition.id === "access-audit"), false);
  assert.equal(nextEventTime(before), 57);
  const spawned = advanceTo(before, 57);
  assert.equal(spawned.agents["access-audit"].definition.parentId, "security");
  const running = advanceTo(spawned, 58);
  const tool = running.agents["access-audit"].records.find(record => record.kind === "tool");
  assert.equal(tool?.kind, "tool");
  if (tool?.kind !== "tool") throw new Error("Expected the ownership read fixture");
  assert.equal(tool.tool.name, "read_file");
  assert.equal(tool.tool.status, "running");
  const checked = advanceTo(running, 61);
  assert.equal(checked.agents["access-audit"].status, "running", "a finished tool does not complete the agent");
  assert.equal(checked.agents.security.records.some(record => record.kind === "message" && record.message.returnedAgentId === "access-audit"), false);
  const done = advanceTo(checked, 63);
  assert.equal(done.agents["access-audit"].status, "completed");
  assert.equal(done.agents["access-audit"].finishedAt, 63);
  const results = done.agents.security.records.filter(record => record.kind === "message" && record.message.returnedAgentId === "access-audit");
  assert.equal(results.length, 1);
  assert.equal(done.agents.security.status, "running", "Security continues its own check after receiving the nested result");
});
