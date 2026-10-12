import assert from "node:assert/strict";
import { test } from "node:test";
import { buildActivityTree, pageActivityTree } from "./activityTree";
import { advanceTo, queueMainMessage, queueMessage, startDemo } from "./engine";
import { MAIN_AGENT_DEFINITION } from "./fixtures";
import { mainAgentSnapshot } from "./mainRuntime";
import type { ActivityState, AgentRun, RecordItem } from "./types";

function agent(id: string, parentId: string, spawnedAt = 1, records: RecordItem[] = []): AgentRun {
  return {
    definition: { ...MAIN_AGENT_DEFINITION, id, parentId, name: id, spawnedAt },
    startedAt: spawnedAt, status: "running", activity: "Working", records,
  };
}

function message(at: number): RecordItem {
  return { kind: "message", message: { id: `message-${at}`, at, role: "assistant", text: "Activity" } };
}

function stateWith(agents: AgentRun[], now = 100): ActivityState {
  return { ...startDemo(), now, agents: Object.fromEntries(agents.map(run => [run.definition.id, run])), seenEvents: [] };
}

function treeFor(state: ActivityState) { return buildActivityTree(state, mainAgentSnapshot(state, {})); }

test("the main conversation is the root of one activity-ordered tree", () => {
  const tree = treeFor(advanceTo(startDemo(), 8));
  assert.equal(tree.root.agent.definition.id, "assistant");
  assert.equal(tree.totalCount, 5);
  assert.deepEqual(tree.root.children.map(node => node.agent.definition.id), ["builder", "research", "tests"]);
  assert.equal(tree.nodesById.get("builder")?.children[0].agent.definition.id, "layout");
  assert.equal(tree.nodesById.get("builder")?.latestActivityAt, 6);
  const rows = pageActivityTree(tree, 20).rows;
  assert.equal(rows[0].depth, 0);
  assert.equal(rows[0].node.agent.definition.id, "assistant");
  assert.equal(rows.find(row => row.node.agent.definition.id === "layout")?.depth, 2);
});

test("the deepest descendant's latest activity moves its entire ancestor branch", () => {
  const state = stateWith([
    agent("old-parent", "assistant", 1, [message(3)]),
    agent("middle", "old-parent", 2, [message(4)]),
    agent("leaf", "middle", 3, [message(90)]),
    agent("recent-sibling", "assistant", 1, [message(80)]),
  ]);
  const tree = treeFor(state);
  assert.equal(tree.root.children[0].agent.definition.id, "old-parent");
  assert.equal(tree.nodesById.get("old-parent")?.ownActivityAt, 3);
  assert.equal(tree.nodesById.get("old-parent")?.latestActivityAt, 90);
  const page = pageActivityTree(tree, 1, "", { expandedIds: new Set(["middle"]) });
  assert.deepEqual(page.rows.map(row => row.node.agent.definition.id), ["assistant", "old-parent", "middle", "leaf"]);
  assert.equal(page.remainingCount, 1);
});

test("incoming queued messages and later pickup both count as real activity", () => {
  const completed = advanceTo(startDemo(), 92);
  const queued = queueMessage(completed, "layout", "Check landscape too.");
  const before = treeFor(queued);
  assert.equal(before.nodesById.get("layout")?.ownActivityAt, 92);
  assert.equal(before.root.children[0].agent.definition.id, "builder");
  assert.equal(before.nodesById.get("layout")?.agent.records.at(-1)?.kind, "message");
  assert.equal(treeFor({ ...queued, now: 94 }).nodesById.get("layout")?.ownActivityAt, 92, "scheduled follow-ups are not activity");
  const after = treeFor(advanceTo(queued, 95));
  assert.equal(after.nodesById.get("layout")?.ownActivityAt, 95);
});

test("main user messages and tools use their own timestamps without touching child recency", () => {
  const state = queueMainMessage(advanceTo(startDemo(), 92), "One more question.");
  const snapshot = mainAgentSnapshot(state, {});
  snapshot.records.push({ kind: "tool", tool: { id: "main-tool", name: "read_file", title: "Read", arguments: {}, startedAt: 90, finishedAt: 91, status: "completed", summary: "Read", output: "Content", format: "text" } });
  const tree = buildActivityTree(state, snapshot);
  assert.equal(tree.root.ownActivityAt, 92);
  assert.equal(tree.nodesById.get("performance")?.ownActivityAt, 86);
  const replied = advanceTo(state, 93);
  assert.equal(treeFor(replied).root.ownActivityAt, 93);
  const toolOnly = stateWith([], 100);
  const root = { ...mainAgentSnapshot(toolOnly, {}), records: snapshot.records.filter(record => record.kind === "tool") };
  assert.equal(buildActivityTree(toolOnly, root).root.ownActivityAt, 91);
});

