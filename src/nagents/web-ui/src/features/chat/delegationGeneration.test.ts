import assert from "node:assert/strict";
import test from "node:test";
import type { RetainedTask, Snapshot, WireEvent } from "../../types.js";
import type { TaskActivity, TeamAgent } from "../team/model.js";
import { DelegationFlow } from "./delegationFlow.js";
import { applySnapshot } from "./liveTranscript.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";

const runId = "root-run";

function agent(id: string, phase: TeamAgent["phase"] = "running"): TeamAgent {
  return {
    id, name: id === "child-a" ? "gentle willow" : id === "child-b" ? "bright reed" : id,
    profile: "mini", parentId: "", prompt: `Check ${id}.`, phase, reason: "", provider: "", providerKind: "",
    model: "", startedAt: 1, updatedAt: 2, canMessage: true, messageReason: "",
  };
}

function options(agents: TeamAgent[]) {
  return { agents, activity: [] as TaskActivity[], inspect: (_id: string) => undefined };
}

function delegateCall(entries: Entry[], taskId: string, callId = `call-${taskId}`): Entry[] {
  return appendEvent(entries, {
    event: "tool_call", run_id: runId, id: callId, name: "to_agent",
    arguments: { agent: "mini", prompt: `Check ${taskId}.` },
  });
}

function delegateResult(entries: Entry[], taskId: string, callId = `call-${taskId}`): Entry[] {
  return appendEvent(entries, {
    event: "tool_result", run_id: runId, id: callId, name: "to_agent", result: { session_id: taskId, status: "running" },
  });
}

function taskStarted(entries: Entry[], taskId: string): Entry[] {
  return appendEvent(entries, {
    event: "task_started", run_id: runId, session_id: taskId, parent_session_id: "", root_session_id: "root", activation: 0,
    title: agent(taskId).name, prompt: `Check ${taskId}.`,
  });
}

function taskCompleted(entries: Entry[], taskId: string, status: "completed" | "failed" | "cancelled" = "completed",
  parentResponseId = "", parentResponseSessionId = "", completedAt?: number): Entry[] {
  return appendEvent(entries, {
    event: "task_completed", run_id: runId, session_id: taskId, parent_session_id: "", root_session_id: "root", activation: 0,
    title: agent(taskId).name, status, result: `Result for ${taskId}.`,
    ...(parentResponseId ? { parent_response_id: parentResponseId } : {}),
    ...(parentResponseSessionId ? { parent_response_session_id: parentResponseSessionId } : {}),
    ...(completedAt === undefined ? {} : { completed_at: completedAt }),
    ...(status === "failed" ? { error: `Failure for ${taskId}.` } : {}),
  });
}

function response(entries: Entry[], responseId: string, status: "thinking" | "responding" | "completed" | "failed" | "cancelled",
  revision = status === "thinking" ? 1 : status === "responding" ? 2 : 3, historyId = ""): Entry[] {
  return appendEvent(entries, {
    event: "response_metadata", run_id: runId, response_id: responseId, response_session_id: "root",
    revision, round_number: 0, history_id: historyId, status,
    summary: "The parent is checking the work.", summary_truncated: false, summary_expired: false, usage_reported: false,
  });
}

function responseText(entries: Entry[], responseId: string, historyId: string, text: string): Entry[] {
  return appendEvent(entries, {
    event: "text_done", run_id: runId, response_id: responseId, history_id: historyId, text,
  });
}

function responseChunk(entries: Entry[], responseId: string, chunk: string): Entry[] {
  return appendEvent(entries, { event: "text_chunk", run_id: runId, response_id: responseId, chunk });
}

function toolSlot(entries: Entry[], historyId: string, id: string): Entry[] {
  return appendEvent(entries, {
    event: "tool_call", run_id: runId, history_id: historyId, call_position: 0,
    id, name: "read_file", arguments: { path: "README.md" },
  });
}

function rows(entries: Entry[]): Entry[] {
  return entries.filter(entry => entry.delegation !== undefined);
}

function starts(entries: Entry[]): Entry[] {
  return rows(entries).filter(entry => entry.delegation?.kind === "start");
}

