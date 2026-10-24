import assert from "node:assert/strict";
import test from "node:test";
import type { RetainedTask, Snapshot } from "../../types.js";
import { taskAgent, type TaskDetail } from "../team/model.js";
import { TaskTranscript, taskEntries } from "../team/TaskConversation.js";
import { AGENT_UPDATES_PREFIX } from "./backgroundUpdates.js";
import { DelegationFlow } from "./delegationFlow.js";
import { applySnapshot } from "./liveTranscript.js";
import { fromHistory, type Entry } from "./transcript.js";

function savedActivation(session_id: string, activation: number, history: string, position: number, parent_session_id = ""): RetainedTask {
  return {
    session_id, activation, title: activation === 0 ? "Same title" : session_id === "s1" && activation === 2 ? "Model override" : "Updated title",
    status: "completed", result: `Verified ${session_id} activation ${activation}`, error: "", root_session_id: "ngn-root", parent_session_id,
    prompt: `Prompt ${session_id} activation ${activation}`, profile: `profile-${activation}`, provider: `provider-${activation}`, model: `model-${activation}`,
    mode: "build", followups: 0, depth: parent_session_id ? 2 : 1, trigger: "delegation",
    activation_history_id: history, activation_run_id: `run-${activation}`, activation_call_id: session_id === "s1" ? "reused-call" : "peer-call", activation_call_position: position,
  };
}

function coldSnapshot(rounds: number, parent = ""): Snapshot {
  const history: Snapshot["history"] = [], activations: RetainedTask[] = [];
  const row = (history_id: string, role: string, content: string, extra: Partial<Snapshot["history"][number]> = {}) =>
    history.push({ history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra });
  for (let activation = 0; activation < rounds; activation++) {
    const historyId = `calls-${activation}`;
    const round = (activation === 2 ? ["s1"] : ["s1", "s2"]).map((id, position) => savedActivation(id, activation, historyId, position, parent));
    activations.push(...round);
    row(`input-${activation}`, "user", `Request ${activation}`);
    row(historyId, "assistant", "", { tool_calls: round.map(task => ({
      id: task.activation_call_id!, name: "to_agent", arguments: { prompt: task.prompt,
        ...(activation ? { session_id: task.session_id } : { agent: task.profile, title: "Same title" }) },
    })) });
    for (const task of round) row(`receipt-${task.session_id}-${activation}`, "tool", JSON.stringify({ session_id: task.session_id, status: "running" }),
      { tool_call_id: task.activation_call_id!, name: "to_agent" });
    row(`waiting-${activation}`, "assistant", `Waiting ${activation}`);
    row(`context-${activation}`, "user", AGENT_UPDATES_PREFIX + JSON.stringify({
      sessions: round.map(task => ({ session_id: task.session_id, status: task.status, result: task.result })), human_messages: [],
    }));
    row(`answer-${activation}`, "assistant", `Parent answer ${activation}`);
  }
  const current = new Map(activations.map(task => [task.session_id, { ...task, title: "LATEST REGISTRY TITLE", profile: "latest-profile", model: "latest-model", result: "LATEST REGISTRY RESULT" }]));
  return { session_id: "ngn-root", sessions: [], history, retained_tasks: [...current.values()], retained_activations: activations };
}

function project(entries: Entry[], snapshot: Snapshot, parentId = "") {
  // The live registry can have newer labels/settings. Historical Relay rows
  // must use the corresponding frozen activation instead.
  const agents = snapshot.retained_tasks.map(task => taskAgent({ ...task, title: "LATEST REGISTRY TITLE", profile: "latest-profile", model: "latest-model" }));
  return new DelegationFlow().project(entries, { agents, activity: [], parentId, inspect() {} });
}