test("observed status transitions count without needing a message or finished timestamp", () => {
  const initial = startDemo();
  const state = { ...initial, now: 19, agents: { tests: initial.agents.tests }, seenEvents: ["t4"] };
  assert.equal(treeFor(state).nodesById.get("tests")?.ownActivityAt, 19);
  assert.equal(treeFor({ ...state, seenEvents: [] }).nodesById.get("tests")?.ownActivityAt, 2);
  assert.equal(treeFor({ ...state, now: 18 }).nodesById.get("tests")?.ownActivityAt, 2, "even acknowledged future events cannot leak into ordering");
});

test("future agents, messages, tool results and pickups never affect visible recency", () => {
  const child = agent("child", "assistant", 1, [
    message(5), message(200),
    { kind: "message", message: { id: "queued", at: 8, pickedUpAt: 130, delivery: "picked_up", role: "user", text: "Later" } },
    { kind: "tool", tool: { id: "tool", name: "read_file", title: "Read", arguments: {}, startedAt: 9, finishedAt: 120, status: "completed", summary: "Read", output: "Content", format: "text" } },
  ]);
  const state = stateWith([child, agent("future", "assistant", 101)], 100);
  state.mainMessages.push({ id: "future-main", at: 150, role: "user", text: "Future" });
  const tree = treeFor(state);
  assert.equal(tree.nodesById.has("future"), false);
  assert.equal(tree.nodesById.get("child")?.ownActivityAt, 9);
  assert.equal(tree.root.ownActivityAt, 0);
  assert.equal(tree.root.latestActivityAt, 9);
});

test("clock ticks alone do not change ordering or activity times and ties are deterministic", () => {
  const state = stateWith([agent("z", "assistant", 1, [message(10)]), agent("a", "assistant", 1, [message(10)])], 20);
  const before = treeFor(state);
  const after = treeFor({ ...state, now: 90, agents: { a: state.agents.a, z: state.agents.z } });
  assert.deepEqual(before.root.children.map(node => [node.agent.definition.id, node.latestActivityAt]), [["a", 10], ["z", 10]]);
  assert.deepEqual(after.root, before.root);
});

test("paging retains selected ancestry and every additional page only adds nodes", () => {
  const tree = treeFor(advanceTo(startDemo(), 92));
  const first = pageActivityTree(tree, 1, "layout");
  assert.deepEqual([...first.visibleIds].sort(), ["assistant", "builder", "layout", "performance"]);
  assert.equal(first.remainingCount, 5);
  assert.equal(first.totalCount, 10);
  const next = pageActivityTree(tree, 4, "layout");
  for (const id of first.visibleIds) assert.ok(next.visibleIds.has(id));
  assert.ok(next.remainingCount < first.remainingCount);
  const all = pageActivityTree(tree, 100, "layout");
  assert.equal(all.remainingCount, 0);
  assert.equal(all.rows.length, 9);
  assert.equal(new Set(all.rows.map(row => row.node.agent.definition.id)).size, 9);
  assert.equal(tree.nodesById.get("reviewer")?.children.length, 1, "paging must not trim the source tree");
  assert.equal(tree.nodesById.get("security")?.children[0].agent.definition.id, "access-audit");
  const expanded = pageActivityTree(tree, 100, "", { expandedIds: new Set(["security"]) });
  assert.equal(expanded.rows.length, 10);
  assert.equal(expanded.remainingCount, 0);
});

test("the depth-three fixture can be expanded or inspected while its activity moves the whole review branch", () => {
  const before = treeFor(advanceTo(startDemo(), 60));
  assert.equal(before.root.children[0].agent.definition.id, "docs");
  const tree = treeFor(advanceTo(startDemo(), 61));
  assert.equal(tree.root.children[0].agent.definition.id, "reviewer");
  assert.equal(tree.nodesById.get("reviewer")?.latestActivityAt, 61);
  assert.equal(tree.nodesById.get("security")?.ownActivityAt, 56);
  const defaultPage = pageActivityTree(tree, 100);
  assert.equal(defaultPage.visibleIds.has("security"), true);
  assert.equal(defaultPage.visibleIds.has("access-audit"), false);
  assert.equal(defaultPage.remainingCount, 0, "collapsed descendants do not make More linger");
  const expanded = pageActivityTree(tree, 100, "", { expandedIds: new Set(["security"]) });
  assert.equal(expanded.rows.find(row => row.node.agent.definition.id === "access-audit")?.depth, 3);
  const selected = pageActivityTree(tree, 0, "access-audit");
  assert.deepEqual(selected.rows.map(row => row.node.agent.definition.id), ["assistant", "reviewer", "security", "access-audit"]);
});

test("cycles, self-links and missing parents stay reachable with deterministic ancestry", () => {
  const state = stateWith([agent("b", "a"), agent("a", "b"), agent("self", "self"), agent("orphan", "missing")]);
  const tree = treeFor(state);
  assert.equal(tree.totalCount, 5);
  assert.deepEqual(tree.root.children.map(node => node.agent.definition.id), ["a", "orphan", "self"]);
  assert.equal(tree.nodesById.get("a")?.children[0].agent.definition.id, "b");
  const selected = pageActivityTree(tree, 0, "b");
  assert.deepEqual(selected.rows.map(row => row.node.agent.definition.id), ["assistant", "a", "b"]);
  assert.equal(selected.remainingCount, 2);
  assert.deepEqual(treeFor({ ...state, agents: Object.fromEntries(Object.entries(state.agents).reverse()) }).root, tree.root);
});

