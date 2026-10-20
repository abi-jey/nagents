import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { Composer } from "./Composer.js";
import { appendEvent } from "./transcript.js";
import { TaskTimeline } from "../team/TaskTimeline.js";
import { AgentConfiguration, TeamPane } from "../team/TeamPane.js";
import { TeamTree } from "../team/TeamTree.js";
import { TaskConversation, taskEntries } from "../team/TaskConversation.js";
import { profileTarget, useTeam, type TeamController } from "../team/useTeam.js";
import { TaskDrafts, agentAncestors, mergeTaskActivity, orderedTeam, providerLabel, taskAgent, type TaskActivity, type TeamAgent, type TaskDetail, type TeamProfile } from "../team/model.js";
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

test("team keeps newest spawn first despite later updates, child activity, and phase changes", () => {
  const olderSpawn = taskAgent({ ...task("old", "", 100), started_at: 10, phase: "waiting", status: "running" });
  const newerSpawn = taskAgent({ ...task("new", "", 1), started_at: 20, phase: "running", status: "running" });
  const activeDescendant = taskAgent({ ...task("nested", "old", 1000), started_at: 30, phase: "failed", status: "failed" });
  const agents = [root, olderSpawn, newerSpawn, activeDescendant];
  const tree = orderedTeam(agents);
  assert.deepEqual(tree.children.get("")?.map(item => item.id), ["new", "old"]);
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

test("used definitions remain in their own list and task names stay distinct from their definition", () => {
  const research = taskAgent({ ...task("market-survey"), name: "Market survey", profile: "research" });
  const profiles: TeamProfile[] = [
    { id: "research", name: "Researcher", description: "Survey", provider: "openai", model: "model",
      source: "definitions/researcher.yaml", used: true, available: true },
    { id: "summary", name: "Summarizer", description: "Write a concise summary", provider: "openai", model: "model",
      used: false, available: true },
    { id: "assistant", name: "Assistant", description: "Main", provider: "openai", model: "model", used: true, available: true },
  ];
  const markup = renderToStaticMarkup(createElement(TeamTree, { agents: [root, research], profiles,
    selected: "", inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {} }));
  const document = new JSDOM(markup).window.document;
  const definitions = document.querySelector<HTMLElement>('[aria-label="Agent definitions"]');
  assert.ok(definitions);
  assert.ok(document.querySelector(".team-tree-scroll"));
  assert.ok(definitions.querySelector(".team-profile-list"), "definitions have their own scroll region");
  assert.equal(definitions.querySelectorAll(".team-profile-row").length, 3,
    "used definitions remain listed alongside unused definitions");
  const usedDefinition = [...definitions.querySelectorAll<HTMLButtonElement>(".team-profile")]
    .find(button => button.querySelector("strong")?.textContent === "Researcher");
  assert.ok(usedDefinition);
  assert.match(usedDefinition.textContent || "", /1 active/);
  assert.doesNotMatch(usedDefinition.textContent || "", /Not started/);
  assert.equal(usedDefinition.title, "Definition file: definitions/researcher.yaml");
  const unusedDefinition = [...definitions.querySelectorAll<HTMLButtonElement>(".team-profile")]
    .find(button => button.querySelector("strong")?.textContent === "Summarizer");
  assert.match(unusedDefinition?.textContent || "", /Not started/);

  const taskRow = document.querySelector<HTMLElement>('[data-agent-id="market-survey"]');
  assert.equal(taskRow?.querySelector(".team-agent-heading strong")?.textContent, "Market survey");
  assert.equal(taskRow?.querySelector(".team-agent-definition")?.textContent, "research");
  assert.match(taskRow?.querySelector(".team-agent-open")?.getAttribute("aria-label") || "", /Definition research/);

  const configuration = renderToStaticMarkup(createElement(AgentConfiguration, {
    agent: research, token: "", sessionId: "root", changed() {}, close() {}, configureDefinition() {},
  }));
  const configDocument = new JSDOM(configuration).window.document;
  const definitionTerm = [...configDocument.querySelectorAll("dt")].find(term => term.textContent === "Definition");
  assert.equal(definitionTerm?.nextElementSibling?.textContent, "research");
  assert.ok([...configDocument.querySelectorAll("button")].some(button => button.textContent?.includes("Edit research definition")));
});

test("starting a definition creates a separate child while preserving running row identity and drafts", async (t) => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    matchMedia: (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as unknown as MediaQueryList };
  Object.assign(globalThis, globals);
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout"] });
  type PendingRequest = { path: string; init: RequestInit; responded: boolean; respond: (value: Response) => void };
  const requests: PendingRequest[] = [];
  t.mock.method(globalThis, "fetch", (path: string | URL, init: RequestInit = {}) => {
    let fulfill!: (value: Response) => void;
    const result = new Promise<Response>(resolve => { fulfill = resolve; });
    const pending: PendingRequest = { path: String(path), init, responded: false,
      respond(value) { pending.responded = true; fulfill(value); } };
    requests.push(pending);
    return result;
  });
  const react = createRoot(dom.window.document.getElementById("root")!);
  let mounted = true;
  let team!: TeamController;
  const existing = { ...task("active-research"), name: "Current research run", profile: "research" };
  const definition: TeamProfile = { id: "research", name: "Researcher", description: "Survey", provider: "openai",
    model: "model", used: true, available: true, can_start: true };
  function View() {
    team = useTeam("token", "root", 0, true, []);
    const agents = [root, ...team.tasks.map(taskAgent)];
    return createElement(TeamPane, { team, agents, arrivals: {}, open: true, close() {}, configure() {} });
  }
  const respondIndexes = async (tasks: RetainedTask[]) => {
    const pending = requests.filter(request => !request.responded && request.path === "/api/sessions/root/tasks"
      && (!request.init.method || request.init.method === "GET"));
    assert.ok(pending.length, "a task-index refresh is pending");
    for (const request of pending) await act(async () => {
      const occupied = tasks.filter(item => item.status === "running").length;
      request.respond(Response.json({ tasks, profiles: [definition], subagent_usage: {
        limits: { max_concurrent_subagents: 3, max_subagent_executions: 8, max_subagent_depth: 2 },
        used_executions: tasks.length, remaining_executions: Math.max(0, 8 - tasks.length),
        occupied_slots: occupied, available_slots: Math.max(0, 3 - occupied),
        retained_tasks: tasks.length, max_retained_tasks: 64, depth: 0,
        reset: "Only a new external human root run resets used executions; settings reload does not.",
      } }));
    });
  };
  const refreshWith = async (tasks: RetainedTask[]) => {
    await act(async () => team.refresh());
    await respondIndexes(tasks);
  };
  try {
    await act(async () => react.render(createElement(View)));
    await respondIndexes([existing]);
    const existingRow = document.querySelector<HTMLElement>('[data-agent-id="active-research"]');
    assert.ok(existingRow);
    assert.match(document.querySelector(".team-profile")?.textContent || "", /1 active/);
    const usageLine = document.querySelector<HTMLElement>('[aria-label="Subagent limits and usage"]');
    assert.equal(usageLine?.textContent?.trim(), "1/3 slots · 1/8 executions");
    assert.match(usageLine?.title || "", /Depth 0\/2\. 7 executions remain/);
    assert.match(usageLine?.title || "", /Only a new external human root run resets/);

    await act(async () => team.select("active-research"));
    await act(async () => team.setPrompt("Keep this draft on the active run"));

    const definitionButton = [...document.querySelectorAll<HTMLButtonElement>(".team-profile")]
      .find(button => button.querySelector("strong")?.textContent === "Researcher");
    assert.ok(definitionButton);
    await act(async () => definitionButton.click());
    assert.equal(team.selected, profileTarget("research"));
    assert.equal(team.profile?.id, "research");
    await act(async () => team.setPrompt("Start another research conversation"));
    let submission!: Promise<void>;
    await act(async () => { submission = team.submit(); });
    const startIndex = requests.length - 1;
    assert.equal(requests[startIndex].path, "/api/sessions/root/tasks");
    assert.equal(requests[startIndex].init.method, "POST");
    const submittedBody = JSON.parse(String(requests[startIndex].init.body)) as { message_id: string; text: string; agent: string };
    assert.equal(submittedBody.text, "Start another research conversation");
    assert.equal(submittedBody.agent, "research", "the selected definition is sent as the new child recipient");
    assert.ok(submittedBody.message_id);
    const newChild = { ...task("new-research"), name: "Follow-up study", profile: "research" };
    await act(async () => {
      requests[startIndex].respond(Response.json({ task: newChild,
        receipt: { message_id: "new-message", task_id: "new-research", status: "picked_up", revision: 1 } }));
      await submission;
    });
    const newChildRow = document.querySelector<HTMLElement>('[data-agent-id="new-research"]');
    assert.ok(newChildRow);
    assert.equal(document.querySelector('[data-agent-id="active-research"]'), existingRow,
      "starting another child preserves the existing run's DOM row");
    assert.equal(document.querySelector('[data-agent-id="new-research"] .team-agent-heading strong')?.textContent, "Follow-up study");
    assert.equal(document.querySelector('[data-agent-id="new-research"] .team-agent-definition')?.textContent, "research");
    assert.equal(team.selected, "new-research");
    assert.equal(team.prompt, "", "the new child consumes only its own draft");

    await refreshWith([{ ...existing }, { ...newChild, status: "completed", phase: "done" }]);
    assert.equal(document.querySelector('[data-agent-id="new-research"]'), newChildRow,
      "the new child's row survives its completion update");
    assert.equal(document.querySelector('[data-agent-id="active-research"]'), existingRow);
    assert.match(document.querySelector(".team-profile")?.textContent || "", /1 active/);

    await refreshWith([existing]);
    assert.equal(document.querySelector('[data-agent-id="new-research"]'), null,
      "removing a conversation removes only its row");
    assert.equal(document.querySelector('[data-agent-id="active-research"]'), existingRow);
    assert.match(document.querySelector(".team-profile")?.textContent || "", /1 active/,
      "the definition remains in the persistent list after one conversation is removed");
    await act(async () => team.select("active-research"));
    assert.equal(team.prompt, "Keep this draft on the active run");
  } finally {
    if (mounted) await act(async () => react.unmount());
    for (const pending of requests.filter(request => !request.responded)) {
      pending.respond(Response.json({ tasks: [], profiles: [definition], task: existing, history: [] }));
    }
    t.mock.timers.reset();
    dom.window.close();
    for (const name of Object.keys(globals)) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("team refreshes added and renamed definitions during a run without retargeting drafts or existing children", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, requestAnimationFrame: () => 0, cancelAnimationFrame() {} };
  Object.assign(globalThis, globals);
  t.mock.timers.enable({ apis: ["setInterval", "setTimeout"] });
  const requests: { path: string; signal?: AbortSignal; respond: (value: Response) => void }[] = [];
  t.mock.method(globalThis, "fetch", (path: string, init: RequestInit) => new Promise<Response>(respond => {
    requests.push({ path, signal: init.signal || undefined, respond });
  }));
  const react = createRoot(dom.window.document.getElementById("root")!);
  let mounted = true;
  let team!: TeamController;
  function View({ revision }: { revision: number }) {
    team = useTeam("token", "root", revision, true, []);
    return createElement(TeamTree, { agents: [root, ...team.tasks.map(taskAgent)], profiles: team.profiles,
      selected: team.selected, inspect: team.select, configure() {}, arrivals: {}, error: team.error, refresh: team.refresh });
  }
  const profile = (id: string): TeamProfile => ({ id, name: id, description: `${id} coding agent`,
    provider: "openai", model: "accepted-model", used: false, available: true, can_start: true });
  const respond = async (index: number, profiles: TeamProfile[], tasks: RetainedTask[] = []) => act(async () => {
    requests[index].respond(Response.json({ tasks, profiles }));
  });
  const shownProfiles = () => [...document.querySelectorAll(".team-profile strong")].map(row => row.textContent);
  try {
    await act(async () => react.render(createElement(View, { revision: 0 })));
    await respond(0, []);
    assert.deepEqual(shownProfiles(), []);
    await act(async () => { t.mock.timers.tick(300); });
    await respond(1, [profile("mint")]);
    assert.deepEqual(shownProfiles(), ["mint"]);
    assert.match(document.querySelector(".team-profile")?.textContent || "", /Not started/);
    await act(async () => team.select(profileTarget("mint")));
    await act(async () => team.setPrompt("Please inspect the workspace"));

    await act(async () => react.render(createElement(View, { revision: 1 })));
    await act(async () => { t.mock.timers.tick(300); });
    await respond(2, [profile("min")]);
    assert.deepEqual(shownProfiles(), ["min"]);
    assert.equal(team.selected, profileTarget("mint"), "A rename must not silently choose another recipient");
    assert.equal(team.prompt, "Please inspect the workspace");
    assert.equal(team.canMessage, false);
    const missing = renderToStaticMarkup(createElement(TaskConversation, { team, agents: [root], arrivals: {}, token: "", sessionId: "root", back() {} }));
    assert.match(missing, /profile was removed or renamed/);
    assert.match(missing, /Your draft is kept/);
    assert.doesNotMatch(missing, /Loading conversation/);

    const accepted = { ...task("accepted-child"), name: "mint", profile: "mint", model: "accepted-model" };
    await act(async () => react.render(createElement(View, { revision: 2 })));
    await act(async () => { t.mock.timers.tick(300); });
    await respond(3, [profile("mini")], [accepted]);
    assert.deepEqual(shownProfiles(), ["mini"]);
    assert.equal(document.querySelector('[data-agent-id="accepted-child"] strong')?.textContent, "mint");
    assert.equal(team.tasks[0].model, "accepted-model");
    await act(async () => team.select(profileTarget("mini")));
    assert.equal(team.profile?.name, "mini");
    assert.equal(team.canMessage, true);
    assert.equal(team.prompt, "", "A new definition must not inherit the removed profile's draft");
    await act(async () => team.select(profileTarget("mint")));
    assert.equal(team.prompt, "Please inspect the workspace");
    assert.ok(requests.every(request => request.path === "/api/sessions/root/tasks"));

    await act(async () => { t.mock.timers.tick(900); });
    assert.equal(requests.length, 5, "The active-run timer refreshes even without a transcript event");
    await act(async () => react.unmount());
    mounted = false;
    assert.equal(requests[4].signal?.aborted, true);
  } finally {
    if (mounted) await act(async () => react.unmount());
    t.mock.timers.reset(); dom.window.close();
    for (const name of Object.keys(globals)) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
