import assert from "node:assert/strict";
import { test } from "node:test";
import { advanceTo, queueMessage, startDemo } from "./engine";
import { DEMO_END } from "./fixtures";
import { fitTeamToViewport, partitionTeam, spawnGroups } from "./teamPresentation";
import type { ActivityState, AgentRun } from "./types";

test("the overview contains no future agents or spawn announcements", () => {
  const state = startDemo();
  assert.deepEqual(partitionTeam(state), { visible: [], older: [], recentCompletedIds: [], carriedAncestorIds: [] });
  assert.deepEqual(spawnGroups(state), []);
  const first = advanceTo(state, 2);
  assert.deepEqual(spawnGroups(first).map(group => [group.at, group.parentId, group.originMessageId, group.agentIds]), [
    [2, "assistant", "main-request", ["research", "builder", "tests"]],
  ]);
  const nested = spawnGroups(advanceTo(first, 5));
  assert.equal(nested.length, 2);
  assert.deepEqual(nested[0], spawnGroups(first)[0], "existing creation identities must not change when a nested delegation arrives");
  assert.deepEqual(nested[1].agentIds, ["layout"]);
  assert.equal(nested[1].parentId, "builder");
});

test("later delegations retain their own originating message and nested parent", () => {
  const before = advanceTo(startDemo(), 51);
  assert.equal(spawnGroups(before).length, 2);
  const roots = spawnGroups(advanceTo(before, 52));
  assert.equal(roots.length, 3);
  assert.deepEqual(roots[2].agentIds, ["reviewer", "docs", "performance"]);
  assert.equal(roots[2].originMessageId, "main-followup-request");
  const nested = spawnGroups(advanceTo(before, 55));
  assert.equal(nested[3].parentId, "reviewer");
  assert.equal(nested[3].originMessageId, "main-followup-request");
  assert.deepEqual(nested[3].agentIds, ["security"]);
  assert.equal(new Set(nested.map(group => group.id)).size, nested.length);
  const deep = spawnGroups(advanceTo(before, 57));
  assert.equal(deep[4].parentId, "security");
  assert.equal(deep[4].at, 57);
  assert.deepEqual(deep[4].agentIds, ["access-audit"]);
  assert.equal(deep[4].originMessageId, "main-followup-request");
});

test("active and waiting work stays visible alongside the three latest completions", () => {
  const state = advanceTo(startDemo(), 60);
  const partition = partitionTeam(state);
  assert.deepEqual(partition.recentCompletedIds, ["docs", "tests", "builder"]);
  assert.deepEqual(partition.visible.map(agent => agent.definition.id), ["builder", "tests", "reviewer", "docs", "performance", "security", "access-audit"]);
  assert.equal(state.agents.performance.status, "waiting");
  assert.deepEqual(partition.older.map(agent => agent.definition.id), ["research", "layout"]);
  assert.ok(partition.older.every(agent => agent.status === "completed"));
  assert.equal(new Set([...partition.visible, ...partition.older].map(agent => agent.definition.id)).size, 9);
});

test("recent completion order follows finish time, including nested agents", () => {
  const partition = partitionTeam(advanceTo(startDemo(), DEMO_END));
  assert.deepEqual(partition.recentCompletedIds, ["performance", "reviewer", "security"]);
  assert.deepEqual(partition.older.map(agent => agent.definition.id), ["access-audit", "docs", "tests", "builder", "research", "layout"]);
  assert.equal(partition.visible.length, 3);
  assert.equal(partition.older.length, 6);
});

test("a completed ancestor is retained when its child remains active", () => {
  const state = advanceTo(startDemo(), 56);
  const withCompletedParent = { ...state, agents: { ...state.agents, reviewer: { ...state.agents.reviewer, status: "completed" as const, finishedAt: 54 } } };
  const partition = partitionTeam(withCompletedParent, 0);
  assert.ok(partition.visible.some(agent => agent.definition.id === "security"));
  assert.ok(partition.visible.some(agent => agent.definition.id === "reviewer"));
  assert.deepEqual(partition.carriedAncestorIds, ["reviewer"]);
  assert.equal(partition.older.some(agent => agent.definition.id === "reviewer"), false);
  assert.deepEqual(partition.recentCompletedIds, []);
});