test("empty and invalid page requests preserve the root and report exact remaining counts", () => {
  const empty = treeFor(stateWith([]));
  assert.equal(pageActivityTree(empty).rows.length, 1);
  assert.equal(pageActivityTree(empty).remainingCount, 0);
  const tree = treeFor(advanceTo(startDemo(), 92));
  assert.equal(pageActivityTree(tree, -1, "unknown").rows.length, 1);
  assert.equal(pageActivityTree(tree, -1).remainingCount, 8);
  assert.deepEqual(pageActivityTree(tree, Number.NaN).rows, pageActivityTree(tree, 4).rows);
});

test("depth two loads by default while deep descendants still determine branch ordering", () => {
  const state = stateWith([
    agent("parent", "assistant", 1), agent("middle", "parent", 2),
    agent("leaf", "middle", 3, [message(90)]), agent("sibling", "assistant", 1, [message(80)]),
  ]);
  const tree = treeFor(state);
  const page = pageActivityTree(tree, 100);
  assert.deepEqual(page.rows.map(row => row.node.agent.definition.id), ["assistant", "parent", "middle", "sibling"]);
  assert.equal(page.rows.find(row => row.node.agent.definition.id === "middle")?.node.latestActivityAt, 90);
  assert.equal(page.remainingCount, 0, "collapsed descendants are not More results");
  assert.equal(page.totalCount, 5);
  assert.equal(tree.nodesById.get("middle")?.children.length, 1, "the full tree supplies the expansion affordance");
  const expanded = pageActivityTree(tree, 100, "", { expandedIds: new Set(["middle"]) });
  assert.deepEqual(expanded.rows.map(row => row.node.agent.definition.id), ["assistant", "parent", "middle", "leaf", "sibling"]);
});

test("a selected deep path overrides collapse but does not reveal unrelated descendants", () => {
  const state = stateWith([
    agent("parent", "assistant"), agent("middle", "parent"),
    agent("selected", "middle", 2, [message(3)]), agent("hidden", "middle", 2, [message(80)]),
    agent("deepest", "selected", 3, [message(90)]),
  ]);
  const tree = treeFor(state);
  const selected = pageActivityTree(tree, 0, "deepest");
  assert.deepEqual(selected.rows.map(row => row.node.agent.definition.id), ["assistant", "parent", "middle", "selected", "deepest"]);
  assert.equal(selected.remainingCount, 0);
  const expanded = pageActivityTree(tree, 100, "deepest", { expandedIds: new Set(["middle"]) });
  assert.ok(expanded.visibleIds.has("hidden"));
});

test("expanded levels remain independently nested and lazy pagination counts only eligible nodes", () => {
  const state = stateWith([
    agent("parent", "assistant"), agent("middle", "parent"),
    agent("leaf", "middle", 2, [message(80)]), agent("deepest", "leaf", 3, [message(90)]),
    agent("sibling", "assistant", 1, [message(70)]),
  ]);
  const tree = treeFor(state);
  const page = pageActivityTree(tree, 1, "", { expandedIds: new Set(["middle"]) });
  assert.ok(page.visibleIds.has("leaf"));
  assert.equal(page.visibleIds.has("deepest"), false);
  assert.equal(page.remainingCount, 1, "only the eligible sibling is left to load");
  const fullyExpanded = pageActivityTree(tree, 100, "", { expandedIds: new Set(["middle", "leaf"]) });
  assert.equal(fullyExpanded.rows.length, 6);
  const collapsed = pageActivityTree(tree, 100, "", { defaultDepth: 0 });
  assert.equal(collapsed.rows.length, 1);
  assert.equal(collapsed.remainingCount, 0);
});

test("explicit expansion loads old direct children even when newer branches fill the page", () => {
  const tree = treeFor(stateWith([
    agent("old-parent", "assistant"), agent("old-middle", "old-parent"),
    agent("old-child-a", "old-middle", 2), agent("old-child-b", "old-middle", 2),
    agent("newer-a", "assistant", 1, [message(90)]), agent("newer-b", "assistant", 1, [message(80)]),
  ]));
  const collapsed = pageActivityTree(tree, 1);
  assert.deepEqual(collapsed.rows.map(row => row.node.agent.definition.id), ["assistant", "newer-a"]);
  const expanded = pageActivityTree(tree, 1, "", { expandedIds: new Set(["old-middle"]) });
  assert.deepEqual(expanded.rows.map(row => row.node.agent.definition.id), ["assistant", "newer-a", "old-parent", "old-middle", "old-child-a", "old-child-b"]);
  assert.equal(expanded.remainingCount, 1, "only the second newer branch remains behind More");
  assert.equal(expanded.totalCount, 7);
  assert.equal(tree.nodesById.get("old-child-a")?.ownActivityAt, 2, "opening a branch does not invent new activity");
});