function results(entries: Entry[]): Entry[] {
  return rows(entries).filter(entry => entry.delegation?.kind === "result");
}

function responseEntry(entries: Entry[], responseId: string): Entry {
  return entries.find(entry => entry.kind === "assistant" && entry.responseId === responseId)!;
}

function snapshot(events: WireEvent[]): Snapshot {
  return {
    session_id: "root", history: [], sessions: [], retained_tasks: [],
    active_run: { id: runId, status: "running", session_id: "root", events },
  };
}

test("a fast child finishing before parent text waits for its already-started thinking generation", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([], "child-a");
  entries = delegateResult(entries, "child-a");
  entries = taskStarted(entries, "child-a");
  entries = response(entries, "parent-response", "thinking");
  entries = taskCompleted(entries, "child-a", "completed", "parent-response", "root");

  const lifecycle = entries.find(entry => entry.kind === "task" && entry.taskId === "child-a")!;
  assert.deepEqual(lifecycle.completionGeneration, { responseId: "parent-response", sessionId: "root" });
  assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0,
    "metadata marks the parent generation active even before its first text token");

  entries = response(entries, "parent-response", "responding", 2);
  entries = responseText(entries, "parent-response", "parent-history", "I checked the result.");
  entries = toolSlot(entries, "parent-history", "parent-tool-slot");
  assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0,
    "text output and tool slots do not release the child while the generation is open");

  entries = response(entries, "parent-response", "completed", 3, "parent-history");
  const projected = flow.project(entries, options([agent("child-a")]));
  const result = results(projected)[0];
  const assistantIndex = projected.findIndex(entry => entry.id === responseEntry(projected, "parent-response").id);
  const slotIndex = projected.findIndex(entry => entry.callId === "parent-tool-slot");
  assert.equal(results(projected).length, 1);
  assert.ok(projected.indexOf(result) > assistantIndex);
  assert.ok(projected.indexOf(result) > slotIndex, "the result follows the generation's assistant text and tool-call slots");
});

test("early text_done is still held until terminal metadata, then the result follows the generation", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([], "child-a");
  entries = delegateResult(entries, "child-a");
  entries = taskStarted(entries, "child-a");
  entries = response(entries, "response-early-done", "thinking");
  entries = responseChunk(entries, "response-early-done", "The parent is still reasoning.");
  entries = response(entries, "response-early-done", "responding", 2);
  entries = taskCompleted(entries, "child-a", "completed", "response-early-done", "root");
  entries = responseText(entries, "response-early-done", "early-history", "The answer arrived first.");
  assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0);

  entries = response(entries, "response-early-done", "completed", 3, "early-history");
  const projected = flow.project(entries, options([agent("child-a")]));
  assert.equal(results(projected).length, 1);
  assert.ok(projected.findIndex(entry => entry.delegation?.kind === "result") >
    projected.findIndex(entry => entry.id === responseEntry(projected, "response-early-done").id));
});

test("without an active parent generation, child completion appears immediately", () => {
  let entries = delegateCall([], "child-a");
  entries = delegateResult(entries, "child-a");
  entries = taskStarted(entries, "child-a");
  entries = taskCompleted(entries, "child-a");
  const projected = new DelegationFlow().project(entries, options([agent("child-a", "done")]));
  assert.deepEqual(rows(projected).map(entry => entry.delegation?.kind), ["start", "result"]);
  assert.equal(results(projected)[0].delegation?.text, "Result for child-a.");
});

test("a later parent generation does not postpone a result bound to the earlier generation", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([], "child-a");
  entries = delegateResult(entries, "child-a");
  entries = taskStarted(entries, "child-a");
  entries = response(entries, "generation-one", "thinking");
  entries = taskCompleted(entries, "child-a", "completed", "generation-one", "root");
  assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0,
    "a later generation must not release a result from an earlier still-active generation");
  entries = responseText(entries, "generation-one", "history-one", "First generation answer.");
  entries = response(entries, "generation-one", "completed", 3, "history-one");
  entries = response(entries, "generation-two", "thinking", 1);
  entries = responseText(entries, "generation-two", "history-two", "Later generation answer.");

  const projected = flow.project(entries, options([agent("child-a")]));
  const result = results(projected)[0];
  const first = responseEntry(projected, "generation-one");
  const second = responseEntry(projected, "generation-two");
  assert.ok(projected.indexOf(result) > projected.indexOf(first));
  assert.ok(projected.indexOf(result) < projected.indexOf(second));
});

