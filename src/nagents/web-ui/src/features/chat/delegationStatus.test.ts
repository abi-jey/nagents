import assert from "node:assert/strict";
import test from "node:test";
import type { TaskActivity, TeamAgent } from "../team/model.js";
import { DelegationFlow } from "./delegationFlow.js";
import { appendEvent, type Entry } from "./transcript.js";

const runId = "root-run";

function agent(id: string, parentId = "", phase: TeamAgent["phase"] = "running"): TeamAgent {
  return {
    id, name: "gentle willow", profile: "mini", parentId, prompt: "Check the session path.", phase,
    reason: "", provider: "", providerKind: "", model: "", startedAt: 1, updatedAt: 2,
    canMessage: true, messageReason: "",
  };
}

function options(agents: TeamAgent[], parentId = "") {
  return { agents, activity: [] as TaskActivity[], parentId, inspect: (_id: string) => undefined };
}

function delegateCall(entries: Entry[], id = "delegate-call", historyId = "", callPosition = 0): Entry[] {
  return appendEvent(entries, {
    event: "tool_call", run_id: runId, id, name: "to_agent",
    ...(historyId ? { history_id: historyId, call_position: callPosition } : {}),
    arguments: { agent: "mini", prompt: "Check the session path." },
  });
}

function delegateResult(entries: Entry[], id = "delegate-call"): Entry[] {
  return appendEvent(entries, {
    event: "tool_result", run_id: runId, id, name: "to_agent", result: { session_id: "child", status: "running" },
  });
}

function taskStarted(entries: Entry[], activation = 0, provenance = false): Entry[] {
  return appendEvent(entries, {
    event: "task_started", run_id: runId, session_id: "child", parent_session_id: "", activation,
    title: activation ? "Updated willow" : "gentle willow", prompt: "Check the session path.",
    ...(provenance ? { activation_history_id: "resume-history", activation_run_id: runId,
      activation_call_id: "resume-call", activation_call_position: 1 } : {}),
  });
}

function taskCompleted(entries: Entry[], activation = 0): Entry[] {
  return appendEvent(entries, {
    event: "task_completed", run_id: runId, session_id: "child", parent_session_id: "", activation,
    title: activation ? "Updated willow" : "gentle willow", status: "completed", result: `Verified activation ${activation}.`,
  });
}

function statusPoll(entries: Entry[], id: string, sessionId = "child", status = "running"): Entry[] {
  entries = appendEvent(entries, {
    event: "tool_call", run_id: runId, id, name: "agent_status", arguments: { session_id: sessionId },
  });
  return appendEvent(entries, {
    event: "tool_result", run_id: runId, id, name: "agent_status",
    result: {
      session_id: sessionId, title: "gentle willow", status, agent: "mini", parent_session_id: "",
      provider: "", model: "", phase: status, context: {}, compaction: {}, usage: {}, last_text: "",
      last_tool: { name: "", status: "" }, last_output: "", result: `Polled result ${id}`, error: "", history: [], truncated: false,
    },
  });
}

function statusSources(row: Entry | undefined): Entry[] {
  return row?.delegation?.sources.filter(source => source.title === "agent_status") || [];
}

function delegationRows(entries: Entry[]): Entry[] {
  return entries.filter(entry => entry.delegation !== undefined);
}

test("repeated single-session agent_status polls fold into the verified child delegation without extra rows", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = statusPoll(entries, "poll-one");
  entries = statusPoll(entries, "poll-two");
  entries = appendEvent(entries, { event: "text_done", run_id: runId, response_id: "main-ack", text: "The check is complete." });
  const mainAcknowledgement = entries.at(-1)!;
  entries = taskCompleted(entries);

  const projected = flow.project(entries, options([agent("child", "", "done")]));
  const rows = delegationRows(projected);
  assert.deepEqual(rows.map(row => row.delegation?.kind), ["start", "result"]);
  assert.equal(rows[0].id, entries[0].id);
  assert.equal(rows[1].id, "delegation-result:child:0");
  assert.ok(projected.findIndex(entry => entry.id === rows[1].id) > projected.findIndex(entry => entry.id === mainAcknowledgement.id));
  assert.equal(projected.some(entry => entry.kind === "tool" && entry.title === "agent_status"), false);
  assert.deepEqual(statusSources(rows[0]).map(source => source.callId), ["poll-one", "poll-two"]);
  assert.deepEqual(statusSources(rows[1]).map(source => source.callId), ["poll-one", "poll-two"]);
  assert.equal(rows[1].delegation?.text, "Verified activation 0.", "poll result text cannot replace lifecycle result text");
});

test("a terminal status response cannot invent lifecycle completion or an activation", () => {
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = statusPoll(entries, "terminal-poll", "child", "completed");

  const projected = new DelegationFlow().project(entries, options([agent("child", "", "done")]));
  const rows = delegationRows(projected);
  assert.deepEqual(rows.map(row => row.delegation?.kind), ["start"]);
  assert.equal(rows[0].delegation?.activation, 0);
  assert.equal(rows[0].delegation?.phase, "running");
  assert.equal(rows[0].delegation?.text, "");
  assert.equal(projected.some(entry => entry.kind === "tool" && entry.title === "agent_status"), true,
    "status without a verified lifecycle match remains visible");
});