test("queued followups and failed agents cannot be buried in older completions", () => {
  const done = advanceTo(startDemo(), DEMO_END);
  const queued = queueMessage(done, "layout", "Check landscape as well.");
  const failed = { ...queued, agents: { ...queued.agents, research: { ...queued.agents.research, status: "failed" as const } } };
  const partition = partitionTeam(failed);
  assert.ok(partition.visible.some(agent => agent.definition.id === "layout"));
  assert.ok(partition.visible.some(agent => agent.definition.id === "research"));
  assert.ok(partition.visible.some(agent => agent.definition.id === "builder"), "a queued nested followup retains its completed parent");
  assert.deepEqual(partition.carriedAncestorIds, ["builder"]);
  assert.deepEqual(partition.recentCompletedIds, ["performance", "reviewer", "security"]);
  assert.equal(partition.older.some(agent => ["layout", "research", "builder"].includes(agent.definition.id)), false);
  assert.equal(done.agents.layout.records.length + 1, queued.agents.layout.records.length);
});

type TestAgent = { id: string; parentId?: string; origin?: string; status?: AgentRun["status"]; finishedAt?: number };

function teamState(...agents: TestAgent[]): ActivityState {
  const initial = startDemo();
  const template = initial.agents.research;
  return {
    ...initial,
    now: 100,
    agents: Object.fromEntries(agents.map(({ id, parentId = "assistant", origin = "first-message", status = "completed", finishedAt = 10 }) => [id, {
      ...template,
      definition: { ...template.definition, id, parentId, originMessageId: origin, spawnedAt: 0 },
      startedAt: 0,
      finishedAt,
      status,
    }])),
  };
}

const viewport = { availableHeight: 300, rowHeight: 40, originHeight: 20, moreHeight: 30, maxRecent: 3 };

test("viewport fitting keeps at most three recent completions and fits fewer in a short pane", () => {
  const state = teamState({ id: "active", status: "running" }, { id: "first", finishedAt: 1 }, { id: "second", finishedAt: 2 }, { id: "third", finishedAt: 3 }, { id: "fourth", finishedAt: 4 });
  const tall = fitTeamToViewport(state, viewport);
  assert.equal(tall.selectedRecentLimit, 3);
  assert.deepEqual(tall.recentCompletedIds, ["fourth", "third", "second"]);
  assert.equal(tall.estimatedHeight, 210);
  assert.equal(tall.activeOverflow, false);
  const short = fitTeamToViewport(state, { ...viewport, availableHeight: 130 });
  assert.equal(short.selectedRecentLimit, 1);
  assert.deepEqual(short.visible.map(agent => agent.definition.id), ["active", "fourth"]);
  assert.deepEqual(short.older.map(agent => agent.definition.id), ["third", "second", "first"]);
  assert.equal(short.estimatedHeight, 130);
  assert.equal(short.activeOverflow, false);
});

test("More reserves space only when older completions exist, including an exact-fit boundary", () => {
  const state = teamState({ id: "first", finishedAt: 1 }, { id: "second", finishedAt: 2 });
  const exact = fitTeamToViewport(state, { ...viewport, availableHeight: 100 });
  assert.equal(exact.selectedRecentLimit, 2);
  assert.equal(exact.older.length, 0);
  assert.equal(exact.estimatedHeight, 100, "all rows fit because no More control is needed");
  const one = fitTeamToViewport(state, { ...viewport, availableHeight: 90 });
  assert.equal(one.selectedRecentLimit, 1);
  assert.equal(one.estimatedHeight, 90);
  const onlyMore = fitTeamToViewport(state, { ...viewport, availableHeight: 89 });
  assert.equal(onlyMore.selectedRecentLimit, 0);
  assert.equal(onlyMore.estimatedHeight, 30);
  assert.equal(onlyMore.activeOverflow, false);
});