test("checkpoint replay preserves generation-bound result IDs and order through terminal reconciliation", () => {
  const flow = new DelegationFlow();
  let events: WireEvent[] = [
    { event: "tool_call", run_id: runId, id: "call-child-a", name: "to_agent", arguments: { agent: "mini", prompt: "Check child-a." } },
    { event: "tool_result", run_id: runId, id: "call-child-a", name: "to_agent", result: { session_id: "child-a", status: "running" } },
    { event: "task_started", run_id: runId, session_id: "child-a", parent_session_id: "", root_session_id: "root", activation: 0, title: "gentle willow", prompt: "Check child-a." },
    { event: "response_metadata", run_id: runId, response_id: "checkpoint-response", response_session_id: "root", revision: 1, round_number: 0, history_id: "", status: "thinking", summary: "Checking.", summary_truncated: false, summary_expired: false, usage_reported: false },
    { event: "task_completed", run_id: runId, session_id: "child-a", parent_session_id: "", root_session_id: "root", activation: 0,
      parent_response_id: "checkpoint-response", parent_response_session_id: "root", title: "gentle willow", status: "completed", result: "Result for child-a." },
    { event: "text_done", run_id: runId, response_id: "checkpoint-response", history_id: "checkpoint-history", text: "Checkpointed answer." },
  ];
  const partial = applySnapshot({ entries: [] }, snapshot(events));
  assert.equal(results(flow.project(partial.entries, options([agent("child-a")]))).length, 0);

  events = [...events, { event: "response_metadata", run_id: runId, response_id: "checkpoint-response", response_session_id: "root",
    revision: 3, round_number: 0, history_id: "checkpoint-history", status: "completed", summary: "Checking.", summary_truncated: false,
    summary_expired: false, usage_reported: false }];
  const terminal = applySnapshot(partial, snapshot(events));
  const projected = flow.project(terminal.entries, options([agent("child-a")]));
  const startId = starts(projected)[0].id;
  const resultId = results(projected)[0].id;
  const assistantId = responseEntry(projected, "checkpoint-response").id;
  assert.ok(projected.findIndex(entry => entry.id === resultId) > projected.findIndex(entry => entry.id === assistantId));

  const replayed = applySnapshot(terminal, snapshot(events));
  const replayedProjection = flow.project(replayed.entries, options([agent("child-a")]));
  assert.equal(starts(replayedProjection)[0].id, startId);
  assert.equal(results(replayedProjection)[0].id, resultId);
  assert.equal(replayedProjection.findIndex(entry => entry.id === resultId), projected.findIndex(entry => entry.id === resultId));

  const cold = applySnapshot({ entries: [] }, snapshot(events));
  const coldProjection = new DelegationFlow().project(cold.entries, options([agent("child-a")]));
  assert.deepEqual(rows(coldProjection).map(entry => entry.delegation?.kind), ["start", "result"]);
  assert.ok(coldProjection.findIndex(entry => entry.id === results(coldProjection)[0].id) >
    coldProjection.findIndex(entry => entry.id === responseEntry(coldProjection, "checkpoint-response").id));
});

test("parallel children finishing in reverse call order retain completion order at one generation boundary", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([], "child-a");
  entries = delegateResult(entries, "child-a");
  entries = taskStarted(entries, "child-a");
  entries = delegateCall(entries, "child-b");
  entries = delegateResult(entries, "child-b");
  entries = taskStarted(entries, "child-b");
  entries = response(entries, "parallel-parent", "thinking");
  entries = taskCompleted(entries, "child-b", "completed", "parallel-parent", "root");
  entries = taskCompleted(entries, "child-a", "completed", "parallel-parent", "root");
  entries = responseText(entries, "parallel-parent", "parallel-history", "Both checks are complete.");
  entries = response(entries, "parallel-parent", "completed", 3, "parallel-history");

  const projected = flow.project(entries, options([agent("child-a"), agent("child-b")]));
  assert.deepEqual(results(projected).map(entry => entry.delegation?.taskId), ["child-b", "child-a"]);
  assert.deepEqual(starts(projected).map(entry => entry.delegation?.taskId), ["child-a", "child-b"]);
});