test("resumed-session polls attach only to their preceding verified activation", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = statusPoll(entries, "activation-zero-poll");
  entries = taskCompleted(entries);
  entries = delegateCall(entries, "resume-call", "resume-history", 1);
  entries = delegateResult(entries, "resume-call");
  entries = taskStarted(entries, 1, true);
  entries = statusPoll(entries, "activation-one-poll");
  entries = taskCompleted(entries, 1);

  const projected = flow.project(entries, options([agent("child", "", "done")]));
  const starts = delegationRows(projected).filter(row => row.delegation?.kind === "start");
  const results = delegationRows(projected).filter(row => row.delegation?.kind === "result");
  assert.deepEqual(starts.map(row => row.delegation?.activation), [0, 1]);
  assert.deepEqual(results.map(row => row.delegation?.text), ["Verified activation 0.", "Verified activation 1."]);
  assert.deepEqual(statusSources(starts[0]).map(source => source.callId), ["activation-zero-poll"]);
  assert.deepEqual(statusSources(starts[1]).map(source => source.callId), ["activation-one-poll"]);
  assert.deepEqual(statusSources(results[0]).map(source => source.callId), ["activation-zero-poll"]);
  assert.deepEqual(statusSources(results[1]).map(source => source.callId), ["activation-one-poll"]);
  assert.equal(projected.some(entry => entry.kind === "tool" && entry.title === "agent_status"), false);
});

test("failed calls and unknown, unmatched, or multi-session status responses stay visible", () => {
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = statusPoll(entries, "failed-poll");
  entries = appendEvent(entries, { event: "tool_result", run_id: runId, id: "failed-poll", name: "agent_status", error: "Unavailable" });
  entries = statusPoll(entries, "unknown-poll", "unknown");
  entries = statusPoll(entries, "mixed-poll", "child");
  entries = appendEvent(entries, { event: "tool_result", run_id: runId, id: "mixed-poll", name: "agent_status", result: {
    sessions: [
      { session_id: "child", title: "gentle willow", status: "running", result: "Polled child." },
      { session_id: "other", title: "other", status: "running", result: "Polled other." },
    ], more: 0,
  } });

  const projected = new DelegationFlow().project(entries, options([agent("child")]));
  assert.deepEqual(projected.filter(entry => entry.kind === "tool" && entry.title === "agent_status").map(entry => entry.callId), [
    "failed-poll", "unknown-poll", "mixed-poll",
  ]);
  assert.deepEqual(delegationRows(projected).map(row => row.delegation?.kind), ["start"]);
});

test("a pending streamed single-session status request does not add a separate tool row", () => {
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = appendEvent(entries, {
    event: "tool_call_progress", run_id: runId, generation_id: "status-generation", index: 0,
    id: "pending-status", name: "agent_status", status: "streaming", arguments_text: '{"session_id":"child"',
  });

  const projected = new DelegationFlow().project(entries, options([agent("child")]));
  assert.deepEqual(delegationRows(projected).map(row => row.delegation?.kind), ["start"]);
  assert.equal(projected.some(entry => entry.kind === "tool" && entry.title === "agent_status"), false);
});

test("a child conversation folds status scoped to its normalized empty own-session ID", () => {
  let entries: Entry[] = [];
  entries = appendEvent(entries, {
    event: "tool_call", run_id: runId, session_id: "", id: "nested-delegate", name: "to_agent",
    arguments: { agent: "mini", prompt: "Check the nested session." },
  });
  entries = appendEvent(entries, {
    event: "tool_result", run_id: runId, session_id: "", id: "nested-delegate", name: "to_agent",
    result: { session_id: "grandchild", status: "running" },
  });
  entries = appendEvent(entries, {
    event: "task_started", run_id: runId, session_id: "grandchild", parent_session_id: "child", activation: 0,
    title: "gentle willow", prompt: "Check the nested session.",
  });
  entries = appendEvent(entries, {
    event: "tool_call", run_id: runId, session_id: "", id: "child-status", name: "agent_status",
    arguments: { session_id: "grandchild" },
  });
  entries = appendEvent(entries, {
    event: "tool_result", run_id: runId, session_id: "", id: "child-status", name: "agent_status",
    result: { session_id: "grandchild", title: "gentle willow", status: "running", result: "Polled result." },
  });

  const source = entries.find(entry => entry.callId === "child-status")!;
  assert.equal(source.taskId, "", "the child conversation normalizes its own task scope to empty");
  const projected = new DelegationFlow().project(entries, options([
    agent("child"), agent("grandchild", "child"),
  ], "child"));
  const rows = delegationRows(projected);
  assert.deepEqual(rows.map(row => row.delegation?.kind), ["start"]);
  assert.equal(rows[0].delegation?.taskId, "grandchild");
  assert.deepEqual(statusSources(rows[0]).map(entry => entry.callId), ["child-status"]);
  assert.equal(projected.some(entry => entry.kind === "tool" && entry.title === "agent_status"), false);
});

test("pending and denied agent_status approvals remain visible beside the delegation", () => {
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = appendEvent(entries, {
    event: "approval", run_id: runId, id: "pending-status", tool: "agent_status", approval_id: "pending-status-approval",
    arguments: { session_id: "child" },
  });
  entries = appendEvent(entries, {
    event: "approval", run_id: runId, id: "denied-status", tool: "agent_status", approval_id: "denied-status-approval",
    arguments: { session_id: "child" },
  });
  entries = appendEvent(entries, { event: "approval_closed", run_id: runId, approval_id: "denied-status-approval", decision: "deny" });

  const projected = new DelegationFlow().project(entries, options([agent("child")]));
  const polls = projected.filter(entry => entry.kind === "tool" && entry.title === "agent_status");
  assert.deepEqual(polls.map(entry => [entry.callId, entry.state, entry.approval]), [
    ["pending-status", "Waiting for approval", undefined],
    ["denied-status", "Approval denied", "Denied"],
  ]);
  assert.deepEqual(delegationRows(projected).map(row => row.delegation?.kind), ["start"]);
});
