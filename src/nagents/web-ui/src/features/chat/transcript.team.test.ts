import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Composer } from "./Composer.js";
import { appendEvent } from "./transcript.js";
import { TaskTimeline } from "../team/TaskTimeline.js";
import { TeamTree } from "../team/TeamTree.js";
import { taskEntries } from "../team/TaskConversation.js";
import { TaskDrafts, agentAncestors, mergeTaskActivity, orderedTeam, providerLabel, taskAgent, type TaskActivity, type TeamAgent, type TaskDetail } from "../team/model.js";
import type { RetainedTask } from "../../types.js";

const task = (id: string, parent = "", updated = 1): RetainedTask => ({ id, name: id, parent_task_id: parent,
  status: "running", result: "", error: "", prompt: `${id} task`, session_id: "root", child_session_id: `child-${id}`,
  parent_session_id: "root", depth: parent ? 2 : 1, profile: "assistant", mode: "build", followups: 0, activation: 0,
  trigger: "delegation", provider: "my-openai", provider_kind: "openai", model: "test-model", updated_at: updated,
  started_at: 1, phase: "running", can_message: true });
const root: TeamAgent = { ...taskAgent(task("")), name: "Assistant", phase: "ready" };
const event = (id: string, sequence: number, payload: TaskActivity["payload"], activation = 0): TaskActivity => ({
  event: "task_activity", task_id: id, activation, sequence, timestamp: sequence, payload,
});

test("team orders siblings by their deepest latest activity without flattening ancestry", () => {
  const agents = [root, taskAgent(task("old", "", 1)), taskAgent(task("new", "", 5)), taskAgent(task("nested", "old", 9))];
  const tree = orderedTeam(agents);
  assert.deepEqual(tree.children.get("")?.map(item => item.id), ["old", "new"]);
  assert.deepEqual(tree.children.get("old")?.map(item => item.id), ["nested"]);
  assert.deepEqual(agentAncestors("nested", agents), ["old", ""]);
});

test("missing and cyclic task parents remain visible without recursive loops", () => {
  const agents = [root, taskAgent(task("a", "b")), taskAgent(task("b", "a")), taskAgent(task("missing", "gone"))];
  assert.deepEqual(orderedTeam(agents).children.get("")?.map(item => item.id), ["a", "b", "missing"]);
});

test("accepted provider and compaction threshold are used, empty context stays unavailable", () => {
  const agent = taskAgent({ ...task("child"), context: {} as RetainedTask["context"],
    compaction: { trigger: "tokens", token_threshold: 42000 } });
  assert.equal(agent.provider, "my-openai"); assert.equal(agent.compactThreshold, 42000); assert.equal(agent.context, undefined);
});

test("unnamed provider connections display their kind without fabricating a settings identifier", () => {
  const agent = taskAgent({ ...task("child"), provider: "", provider_kind: "openai_compatible" });
  assert.equal(providerLabel(agent), "openai_compatible");
  assert.equal(agent.provider, "", "Saving unchanged settings keeps the default connection identifier empty");
});

test("waiting phases explain what is awaited while terminal failure is preserved", () => {
  assert.match(taskAgent({ ...task("a"), phase: "waiting_approval" }).reason, /approval/);
  assert.match(taskAgent({ ...task("b"), phase: "waiting" }).reason, /delegated work/);
  assert.equal(taskAgent({ ...task("c"), status: "failed", error: "Tool failed" }).phase, "failed");
});

test("child drafts remain separate by root and recipient with stable unconfirmed retry IDs", () => {
  const drafts = new TaskDrafts();
  drafts.set("root", "a", "First"); drafts.set("root", "b", "Second"); drafts.set("other", "a", "Other root");
  const first = drafts.prepare("root", "a", "First", () => "id-one");
  assert.equal(drafts.prepare("root", "a", "First", () => "new-id").message_id, "id-one");
  drafts.confirmed("root", "a", first);
  assert.equal(drafts.get("root", "a"), ""); assert.equal(drafts.get("root", "b"), "Second"); assert.equal(drafts.get("other", "a"), "Other root");
});

test("late task acknowledgement cannot erase a draft edited back to the same text", () => {
  const drafts = new TaskDrafts(); drafts.set("r", "a", "Same");
  const sent = drafts.prepare("r", "a", "Same", () => "one");
  drafts.set("r", "a", "Changed"); drafts.set("r", "a", "Same"); drafts.confirmed("r", "a", sent);
  assert.equal(drafts.get("r", "a"), "Same");
});

