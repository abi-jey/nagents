import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { TaskActivity, TeamAgent, TeamProfile } from "../team/model.js";
import { DelegationActivity } from "./DelegationActivity.js";
import { DelegationFlow } from "./delegationFlow.js";
import { appendEvent, type Entry } from "./transcript.js";

const runId = "root-run";

function agent(id: string, profile: string, name: string, providerKind = "a2a"): TeamAgent {
  return {
    id, name, profile, parentId: "", prompt: "Review the session path.", phase: "done", reason: "",
    provider: "", providerKind, model: "", startedAt: 1, updatedAt: 2, canMessage: true, messageReason: "",
  };
}

function profile(id: string, values: Partial<TeamProfile> = {}): TeamProfile {
  return {
    id, name: "Local profile name", description: "Local profile description", provider: "a2a", model: "",
    used: true, available: true, remote_name: "Remote profile name", remote_description: "Remote profile description",
    ...values,
  };
}

function options(agents: TeamAgent[], profiles?: TeamProfile[]) {
  return { agents, activity: [] as TaskActivity[], ...(profiles ? { profiles } : {}), inspect: (_id: string) => undefined };
}

function completedDelegation(profileId = "mini", assignedName = "Assigned willow"): Entry[] {
  let entries: Entry[] = [];
  entries = appendEvent(entries, {
    event: "tool_call", run_id: runId, id: "delegate-call", name: "to_agent",
    arguments: { agent: profileId, prompt: "Review the session path." },
  });
  entries = appendEvent(entries, {
    event: "tool_result", run_id: runId, id: "delegate-call", name: "to_agent", result: { session_id: "child", status: "running" },
  });
  entries = appendEvent(entries, {
    event: "task_started", run_id: runId, session_id: "child", parent_session_id: "", activation: 0,
    title: assignedName, prompt: "Review the session path.",
  });
  return appendEvent(entries, {
    event: "task_completed", run_id: runId, session_id: "child", parent_session_id: "", activation: 0,
    title: assignedName, status: "completed", result: "The session path is sound.",
  });
}

function rows(entries: Entry[]): Entry[] {
  return entries.filter(entry => entry.delegation !== undefined);
}

function markup(item: NonNullable<Entry["delegation"]>): string {
  return renderToStaticMarkup(createElement(DelegationActivity, { item, open: true, toggle() {} }));
}

test("live remote profile metadata updates the delegation card without changing its local identity or authority", () => {
  const flow = new DelegationFlow();
  const entries = completedDelegation();
  const agents = [agent("child", "mini", "Assigned willow")];
  const call = entries[0];
  const originalInputs = call.inputs;

  const first = rows(flow.project(entries, options(agents, [profile("mini", {
    remote_name: "Remote reviewer one", remote_description: "First public description.",
  })])));
  const second = rows(flow.project(entries, options(agents, [profile("mini", {
    remote_name: "Remote reviewer two", remote_description: "Updated public description.",
  })])));
  assert.deepEqual(first.map(row => row.id), second.map(row => row.id));
  assert.deepEqual(first.map(row => row.delegation?.kind), ["start", "result"]);
  for (const row of second) {
    assert.equal(row.delegation?.profile, "mini", "the local connection alias remains the routing identity");
    assert.equal(row.delegation?.name, "Assigned willow", "the task title remains the assigned instance name");
    assert.equal(row.delegation?.profileName, "Remote reviewer two");
    assert.equal(row.delegation?.profileDescription, "Updated public description.");
  }
  assert.equal(call.inputs, originalInputs, "remote metadata cannot change the recorded tool arguments");
  assert.equal(second[1].delegation?.text, "The session path is sound.", "remote metadata cannot replace lifecycle result text");

  const html = markup(second[0].delegation!);
  assert.match(html, /<strong[^>]*>Remote reviewer two<\/strong>/);
  assert.match(html, /<span class="delegation-instance">Assigned willow<\/span>/);
  assert.match(html, /<dt>Connection<\/dt><dd><code>mini<\/code><\/dd>/);
  assert.match(html, /Updated public description\./);
});

test("native profiles keep their local label and description even if remote metadata fields are present", () => {
  const entries = completedDelegation("native-mini", "Native willow");
  const projected = rows(new DelegationFlow().project(entries, options(
    [agent("child", "native-mini", "Native willow", "openai")],
    [profile("native-mini", {
      name: "Native local name", description: "Native local description.",
      remote_name: "Must not appear", remote_description: "Must not appear either", provider: "openai",
    })],
  )));
  for (const row of projected) {
    assert.equal(row.delegation?.profile, "native-mini");
    assert.equal(row.delegation?.profileName, undefined);
    assert.equal(row.delegation?.profileDescription, undefined);
  }
  const html = markup(projected[0].delegation!);
  assert.match(html, /<strong[^>]*>native-mini<\/strong>/);
  assert.doesNotMatch(html, /Must not appear|Native local description/);
});

test("missing profile metadata falls back to the local profile alias", () => {
  const entries = completedDelegation("unavailable-mini", "Unchanged willow");
  const projected = rows(new DelegationFlow().project(entries, options([agent("child", "unavailable-mini", "Unchanged willow")])));
  assert.equal(projected.length, 2);
  for (const row of projected) {
    assert.equal(row.delegation?.profile, "unavailable-mini");
    assert.equal(row.delegation?.profileName, undefined);
    assert.equal(row.delegation?.profileDescription, undefined);
  }
  assert.match(markup(projected[0].delegation!), /<strong[^>]*>unavailable-mini<\/strong>/);
});
