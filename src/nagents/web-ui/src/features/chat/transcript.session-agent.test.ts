import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { RequestError } from "../../api/client.js";
import { agentCommand, SessionAgentController, validSessionAgent, type SessionAgentSelection } from "../sessions/sessionAgent.js";
import { conversationAgent } from "../team/model.js";
import { TeamTree } from "../team/TeamTree.js";
import { Conversation } from "./Conversation.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";
import { LiveSessions } from "./liveTranscript.js";
import type { Snapshot } from "../../types.js";

const assistant = { agent: "assistant", mode: "build" as const, provider: "primary", provider_kind: "openai", model: "fixture-main" };
const reviewer = { agent: "reviewer", mode: "reviewer" as const, provider: "review-provider", provider_kind: "anthropic", model: "fixture-review" };
function selection(overrides: Partial<SessionAgentSelection> = {}): SessionAgentSelection {
  return { session_id: "ngn-one", revision: "r1", agent: "assistant", workspace_default: "assistant", effective: assistant,
    active: null, pending: false, locked_reason: "", profiles: [assistant, reviewer].map(({ agent, ...profile }) => ({
      ...profile, name: agent, description: `${agent} fixture profile`, disabled_reason: "",
    })), ...overrides };
}
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function harness(overrides: Partial<ConstructorParameters<typeof SessionAgentController>[0]> = {}) {
  let current: SessionAgentSelection | undefined, error = "", saving = false;
  const controller = new SessionAgentController({ sessionId: "ngn-one", read: async () => selection(),
    save: async (_revision, agent) => selection({ revision: "r2", agent, effective: reviewer }),
    accept: value => { current = value; }, error: value => { error = value; }, loading() {}, saving: value => { saving = value; }, ...overrides });
  return { controller, current: () => current, error: () => error, saving: () => saving };
}

test("agent command defaults to conversation and requires explicit workspace scope", () => {
  assert.deepEqual(agentCommand(""), { scope: "conversation", name: "" });
  assert.deepEqual(agentCommand("reviewer"), { scope: "conversation", name: "reviewer" });
  assert.deepEqual(agentCommand("--workspace"), { scope: "workspace", name: "" });
  assert.deepEqual(agentCommand("--workspace reviewer"), { scope: "workspace", name: "reviewer" });
  for (const value of ["--global reviewer", "--workspace assistant extra", "--workspace --bad"]) assert.throws(() => agentCommand(value), /Usage:/);
});

test("selection validates active identity and rejects malformed profile policy", () => {
  assert.ok(validSessionAgent(selection()));
  assert.ok(validSessionAgent(selection({ agent: "reviewer", effective: reviewer, active: assistant, pending: true })));
  for (const invalid of [{ active: {} }, { pending: "yes" }, { profiles: [{ name: "assistant" }] }, { revision: "" }])
    assert.equal(validSessionAgent({ ...selection(), ...invalid }), false);
});

test("incoming selection invalidates an older HTTP read and rejects another chat", async () => {
  const read = deferred<SessionAgentSelection>();
  const h = harness({ read: () => read.promise });
  const fetch = h.controller.refresh();
  h.controller.accept(selection({ revision: "r2", agent: "reviewer", effective: reviewer }));
  h.controller.accept(selection({ session_id: "ngn-other", revision: "other" }));
  read.resolve(selection()); await fetch;
  assert.equal(h.current()!.revision, "r2");
  assert.equal(h.current()!.agent, "reviewer");
  h.controller.dispose();
});

test("saving is revision guarded and cannot replace a newer streamed selection", async () => {
  const write = deferred<SessionAgentSelection>(), calls: string[][] = [];
  const h = harness({ read: async () => selection({ revision: "r3" }), save: async (revision, agent) => { calls.push([revision, agent]); return write.promise; } });
  h.controller.accept(selection());
  const saved = h.controller.choose("reviewer");
  assert.equal(h.saving(), true);
  assert.equal(await h.controller.choose("assistant"), false);
  h.controller.accept(selection({ revision: "r3" }));
  write.resolve(selection({ revision: "r2", agent: "reviewer", effective: reviewer }));
  assert.equal(await saved, true);
  await Promise.resolve();
  assert.deepEqual(calls, [["r1", "reviewer"]]);
  assert.equal(h.current()!.revision, "r3");
  assert.equal(h.saving(), false);
  h.controller.dispose();
});

test("stale selection refreshes without silently retrying a write", async () => {
  let writes = 0;
  const h = harness({ read: async () => selection({ revision: "current" }), save: async () => { writes++; throw new RequestError("Agent selection changed. Refresh and choose again.", 409); } });
  h.controller.accept(selection());
  assert.equal(await h.controller.choose("reviewer"), false);
  assert.equal(writes, 1);
  assert.equal(h.current()!.revision, "current");
  assert.match(h.error(), /choose again/);
  h.controller.dispose();
});