test("failed and cancelled parent generations release held child results, and parent errors remain visible", () => {
  for (const status of ["failed", "cancelled"] as const) {
    const flow = new DelegationFlow();
    let entries = delegateCall([], "child-a");
    entries = delegateResult(entries, "child-a");
    entries = taskStarted(entries, "child-a");
    entries = response(entries, `parent-${status}`, "thinking");
    entries = taskCompleted(entries, "child-a", "completed", `parent-${status}`, "root");
    assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0);
    if (status === "failed") entries = appendEvent(entries, { event: "error", run_id: runId, message: "Parent response failed." });
    entries = response(entries, `parent-${status}`, status, 3);
    const projected = flow.project(entries, options([agent("child-a")]));
    assert.equal(results(projected).length, 1, `${status} must release the pending child result`);
    if (status === "failed") assert.ok(projected.some(entry => entry.kind === "error" && entry.text === "Parent response failed."));
  }
});


test("a terminal parent checkpoint before task_completed still anchors at the referenced generation", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([], "child-a");
  entries = delegateResult(entries, "child-a");
  entries = taskStarted(entries, "child-a");
  entries = response(entries, "checkpointed-parent", "thinking");
  entries = responseText(entries, "checkpointed-parent", "checkpoint-history", "The parent checked the child.");
  entries = response(entries, "checkpointed-parent", "completed", 3, "checkpoint-history");
  entries = response(entries, "later-parent", "thinking");
  entries = taskCompleted(entries, "child-a", "completed", "checkpointed-parent", "root");
  entries = responseText(entries, "later-parent", "later-history", "A later generation is active.");

  const lifecycle = entries.find(entry => entry.kind === "task" && entry.taskId === "child-a")!;
  assert.deepEqual(lifecycle.completionGeneration, { responseId: "checkpointed-parent", sessionId: "root" });
  const projected = flow.project(entries, options([agent("child-a")]));
  const result = results(projected)[0];
  const checkpoint = responseEntry(projected, "checkpointed-parent");
  const later = responseEntry(projected, "later-parent");
  assert.equal(results(projected).length, 1);
  assert.ok(projected.indexOf(result) > projected.indexOf(checkpoint));
  assert.ok(projected.indexOf(result) < projected.indexOf(later), "a later open generation cannot steal the result boundary");
});

test("an unknown same-session response reference waits for its marker, while a foreign session cannot hold the result", () => {
  {
    const flow = new DelegationFlow();
    let entries = delegateCall([], "child-a");
    entries = delegateResult(entries, "child-a");
    entries = taskStarted(entries, "child-a");
    entries = taskCompleted(entries, "child-a", "completed", "queued-parent-response", "root");
    const lifecycle = entries.find(entry => entry.kind === "task" && entry.taskId === "child-a")!;
    assert.deepEqual(lifecycle.completionGeneration, { responseId: "queued-parent-response", sessionId: "root" });
    assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0,
      "same-session host identity can precede its response-metadata frame");

    entries = response(entries, "queued-parent-response", "thinking");
    assert.equal(results(flow.project(entries, options([agent("child-a")]))).length, 0);
    entries = responseText(entries, "queued-parent-response", "queued-parent-history", "The parent completed its check.");
    entries = response(entries, "queued-parent-response", "completed", 3, "queued-parent-history");
    const projected = flow.project(entries, options([agent("child-a")]));
    assert.equal(results(projected).length, 1);
    assert.ok(projected.findIndex(entry => entry.delegation?.kind === "result") >
      projected.findIndex(entry => entry.id === responseEntry(projected, "queued-parent-response").id));
  }

  {
    const flow = new DelegationFlow();
    let entries = delegateCall([], "child-a");
    entries = delegateResult(entries, "child-a");
    entries = taskStarted(entries, "child-a");
    entries = response(entries, "local-response", "thinking");
    entries = taskCompleted(entries, "child-a", "completed", "local-response", "foreign-session");
    const lifecycle = entries.find(entry => entry.kind === "task" && entry.taskId === "child-a")!;
    assert.equal(lifecycle.completionGeneration, undefined);
    const projected = flow.project(entries, options([agent("child-a")]));
    assert.equal(results(projected).length, 1, "foreign session metadata cannot suppress a child result");
    assert.equal(projected.some(entry => entry.kind === "assistant" && entry.responseId === "local-response"), true,
      "the unrelated local generation remains ordinary assistant evidence");
  }
});