for (const rounds of [1, 2, 3]) test(`cold reload restores every verified activation after ${rounds} rounds`, () => {
  const snapshot = coldSnapshot(rounds);
  // This is the production cold snapshot path, including local ID allocation.
  const cold = applySnapshot({ entries: [] }, snapshot).entries;
  const projected = project(cold, snapshot);
  const starts = projected.filter(entry => entry.delegation?.kind === "start");
  const results = projected.filter(entry => entry.delegation?.kind === "result");
  assert.equal(starts.length, snapshot.retained_activations!.length);
  assert.equal(results.length, starts.length);
  assert.equal(projected.some(entry => entry.kind === "context" || entry.kind === "notification"), false, "completion context does not produce duplicate Notices");
  for (const task of snapshot.retained_activations!) {
    const start = starts.find(entry => entry.delegation?.taskId === task.session_id && entry.delegation.activation === task.activation)!;
    const result = results.find(entry => entry.delegation?.taskId === task.session_id && entry.delegation.activation === task.activation)!;
    assert.ok(start && result);
    for (const entry of [start, result]) {
      assert.equal(entry.delegation?.phase, "done");
      assert.equal(entry.delegation?.name, task.title);
      assert.equal(entry.delegation?.profile, task.profile);
      assert.equal(entry.delegation?.provider, task.provider);
      assert.equal(entry.delegation?.model, task.model);
      assert.equal(entry.delegation?.text, task.result);
    }
    const waiting = projected.findIndex(entry => entry.text === `Waiting ${task.activation}`);
    const answer = projected.findIndex(entry => entry.text === `Parent answer ${task.activation}`);
    const resultPosition = projected.indexOf(result);
    assert.ok(projected.indexOf(start) < waiting);
    assert.ok(waiting < resultPosition && resultPosition < answer, "result stays between its wait and corresponding parent answer");
  }
  assert.equal(new Set(cold.map(entry => entry.id)).size, cold.length, "activation lifecycle IDs are independent");
  const refreshed = applySnapshot({ entries: cold, history: snapshot.history }, snapshot).entries;
  const ids = (entries: Entry[]) => entries.filter(entry => entry.kind === "task").map(entry => entry.id);
  assert.deepEqual(ids(refreshed), ids(cold), "a repeated host manifest retains each activation's local identity");
});

test("current task metadata and saved context cannot invent missing older activation records", () => {
  const snapshot = coldSnapshot(3);
  const withoutManifest = { ...snapshot, retained_activations: undefined };
  const projected = project(fromHistory(withoutManifest), withoutManifest);
  const results = projected.filter(entry => entry.delegation?.kind === "result");
  assert.deepEqual(results.map(entry => [entry.delegation?.taskId, entry.delegation?.activation]), [["s2", 1], ["s1", 2]]);
  assert.ok(projected.filter(entry => entry.delegation?.kind === "start" && entry.delegation.activation === undefined).every(entry => !entry.delegation?.text));
  assert.ok(projected.some(entry => entry.kind === "context"), "uncorrelated saved context remains inspectable");
});

test("selected child history restores its own direct-child activations using the actual root scope", () => {
  const snapshot = coldSnapshot(3, "s0");
  const parent = { ...savedActivation("s0", 0, "parent-call", 0), title: "Parent" };
  const detail: TaskDetail = { task: parent, history: snapshot.history, retained_activations: snapshot.retained_activations };
  const initial = taskEntries(detail, []);
  const transcript = new TaskTranscript();
  const first = transcript.update(detail, []);
  const second = transcript.update({ ...detail }, []);
  for (const entries of [initial, first, second]) {
    const projected = project(entries, snapshot, "s0");
    assert.equal(projected.filter(entry => entry.delegation?.kind === "start").length, 5);
    assert.equal(projected.filter(entry => entry.delegation?.kind === "result").length, 5);
    assert.ok(entries.filter(entry => entry.kind === "task").every(entry => entry.parentTaskId === "s0"));
    assert.equal(projected.some(entry => entry.kind === "context"), false);
  }
  assert.deepEqual(second.filter(entry => entry.kind === "task").map(entry => entry.id), first.filter(entry => entry.kind === "task").map(entry => entry.id));
});


test("a frozen empty completion cannot borrow a newer registry result", () => {
  const snapshot = coldSnapshot(1);
  snapshot.retained_activations![0]!.result = "";
  delete snapshot.retained_activations![0]!.provider;
  delete snapshot.retained_activations![0]!.model;
  const context = snapshot.history.find(row => row.history_id === "context-0")!;
  context.content = AGENT_UPDATES_PREFIX + JSON.stringify({ sessions: snapshot.retained_activations!.map(task => ({
    session_id: task.session_id, status: "completed", result: task.result,
  })), human_messages: [] });
  const projected = project(fromHistory(snapshot), snapshot);
  assert.equal(projected.find(entry => entry.delegation?.kind === "result" && entry.delegation.taskId === "s1")?.delegation?.text, "");
  assert.equal(projected.some(entry => entry.kind === "context"), false);
  const first = projected.find(entry => entry.delegation?.kind === "start" && entry.delegation.taskId === "s1")!.delegation!;
  assert.equal(first.provider, "");
  assert.equal(first.model, "");
  assert.doesNotMatch(JSON.stringify(projected.filter(entry => entry.delegation)), /LATEST REGISTRY RESULT/);
});