test("locked, unavailable and unknown profiles never issue mutations", async () => {
  let writes = 0;
  const h = harness({ save: async () => { writes++; return selection(); } });
  h.controller.accept(selection({ locked_reason: "Pinned Agent Designer route" }));
  assert.equal(await h.controller.choose("reviewer"), false);
  assert.match(h.error(), /Pinned/);
  h.controller.accept(selection({ profiles: selection().profiles.map(profile => ({ ...profile, disabled_reason: "Not user invocable" })) }));
  assert.equal(await h.controller.choose("reviewer"), false);
  assert.match(h.error(), /Not user invocable/);
  h.controller.accept(selection());
  assert.equal(await h.controller.choose("missing"), false);
  assert.match(h.error(), /not found/);
  assert.equal(writes, 0);
  h.controller.dispose();
});

test("navigation disposes pending reads and saves without publishing into the next chat", async () => {
  const write = deferred<SessionAgentSelection>(); let signal: AbortSignal | undefined;
  const h = harness({ save: (_revision, _agent, value) => { signal = value; return write.promise; } });
  h.controller.accept(selection());
  const saving = h.controller.choose("reviewer");
  h.controller.dispose();
  assert.equal(signal!.aborted, true);
  write.resolve(selection({ revision: "late", agent: "reviewer", effective: reviewer }));
  assert.equal(await saving, false);
  assert.equal(h.current()!.agent, "assistant");
});

test("main team row uses the conversation profile and retains the active identity while a change waits", () => {
  for (const active of [assistant, null]) {
    const state = selection({ agent: "reviewer", effective: reviewer, active, pending: !!active });
    const root = conversationAgent(state, undefined, !!active, false);
    const dom = new JSDOM(renderToStaticMarkup(createElement(TeamTree, { agents: [root], profiles: [], selected: "", inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {} })));
    try {
      const row = dom.window.document.querySelector('[data-agent-id="main"]')!;
      assert.ok(row.textContent!.includes((active || reviewer).agent));
      assert.ok(row.textContent!.includes((active || reviewer).model));
      assert.ok(row.textContent!.includes((active || reviewer).provider));
      assert.equal(root.name, (active || reviewer).agent);
    } finally { dom.window.close(); }
  }
});

test("agent changes are compact display-only markers, deduplicated through history and reconnect", () => {
  const event = { event: "agent_changed", history_id: "agent-change-1", agent: "reviewer", previous_agent: "assistant", provider: "review-provider", model: "fixture-review", anchor_history_id: "10" };
  let entries: Entry[] = [{ id: "old", kind: "assistant", historyId: "10", text: "Previous reply" }, { id: "queued", kind: "user", text: "Next question", queued: true }];
  entries = appendEvent(entries, event);
  assert.deepEqual(entries.map(entry => entry.kind), ["assistant", "agent_change", "user"]);
  assert.equal(appendEvent(entries, event), entries);
  const row = { ...event, role: "agent_changed", content: "Agent: reviewer", name: "", tool_call_id: "", tool_calls: [] };
  const snapshot: Snapshot = { session_id: "ngn-one", sessions: [], retained_tasks: [], history: [row] };
  const cache = new LiveSessions();
  cache.receive({ type: "event", epoch: "one", cursor: 1, session_id: "ngn-one", record: event });
  cache.receive({ type: "snapshot", epoch: "one", cursor: 2, session_id: "ngn-one", snapshot });
  assert.equal(cache.get("ngn-one").entries.length, 1);
  assert.equal(cache.get("ngn-one").entries[0].kind, "agent_change");
  const cold = fromHistory(snapshot);
  assert.equal(cold.length, 1);
  const dom = new JSDOM(renderToStaticMarkup(createElement(Conversation, { entries: cold, sessionId: "ngn-one", demo: false, canSubmit: false, submit() {} })));
  try {
    assert.equal(dom.window.document.querySelectorAll(".agent-change-marker").length, 1);
    assert.equal(dom.window.document.querySelector(".agent-change-marker")!.textContent, "Agent: reviewer");
    assert.equal(dom.window.document.querySelectorAll(".entry-label").length, 0);
  } finally { dom.window.close(); }
  cache.receive({ type: "snapshot", epoch: "one", cursor: 3, session_id: "ngn-one", snapshot: { ...snapshot, history: [] } });
  assert.equal(cache.get("ngn-one").entries.length, 0, "Cleared history removes its display markers");
});