test("viewport fitting counts unique conversation origins and carried ancestors", () => {
  const state = teamState({ id: "parent", finishedAt: 1 }, { id: "child", parentId: "parent", finishedAt: 4 }, { id: "other", origin: "later-message", finishedAt: 3 }, { id: "older", finishedAt: 2 });
  const fitting = fitTeamToViewport(state, { ...viewport, availableHeight: 150 });
  assert.equal(fitting.selectedRecentLimit, 1);
  assert.deepEqual(fitting.visible.map(agent => agent.definition.id), ["parent", "child"]);
  assert.deepEqual(fitting.carriedAncestorIds, ["parent"]);
  assert.equal(fitting.estimatedHeight, 130, "the nested pair shares one origin header");
  const taller = fitTeamToViewport(state, { ...viewport, availableHeight: 190 });
  assert.equal(taller.selectedRecentLimit, 2);
  assert.equal(taller.estimatedHeight, 190, "the next completion adds a row and another origin header");
});

test("short panes retain active, waiting, failed and scheduled or queued branches with their ancestors", () => {
  const state = teamState({ id: "active", status: "running" }, { id: "waiting", status: "waiting" }, { id: "failed", status: "failed" }, { id: "parent" }, { id: "queued", parentId: "parent" }, { id: "scheduled" }, { id: "older" });
  const queued = queueMessage(state, "queued", "Check the narrow layout.");
  const scheduled = { ...queued, followUps: { ...queued.followUps, scheduled: 110 } };
  const fitting = fitTeamToViewport(scheduled, { ...viewport, availableHeight: 40 });
  assert.equal(fitting.selectedRecentLimit, 0);
  assert.deepEqual(fitting.visible.map(agent => agent.definition.id), ["active", "waiting", "failed", "parent", "queued", "scheduled"]);
  assert.deepEqual(fitting.carriedAncestorIds, ["parent"]);
  assert.deepEqual(fitting.older.map(agent => agent.definition.id), ["older"]);
  assert.equal(fitting.activeOverflow, true);
  assert.equal(fitting.estimatedHeight, 290);
});

test("empty history takes no space, while an active-only pane never invents More", () => {
  const empty = fitTeamToViewport(startDemo(), viewport);
  assert.equal(empty.selectedRecentLimit, 0);
  assert.equal(empty.estimatedHeight, 0);
  assert.equal(empty.activeOverflow, false);
  const active = fitTeamToViewport(teamState({ id: "active", status: "running" }), { ...viewport, availableHeight: 60 });
  assert.equal(active.estimatedHeight, 60);
  assert.deepEqual(active.older, []);
  assert.equal(active.activeOverflow, false);
});

test("invalid dimensions stay finite and cannot archive actionable agents", () => {
  const state = teamState({ id: "active", status: "running" }, { id: "completed" });
  for (const availableHeight of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
    const fitting = fitTeamToViewport(state, { ...viewport, availableHeight });
    assert.deepEqual(fitting.visible.map(agent => agent.definition.id), ["active"]);
    assert.equal(fitting.activeOverflow, true);
    assert.equal(fitting.estimatedHeight, 90);
  }
  const zeroed = fitTeamToViewport(state, { availableHeight: 0, rowHeight: -1, originHeight: Number.NaN, moreHeight: Number.POSITIVE_INFINITY, maxRecent: Number.NaN });
  assert.equal(zeroed.selectedRecentLimit, 1);
  assert.equal(zeroed.estimatedHeight, 0);
  assert.equal(zeroed.activeOverflow, false);
  const huge = fitTeamToViewport(state, { ...viewport, rowHeight: Number.MAX_VALUE, originHeight: Number.MAX_VALUE, moreHeight: Number.MAX_VALUE });
  assert.equal(huge.estimatedHeight, Number.MAX_SAFE_INTEGER);
  assert.equal(huge.activeOverflow, true);
});

test("the recent preference is bounded, floored, and counts available completions", () => {
  const state = teamState({ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" });
  assert.equal(fitTeamToViewport(state, { ...viewport, maxRecent: 200 }).selectedRecentLimit, 3);
  assert.equal(fitTeamToViewport(state, { ...viewport, maxRecent: 1.9 }).selectedRecentLimit, 1);
  assert.equal(fitTeamToViewport(state, { ...viewport, maxRecent: -1 }).selectedRecentLimit, 0);
  assert.equal(fitTeamToViewport(state, { ...viewport, maxRecent: Number.NaN }).selectedRecentLimit, 3);
  assert.equal(fitTeamToViewport(state, { ...viewport, maxRecent: Number.POSITIVE_INFINITY }).selectedRecentLimit, 3);
});