test("cold retained activations preserve the authoritative parent response reference", () => {
  const activation: RetainedTask = {
    session_id: "child-a", title: "gentle willow", status: "completed", result: "Result for child-a.", error: "",
    root_session_id: "root", prompt: "Check child-a.", parent_session_id: "", depth: 1, profile: "mini", mode: "build",
    followups: 0, activation: 0, trigger: "delegation", parent_response_id: "retained-parent", parent_response_session_id: "root",
  };
  const parentMetadata = {
    response_id: "retained-parent", response_session_id: "root", run_id: runId, revision: 3, round_number: 0,
    history_id: "retained-history", status: "completed", summary: "Parent response.", summary_truncated: false,
    summary_expired: false, usage_reported: false,
  };
  const saved: Snapshot = {
    session_id: "root", sessions: [], retained_tasks: [], retained_activations: [activation],
    history: [{ role: "assistant", content: "Parent response.", name: "", tool_call_id: "", tool_calls: [],
      history_id: "retained-history", response_id: "retained-parent", response_metadata: parentMetadata }],
  };
  const lifecycle = fromHistory(saved).find(entry => entry.kind === "task" && entry.taskId === "child-a");
  assert.ok(lifecycle?.recorded);
  assert.deepEqual(lifecycle.completionGeneration, { responseId: "retained-parent", sessionId: "root" });
});


test("cold retained replay anchors results after a known terminal response", () => {
  const activation: RetainedTask = {
    session_id: "child-a", title: "gentle willow", status: "completed", result: "Result for child-a.", error: "",
    root_session_id: "root", prompt: "Check child-a.", parent_session_id: "", depth: 1, profile: "mini", mode: "build",
    followups: 0, activation: 0, trigger: "delegation", parent_response_id: "retained-parent", parent_response_session_id: "root",
  };
  const parentMetadata = {
    response_id: "retained-parent", response_session_id: "root", run_id: runId, revision: 3, round_number: 0,
    history_id: "retained-history", status: "completed", summary: "Parent response.", summary_truncated: false,
    summary_expired: false, usage_reported: false,
  };
  const saved: Snapshot = {
    session_id: "root", sessions: [], retained_tasks: [], retained_activations: [activation],
    history: [
      { role: "assistant", content: "", name: "", tool_call_id: "", history_id: "delegate-call-history",
        tool_calls: [{ id: "call-child-a", name: "to_agent", arguments: { agent: "mini", prompt: "Check child-a." } }] },
      { role: "tool", content: JSON.stringify({ session_id: "child-a", status: "running" }), name: "to_agent",
        tool_call_id: "call-child-a", tool_calls: [], history_id: "delegate-ack-history" },
      { role: "assistant", content: "Parent response.", name: "", tool_call_id: "", tool_calls: [],
        history_id: "retained-history", response_id: "retained-parent", response_metadata: parentMetadata },
    ],
  };
  const entries = fromHistory(saved);
  const lifecycle = entries.find(entry => entry.kind === "task" && entry.taskId === "child-a");
  assert.deepEqual(lifecycle?.completionGeneration, { responseId: "retained-parent", sessionId: "root" });
  const projected = new DelegationFlow().project(entries, options([agent("child-a", "done")]));
  const result = results(projected)[0];
  const parent = responseEntry(projected, "retained-parent");
  assert.deepEqual(rows(projected).map(entry => entry.delegation?.kind), ["start", "result"]);
  assert.ok(projected.indexOf(result) > projected.indexOf(parent));
});


