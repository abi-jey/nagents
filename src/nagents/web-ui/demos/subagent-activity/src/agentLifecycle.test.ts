import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveAgentLifecycle } from "./agentLifecycle";
import { advanceTo, queueMainMessage, queueMessage, startDemo } from "./engine";
import { DEMO_END } from "./fixtures";
import { mainAgentSnapshot } from "./mainRuntime";

test("creation metadata keeps the real spawning parent, time and originating request", () => {
  const state = advanceTo(startDemo(), 55);
  const explorer = resolveAgentLifecycle(state.agents.research, state);
  assert.equal(explorer.originLabel, "Spawned by Assistant");
  assert.equal(explorer.startedAt, 2);
  assert.equal(explorer.originMessageId, "main-request");
  const security = resolveAgentLifecycle(state.agents.security, state);
  assert.equal(security.originLabel, "Spawned by Reviewer");
  assert.equal(security.parentName, "Reviewer");
  assert.equal(security.startedAt, 55);
  assert.equal(security.originMessageId, "main-followup-request");
  const deep = advanceTo(state, 57);
  const accessAudit = resolveAgentLifecycle(deep.agents["access-audit"], deep);
  assert.equal(accessAudit.originLabel, "Spawned by Security");
  assert.equal(accessAudit.startedAt, 57);
  assert.equal(accessAudit.originMessageId, "main-followup-request");
  const main = resolveAgentLifecycle(mainAgentSnapshot(state, {}), state);
  assert.equal(main.originLabel, "Conversation started");
  assert.equal(main.startedAt, 0);
});

test("a queued note does not replace an active tool's running state", () => {
  const state = queueMessage(advanceTo(startDemo(), 8), "research", "Check the original download too.");
  const lifecycle = resolveAgentLifecycle(state.agents.research, state);
  assert.equal(lifecycle.state, "running");
  assert.equal(lifecycle.label, "Running");
  assert.equal(lifecycle.detail, "Searching the chat renderer and attachment API");
  assert.equal(lifecycle.queuedMessages, 1);
});

test("a tool awaiting approval is waiting until approval actually starts it", () => {
  const waiting = queueMessage(advanceTo(startDemo(), 8), "tests", "Please include the filename check.");
  const before = resolveAgentLifecycle(waiting.agents.tests, waiting);
  assert.equal(before.state, "waiting");
  assert.equal(before.label, "Waiting");
  assert.equal(before.detail, "Approval · Run focused image regressions");
  assert.equal(before.queuedMessages, 1);
  const running = advanceTo(waiting, 19);
  assert.equal(resolveAgentLifecycle(running.agents.tests, running).state, "running");
});

test("completed tools do not label the agent done and failed tool history does not survive a retry", () => {
  const toolFinished = advanceTo(startDemo(), 12);
  const processing = resolveAgentLifecycle(toolFinished.agents.research, toolFinished);
  assert.equal(processing.state, "running");
  assert.equal(processing.detail, "Repository research");
  const failed = advanceTo(toolFinished, 30);
  assert.equal(resolveAgentLifecycle(failed.agents.tests, failed).state, "failed");
  const recovered = advanceTo(failed, 37);
  const running = resolveAgentLifecycle(recovered.agents.tests, recovered);
  assert.equal(running.state, "running");
  assert.equal(running.detail, "Running the same 12 focused checks");
  const completed = advanceTo(recovered, 46);
  assert.equal(resolveAgentLifecycle(completed.agents.tests, completed).label, "Done");
});

test("settled agents with a note wait for the next response rather than falsely running or done", () => {
  const completed = advanceTo(startDemo(), DEMO_END);
  assert.equal(resolveAgentLifecycle(completed.agents.layout, completed).label, "Done");
  const queued = queueMessage(completed, "layout", "Check landscape as well.");
  const lifecycle = resolveAgentLifecycle(queued.agents.layout, queued);
  assert.equal(lifecycle.state, "waiting");
  assert.equal(lifecycle.detail, "Next response");
  assert.equal(lifecycle.queuedMessages, 1);
  const pickedUp = advanceTo(queued, DEMO_END + 3);
  assert.equal(resolveAgentLifecycle(pickedUp.agents.layout, pickedUp).state, "done");
  const scheduled = { ...completed, followUps: { layout: 100 } };
  assert.equal(resolveAgentLifecycle(scheduled.agents.layout, scheduled).state, "waiting");
});