test("task activity replay uses host identity across siblings and activations", () => {
  const one = event("one", 1, { type: "text_chunk", chunk: "A" });
  const result = mergeTaskActivity([one], [one, event("two", 1, { type: "text_chunk", chunk: "B" }), event("one", 1, { type: "text_chunk", chunk: "C" }, 1)]);
  assert.equal(result.length, 3);
  assert.deepEqual(appendEvent([], one), [], "root transcript must never expand a child envelope");
});

test("task transcript pairs committed history with later activity, drops sibling and private payloads", () => {
  const detail: TaskDetail = { task: task("one"), history: [{ role: "user", content: "Original", name: "", tool_call_id: "", tool_calls: [] }], activity_cursor: 3,
    activity: [event("one", 3, { type: "text_chunk", chunk: "Part" })] };
  const entries = taskEntries(detail, [event("one", 2, { type: "text_chunk", chunk: "Stale" }), event("two", 4, { type: "text_chunk", chunk: "Sibling" }),
    event("one", 4, { type: "text_chunk", chunk: " two", task_id: "forged" }), event("one", 5, { type: "reasoning", text: "Private" })]);
  assert.deepEqual(entries.map(item => item.text), ["Original", "Part two"]);
  assert.ok(entries.every(item => !item.taskId));
});

test("a stream resync gap keeps recorded history and drops partial evidence until a fresh boundary", () => {
  const detail: TaskDetail = { task: task("one"), history: [{ role: "assistant", content: "Saved result", name: "", tool_call_id: "", tool_calls: [] }], activity_cursor: 3,
    activity: [event("one", 3, { type: "text_chunk", chunk: "Partial" })] };
  assert.deepEqual(taskEntries(detail, [event("one", 4, { type: "resync_required" })]).map(item => item.text), ["Saved result"]);
  assert.deepEqual(taskEntries({ ...detail, activity_truncated: true }, []).map(item => item.text), ["Saved result"]);
});

test("child shared composer has one input and no root slash commands or attachment controls", () => {
  const markup = renderToStaticMarkup(createElement(Composer, { inputRef: null, prompt: "/login", setPrompt() {}, demo: false,
    disabled: false, canSubmit: true, running: false, submit() {}, cancel() {}, recipient: "Explorer", commandsEnabled: false, maxLength: 16000 }));
  assert.equal((markup.match(/<textarea/g) || []).length, 1);
  assert.match(markup, /Message Explorer/); assert.match(markup, /maxLength="16000"/i);
  assert.doesNotMatch(markup, /role="listbox"|type="file"|Voice|Stop run/);
});

test("tree shows root and two delegation levels, retains deeper branches behind expansion", () => {
  const agents = [root, taskAgent(task("a")), taskAgent(task("b", "a")), taskAgent(task("c", "b"))];
  const markup = renderToStaticMarkup(createElement(TeamTree, { agents, profiles: [], selected: "", inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {} }));
  assert.match(markup, /data-agent-id="main"/); assert.match(markup, /data-agent-id="b"/);
  assert.doesNotMatch(markup, /data-agent-id="c"/); assert.match(markup, /1 nested agent/);
  assert.doesNotMatch(markup, /Earlier work|row-live-detail|New<\/|origin-link/);
});

test("timeline renders nested task branches with compact terminal rows and readable results", () => {
  const agents = [root, taskAgent({ ...task("a"), status: "completed", result: "Result evidence" }), taskAgent(task("b", "a"))];
  const markup = renderToStaticMarkup(createElement(TaskTimeline, { id: "a", agents, inspect() {}, arrivals: { b: Date.now() } }));
  assert.match(markup, /task-timeline-row is-settled/); assert.match(markup, /Result evidence/); assert.match(markup, /data-task-id="b" class="task-timeline-node is-arriving"/);
  assert.match(markup, /Spawned by a/); assert.doesNotMatch(markup, /started 2 agents|View agents/);
});

test("unused discovered profiles appear as not started and used profiles are not duplicated", () => {
  const markup = renderToStaticMarkup(createElement(TeamTree, { agents: [root], profiles: [
    { id: "research", name: "Researcher", description: "Survey", provider: "openai", model: "model", used: false, available: true },
    { id: "assistant", name: "Assistant", description: "Main", provider: "openai", model: "model", used: true, available: true },
  ], selected: "", inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {} }));
  assert.match(markup, /Researcher/); assert.match(markup, /Not started/); assert.equal((markup.match(/class="team-profile"/g) || []).length, 1);
});