test("live and cold parallel results use host completion time rather than spawn order", () => {
  const parentResponseId = "parallel-completion-parent";
  const liveFlow = new DelegationFlow();
  let live = delegateCall([], "child-a");
  live = delegateResult(live, "child-a");
  live = taskStarted(live, "child-a");
  live = delegateCall(live, "child-b");
  live = delegateResult(live, "child-b");
  live = taskStarted(live, "child-b");
  live = response(live, parentResponseId, "thinking");
  live = taskCompleted(live, "child-b", "completed", parentResponseId, "root", 200);
  live = taskCompleted(live, "child-a", "completed", parentResponseId, "root", 300);
  live = responseText(live, parentResponseId, "parallel-completion-history", "Both children returned.");
  live = response(live, parentResponseId, "completed", 3, "parallel-completion-history");
  const liveProjected = liveFlow.project(live, options([agent("child-a", "done"), agent("child-b", "done")]));
  const liveOrder = results(liveProjected).map(entry => entry.delegation?.taskId);
  assert.deepEqual(starts(liveProjected).map(entry => entry.delegation?.taskId), ["child-a", "child-b"]);
  assert.deepEqual(liveOrder, ["child-b", "child-a"]);

  const retained = (sessionId: string, title: string, completedAt: number): RetainedTask => ({
    session_id: sessionId, title, status: "completed", result: `Result for ${sessionId}.`, error: "",
    root_session_id: "root", prompt: `Check ${sessionId}.`, parent_session_id: "", depth: 1, profile: "mini", mode: "build",
    followups: 0, activation: 0, trigger: "delegation", completed_at: completedAt,
    parent_response_id: parentResponseId, parent_response_session_id: "root",
  });
  const metadata = {
    response_id: parentResponseId, response_session_id: "root", run_id: runId, revision: 3, round_number: 0,
    history_id: "parallel-completion-history", status: "completed", summary: "Both children returned.",
    summary_truncated: false, summary_expired: false, usage_reported: false,
  };
  const cold: Snapshot = {
    session_id: "root", sessions: [], retained_tasks: [],
    retained_activations: [retained("child-a", "gentle willow", 300), retained("child-b", "bright reed", 200)],
    history: [
      { role: "assistant", content: "", name: "", tool_call_id: "", history_id: "call-a-history",
        tool_calls: [{ id: "call-child-a", name: "to_agent", arguments: { agent: "mini", prompt: "Check child-a." } }] },
      { role: "tool", content: JSON.stringify({ session_id: "child-a", status: "running" }), name: "to_agent",
        tool_call_id: "call-child-a", tool_calls: [], history_id: "ack-a-history" },
      { role: "assistant", content: "", name: "", tool_call_id: "", history_id: "call-b-history",
        tool_calls: [{ id: "call-child-b", name: "to_agent", arguments: { agent: "mini", prompt: "Check child-b." } }] },
      { role: "tool", content: JSON.stringify({ session_id: "child-b", status: "running" }), name: "to_agent",
        tool_call_id: "call-child-b", tool_calls: [], history_id: "ack-b-history" },
      { role: "assistant", content: "Both children returned.", name: "", tool_call_id: "", tool_calls: [],
        history_id: "parallel-completion-history", response_id: parentResponseId, response_metadata: metadata },
    ],
  };
  const coldEntries = fromHistory(cold);
  assert.deepEqual(coldEntries.filter(entry => entry.kind === "task").map(entry => entry.taskId), ["child-a", "child-b"],
    "retained activations arrive in spawn order");
  const coldProjected = new DelegationFlow().project(coldEntries, options([agent("child-a", "done"), agent("child-b", "done")]));
  const coldOrder = results(coldProjected).map(entry => entry.delegation?.taskId);
  assert.deepEqual(coldOrder, liveOrder);
  assert.deepEqual(coldOrder, ["child-b", "child-a"]);
  for (const projected of [liveProjected, coldProjected]) {
    const parent = responseEntry(projected, parentResponseId);
    assert.ok(projected.findIndex(entry => entry.delegation?.kind === "result") > projected.indexOf(parent));
  }
});