test("failed agents awaiting a follow-up become waiting, then actual retry takes precedence", () => {
  const failed = queueMessage(advanceTo(startDemo(), 36), "tests", "Please rerun the fix.");
  assert.equal(resolveAgentLifecycle(failed.agents.tests, failed).detail, "Next response");
  const recovered = advanceTo(failed, 37);
  const lifecycle = resolveAgentLifecycle(recovered.agents.tests, recovered);
  assert.equal(lifecycle.state, "running");
  assert.equal(lifecycle.queuedMessages, 1, "the tool must not deliver the queued note");
});

test("main waits on child results and only its own pending response makes it running", () => {
  const state = advanceTo(startDemo(), 8);
  const before = resolveAgentLifecycle(mainAgentSnapshot(state, {}), state);
  assert.equal(before.state, "waiting");
  assert.equal(before.detail, "Results from Explorer, Builder, Verifier");
  assert.doesNotMatch(before.detail, /Layout/, "nested results belong to Builder");
  const queued = queueMainMessage(state, "Keep the mobile download easy to reach.");
  const responding = resolveAgentLifecycle(mainAgentSnapshot(queued, {}), queued);
  assert.equal(responding.state, "running");
  assert.equal(responding.queuedMessages, 1);
  const replied = advanceTo(queued, 9);
  assert.equal(resolveAgentLifecycle(mainAgentSnapshot(replied, {}), replied).state, "waiting");
});

test("main idle and failed-child attention remain explicit and never invent a new delegation", () => {
  const initial = advanceTo(startDemo(), 1);
  assert.equal(resolveAgentLifecycle(mainAgentSnapshot(initial, {}), initial).detail, "Your next message");
  const completed = advanceTo(initial, DEMO_END);
  assert.equal(resolveAgentLifecycle(mainAgentSnapshot(completed, {}), completed).detail, "Your next message");
  const failure = { ...completed, agents: { ...completed.agents, tests: { ...completed.agents.tests, status: "failed" as const, activity: "The checks failed" } } };
  const lifecycle = resolveAgentLifecycle(mainAgentSnapshot(failure, {}), failure);
  assert.equal(lifecycle.state, "waiting");
  assert.equal(lifecycle.detail, "Verifier needs attention");
});

test("scripted main requests are responding until their own answer arrives", () => {
  for (const at of [0, 10, 50, 61]) {
    const request = advanceTo(startDemo(), at);
    assert.equal(request.pendingMainReplies.length, 0, "scripted responses do not use the interactive reply queue");
    const responding = resolveAgentLifecycle(mainAgentSnapshot(request, {}), request);
    assert.equal(responding.state, "running", `the request at ${at}s is unanswered`);
    assert.equal(responding.detail, "Responding to your message");
    const answered = advanceTo(request, at + 1);
    assert.equal(resolveAgentLifecycle(mainAgentSnapshot(answered, {}), answered).state, "waiting", `the answer at ${at + 1}s settles the main response`);
  }
});

test("a delegated result does not stand in for the main assistant's response", () => {
  const request = advanceTo(startDemo(), 50);
  const withResult = {
    ...request,
    mainMessages: [...request.mainMessages, { id: "result-before-response", at: 50, role: "assistant" as const, text: "A delegated result.", returnedAgentId: "research", originMessageId: "main-followup-request" }],
  };
  const lifecycle = resolveAgentLifecycle(mainAgentSnapshot(withResult, {}), withResult);
  assert.equal(lifecycle.state, "running");
  assert.equal(lifecycle.detail, "Responding to your message");
});
