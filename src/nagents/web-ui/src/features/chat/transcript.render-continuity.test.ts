import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import type { Snapshot, WireEvent } from "../../types.js";
import { defaultAppearance, type ChatAppearance } from "../settings/appearance.js";
import type { ArrivalTimes } from "../team/TeamTree.js";
import { TaskTimeline } from "../team/TaskTimeline.js";
import { TaskConversation } from "../team/TaskConversation.js";
import type { TeamController } from "../team/useTeam.js";
import type { TaskActivity, TaskDetail } from "../team/model.js";
import type { TeamAgent } from "../team/model.js";
import { appendEvent, type Entry, type TaskThread } from "./transcript.js";
import { Conversation } from "./Conversation.js";
import { applySnapshot, LiveSessions } from "./liveTranscript.js";

type HistoryRow = Snapshot["history"][number];
const row = (history_id: string, role: string, content: string, extra: Partial<HistoryRow> = {}): HistoryRow => ({
  history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra,
});
const snapshot = (history: HistoryRow[], active_run?: Snapshot["active_run"]): Snapshot => ({
  session_id: "root", sessions: [], retained_tasks: [], history, active_run,
});

function fixture() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: (callback: FrameRequestCallback) => dom.window.setTimeout(() => callback(0), 0),
    cancelAnimationFrame: (handle: number) => dom.window.clearTimeout(handle),
  });
  Object.assign(dom.window.HTMLElement.prototype, {
    attachEvent(this: HTMLElement, type: string, listener: EventListener) {
      this.addEventListener(type.replace(/^on/, ""), listener);
    },
    detachEvent(this: HTMLElement, type: string, listener: EventListener) {
      this.removeEventListener(type.replace(/^on/, ""), listener);
    },
  });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const renderElement = (element: ReactNode) => act(async () => root.render(element));
  const render = (entries: Entry[], options: {
    appearance?: ChatAppearance;
    renderTask?: (thread: TaskThread) => ReactNode;
  } = {}) => act(async () => root.render(createElement(Conversation, {
    entries,
    sessionId: "root",
    demo: false,
    canSubmit: false,
    submit() {},
    ...options,
  })));
  return {
    dom,
    container,
    render, renderElement,
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "requestAnimationFrame", "cancelAnimationFrame"])
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
        else Reflect.deleteProperty(globalThis, key);
    },
  };
}

test("a tool row stays mounted when late canonical data changes its activity category and group", async () => {
  const ui = fixture();
  let entries: Entry[] = [];
  entries = appendEvent(entries, {
    event: "tool_call_progress", run_id: "run", generation_id: "generation", index: 0,
    id: "first", name: "lookup", status: "streaming", arguments_text: "{}",
  });
  entries = appendEvent(entries, {
    event: "tool_call_progress", run_id: "run", generation_id: "generation", index: 1,
    id: "second", name: "lookup", status: "streaming", arguments_text: "{}",
  });
  const second = entries.find(entry => entry.kind === "tool" && entry.toolIndex === 1)!;
  const secondKey = second.id;
  try {
    await ui.render(entries, { appearance: defaultAppearance });
    const record = ui.container.querySelector<HTMLElement>(`[data-record-key="${secondKey}"]`);
    assert.ok(record);
    assert.ok(record.closest(".chat-activity-group"));

    entries = appendEvent(entries, {
      event: "tool_call", run_id: "run", generation_id: "generation", index: 1,
      id: "second", name: "shell", arguments: { command: "true" },
    });
    await ui.render(entries, { appearance: defaultAppearance });
    assert.equal(ui.container.querySelector(`[data-record-key="${secondKey}"]`), record,
      "a canonical name/category update must not remount the already shown tool row");
  } finally { await ui.close(); }
});

function agent(id: string, parentId: string, phase: TeamAgent["phase"]): TeamAgent {
  return {
    id, name: id === "parent" ? "Parent" : "Child", profile: "assistant", parentId,
    prompt: `Work for ${id}`, phase, reason: phase, provider: "", providerKind: "", model: "",
    startedAt: 1, updatedAt: 1, canMessage: false, messageReason: "",
  };
}

test("task timeline rows render from observed threads and keep identity as the registry enriches them", async () => {
  const ui = fixture();
  let entries: Entry[] = [];
  entries = appendEvent(entries, {
    event: "task_started", run_id: "run", task_id: "parent", activation: 0,
    name: "Parent", prompt: "Work for parent", depth: 1,
  });
  entries = appendEvent(entries, {
    event: "task_started", run_id: "run", task_id: "child", parent_task_id: "parent", activation: 0,
    name: "Child", prompt: "Work for child", depth: 2,
  });
  const agents: TeamAgent[] = [];
  const inspect = (_id: string) => {};
  const renderTask = (thread: TaskThread) => createElement(TaskTimeline, {
    key: thread.id, id: thread.id, thread, agents, inspect, arrivals: {},
  });
  try {
    await ui.render(entries, { renderTask });
    const timeline = ui.container.querySelector<HTMLOListElement>('[data-record-key="task:parent"]');
    const childRow = ui.container.querySelector<HTMLElement>('.task-timeline-node[data-task-id="child"]');
    assert.ok(timeline, "observed tasks should have their timeline before registry polling completes");
    assert.ok(childRow, "nested task lifecycle rows should appear from observed task state");

    agents.push(agent("parent", "", "running"), agent("child", "parent", "running"));
    await ui.render(entries, { renderTask });
    assert.equal(ui.container.querySelector('[data-record-key="task:parent"]'), timeline);
    assert.equal(ui.container.querySelector('.task-timeline-node[data-task-id="child"]'), childRow,
      "registry enrichment changes status and metadata without replacing the visible task row");

    entries = appendEvent(entries, {
      event: "task_completed", run_id: "run", task_id: "child", parent_task_id: "parent", activation: 0,
      name: "Child", status: "completed", result: "Finished",
    });
    agents[1] = agent("child", "parent", "done");
    await ui.render(entries, { renderTask });
    assert.equal(ui.container.querySelector('.task-timeline-node[data-task-id="child"]'), childRow,
      "active-to-terminal task state must update the existing row");
  } finally { await ui.close(); }
});

test("a Thinking row remains the same DOM node through empty completion metadata and the first text chunk", async () => {
  const ui = fixture();
  let entries: Entry[] = [];
  const metadata = (revision: number, status: "thinking" | "completed") => ({
    event: "response_metadata" as const,
    run_id: "run",
    response_id: "response",
    response_session_id: "root",
    revision,
    round_number: 0,
    history_id: "",
    status,
    summary: "",
    summary_truncated: false,
    summary_expired: false,
    usage_reported: false,
  });
  try {
    entries = appendEvent(entries, metadata(1, "thinking"));
    await ui.render(entries);
    const row = ui.container.querySelector<HTMLElement>("article.entry.assistant");
    assert.ok(row);
    assert.ok(row.querySelector(".response-thinking"));

    entries = appendEvent(entries, metadata(2, "completed"));
    await ui.render(entries);
    assert.equal(ui.container.querySelector("article.entry.assistant"), row,
      "completed empty metadata must keep the previously shown Thinking row mounted");

    entries = appendEvent(entries, { event: "text_chunk", run_id: "run", chunk: "The answer is ready." });
    await ui.render(entries);
    assert.equal(ui.container.querySelector("article.entry.assistant"), row,
      "the first text replaces the placeholder in place instead of reintroducing a new row");
    assert.ok(ui.container.textContent?.includes("The answer is ready."), "the first text event should still render");
  } finally { await ui.close(); }
});

test("approval-forced visibility is sticky for a tool under hidden activity appearance", async () => {
  const ui = fixture();
  let entries: Entry[] = [];
  const appearance: ChatAppearance = {
    ...defaultAppearance,
    activity: { ...defaultAppearance.activity, tools: { ...defaultAppearance.activity.tools, placement: "hidden" } },
  };
  const call = { event: "tool_call" as const, run_id: "run", id: "call", name: "lookup", arguments: { query: "x" } };
  try {
    entries = appendEvent(entries, call);
    entries = appendEvent(entries, {
      event: "approval", run_id: "run", id: "call", tool: "lookup", approval_id: "approval", arguments: call.arguments,
    });
    await ui.render(entries, { appearance });
    const row = ui.container.querySelector<HTMLElement>("article.entry.tool");
    assert.ok(row, "a required approval must reveal the hidden tool record");
    assert.equal(row.querySelector(".execution-state")?.textContent, "Needs approval");

    entries = appendEvent(entries, { event: "approval_closed", run_id: "run", approval_id: "approval", decision: "allow" });
    await ui.render(entries, { appearance });
    assert.equal(ui.container.querySelector("article.entry.tool"), row,
      "approval completion must not hide a row that was already shown");

    entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "call", result: "Done" });
    await ui.render(entries, { appearance });
    assert.equal(ui.container.querySelector("article.entry.tool"), row,
      "completion changes the existing row instead of removing it");
  } finally { await ui.close(); }
});

test("grouped approval activity stays open through completion until the user collapses it", async () => {
  const ui = fixture();
  let entries: Entry[] = [];
  const call = { event: "tool_call" as const, run_id: "run", id: "approval-call", name: "lookup", arguments: { query: "x" } };
  const closeByUser = async (element: HTMLDetailsElement) => act(async () => {
    element.open = false;
    element.dispatchEvent(new ui.dom.window.Event("toggle"));
  });
  try {
    entries = appendEvent(entries, call);
    entries = appendEvent(entries, {
      event: "approval", run_id: "run", id: "approval-call", tool: "lookup", approval_id: "approval-grouped", arguments: call.arguments,
    });
    await ui.render(entries, { appearance: defaultAppearance });
    const group = ui.container.querySelector<HTMLDetailsElement>('details.chat-activity-group[data-category="tools"]');
    const tool = ui.container.querySelector<HTMLElement>('article.entry.tool');
    const details = ui.container.querySelector<HTMLDetailsElement>("details.execution-record.tool-record");
    assert.ok(group?.open, "approval-required activity should auto-open its grouped parent");
    assert.ok(tool);
    assert.ok(details?.open, "approval-required tool details should auto-open");

    entries = appendEvent(entries, { event: "approval_closed", run_id: "run", approval_id: "approval-grouped", decision: "allow" });
    await ui.render(entries, { appearance: defaultAppearance });
    entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "approval-call", result: "Done" });
    await ui.render(entries, { appearance: defaultAppearance });
    assert.equal(ui.container.querySelector('details.chat-activity-group[data-category="tools"]'), group);
    assert.equal(ui.container.querySelector('article.entry.tool'), tool);
    assert.equal(group.open, true, "resolution must not close the auto-expanded group");
    assert.equal(details.open, true, "resolution must not close the auto-expanded tool details");

    await closeByUser(group);
    await closeByUser(details);
    assert.equal(group.open, false);
    assert.equal(details.open, false);
    entries = appendEvent(entries, { event: "tool_output", run_id: "run", id: "approval-call", text: "Late diagnostic output" });
    await ui.render(entries, { appearance: defaultAppearance });
    assert.equal(ui.container.querySelector('details.chat-activity-group[data-category="tools"]'), group);
    assert.equal(ui.container.querySelector('article.entry.tool'), tool);
    assert.equal(group.open, false, "an explicit user collapse stays closed through later activity");
    assert.equal(details.open, false, "an explicit user collapse stays closed through later activity");
  } finally { await ui.close(); }
});

test("optimistic user row stays mounted through admission, canonical history and active checkpoint", async () => {
  const ui = fixture();
  const cache = new LiveSessions();
  let cursor = 0;
  const receive = (record: WireEvent) => cache.receive({
    type: "event", session_id: "root", epoch: "one", cursor: ++cursor, record,
  });
  const prompt = "Continue the current response";
  const message = { session_id: "root", message_id: "followup", prompt };
  cache.set("root", applySnapshot({ entries: [] }, snapshot([row("earlier", "user", "Earlier", { message_id: "earlier" })]), false));
  try {
    cache.enqueue(message);
    cache.admission(message, "queued");
    await ui.render(cache.get("root").entries);
    const rowElement = [...ui.container.querySelectorAll<HTMLElement>("article.entry.user")]
      .find(item => item.textContent?.includes(prompt));
    assert.ok(rowElement);
    const key = rowElement.dataset.recordKey;

    receive({ event: "run_started", run_id: "run", message_id: message.message_id });
    receive({ event: "input_admitted", run_id: "run", session_id: "root", message_id: message.message_id, channel: "" });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), rowElement);

    receive({ event: "user_message", run_id: "run", history_id: "saved-followup", message_id: message.message_id, text: prompt });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), rowElement);

    const checkpoint = snapshot([
      row("earlier", "user", "Earlier", { message_id: "earlier" }),
      row("saved-followup", "user", prompt, { message_id: message.message_id }),
    ], { id: "run", status: "running", message_id: message.message_id, events: [
      { event: "run_started", run_id: "run", message_id: message.message_id },
      { event: "input_admitted", run_id: "run", session_id: "root", message_id: message.message_id, channel: "" },
      { event: "user_message", run_id: "run", history_id: "saved-followup", message_id: message.message_id, text: prompt },
    ] });
    cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: ++cursor, snapshot: checkpoint });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), rowElement);
    assert.equal(ui.container.querySelectorAll("article.entry.user").length, 2);
  } finally { await ui.close(); }
});

test("checkpointed live answer reconciles to durable history without moving or replacing its row", async () => {
  const ui = fixture();
  const cache = new LiveSessions();
  let cursor = 0;
  const receive = (record: WireEvent) => cache.receive({
    type: "event", session_id: "root", epoch: "one", cursor: ++cursor, record,
  });
  const prior = [row("user-before", "user", "Before", { message_id: "before" })];
  cache.set("root", applySnapshot({ entries: [] }, snapshot(prior), false));
  try {
    await ui.render(cache.get("root").entries);
    receive({ event: "run_started", run_id: "run", message_id: "current" });
    receive({ event: "user_message", run_id: "run", history_id: "user-current", message_id: "current", text: "Current" });
    receive({ event: "text_chunk", run_id: "run", chunk: "Checkpointed answer" });
    await ui.render(cache.get("root").entries);
    const answer = ui.container.querySelector<HTMLElement>('article.entry.assistant[data-run-id="run"]');
    assert.ok(answer);
    const key = answer.dataset.recordKey;

    const checkpoint = snapshot([
      ...prior,
      row("user-current", "user", "Current", { message_id: "current" }),
      row("answer", "assistant", "Checkpointed answer"),
    ], { id: "run", status: "running", message_id: "current", events: [
      { event: "run_started", run_id: "run", message_id: "current" },
      { event: "user_message", run_id: "run", history_id: "user-current", message_id: "current", text: "Current" },
      { event: "text_chunk", run_id: "run", chunk: "Checkpointed answer" },
    ] });
    cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: ++cursor, snapshot: checkpoint });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), answer);
    assert.equal(answer.textContent?.includes("Checkpointed answer"), true);

    receive({ event: "text_done", run_id: "run", text: "Checkpointed answer" });
    await ui.render(cache.get("root").entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${key}"]`), answer);
  } finally { await ui.close(); }
});


test("task conversation polling reconciles streamed tool and answer rows to canonical history", async () => {
  const ui = fixture();
  const childSession = "child-session";
  const task = {
    id: "child", name: "Gentle Brook", status: "completed" as const, result: "Found it", error: "",
    session_id: "root", prompt: "Find one result", child_session_id: childSession, parent_task_id: "",
    parent_session_id: "root", depth: 1, profile: "assistant", mode: "", followups: 0, activation: 0, trigger: "delegation" as const,
  };
  const call = { id: "find-call", name: "find", arguments: { query: "one result" } };
  const activity: TaskActivity[] = [
    { event: "task_activity", task_id: "child", activation: 0, sequence: 1, timestamp: 1, payload: { type: "tool_call", id: "find-call", name: "find", arguments: { query: "one result" } } },
    { event: "task_activity", task_id: "child", activation: 0, sequence: 2, timestamp: 2, payload: { type: "tool_result", id: "find-call", name: "find", result: { matches: ["result"] } } },
    { event: "task_activity", task_id: "child", activation: 0, sequence: 3, timestamp: 3, payload: { type: "response_metadata", response_id: "child-response", response_session_id: childSession, run_id: "child-run", revision: 1, round_number: 0, history_id: "", status: "responding", summary: "", summary_truncated: false, summary_expired: false, usage_reported: false } },
    { event: "task_activity", task_id: "child", activation: 0, sequence: 4, timestamp: 4, payload: { type: "text_done", response_id: "child-response", response_session_id: childSession, run_id: "child-run", text: "Found one result." } },
  ];
  const userRow: TaskDetail["history"][number] = { role: "user", content: "Find one result", name: "", tool_call_id: "", tool_calls: [], history_id: "task-user", message_id: "task-user-message" };
  const responseMetadata = { response_id: "child-response", response_session_id: childSession, run_id: "child-run", revision: 2, round_number: 0, history_id: "assistant-history", status: "completed", summary: "", summary_truncated: false, summary_expired: false, usage_reported: false };
  const detail = (history: TaskDetail["history"], activityRows: TaskActivity[], activityCursor: number): TaskDetail => ({
    task, history, activity: activityRows, activity_cursor: activityCursor,
  });
  const team = {
    selected: "child", loaded: true, profile: undefined, detail: detail([userRow], activity, 0), activity: [],
    loading: false, loadingEarlier: false, select() {}, loadEarlier: async () => {}, startNotice: "",
  } as unknown as TeamController;
  const render = () => ui.renderElement(createElement(TaskConversation, {
    team, agents: [], arrivals: {}, token: "", sessionId: "root", back() {},
  }));
  try {
    await render();
    const tool = [...ui.container.querySelectorAll<HTMLElement>("article.entry.tool")].find(row => row.textContent?.includes("find"));
    const answer = [...ui.container.querySelectorAll<HTMLElement>("article.entry.assistant")].find(row => row.textContent?.includes("Found one result."));
    assert.ok(tool, "live task activity should show the completed tool call");
    assert.ok(answer, "live task activity should show the assistant reply");
    const initialToolKey = tool.dataset.recordKey;
    const initialAnswerKey = answer.dataset.recordKey;

    const history: TaskDetail["history"] = [
      userRow,
      { role: "assistant", content: "", name: "", tool_call_id: "", tool_calls: [call], history_id: "tool-call-row" },
      { role: "tool", content: "{\"matches\":[\"result\"]}", name: "find", tool_call_id: "find-call", tool_calls: [], history_id: "tool-result-row" },
      { role: "assistant", content: "Found one result.", name: "", tool_call_id: "", tool_calls: [], history_id: "assistant-history", response_id: "child-response", response_metadata: responseMetadata },
    ];
    team.detail = detail(history, [], 4);
    await render();
    assert.equal(ui.container.querySelector(`[data-record-key="${initialToolKey}"]`), tool,
      "canonical saved tool output must reconcile into the already-mounted live tool row");
    assert.equal(ui.container.querySelector(`[data-record-key="${initialAnswerKey}"]`), answer,
      "canonical saved reply must reconcile into the already-mounted live answer row");

    team.detail = detail(history, [], 4);
    await render();
    assert.equal(ui.container.querySelectorAll("article.entry.tool").length, 1);
    assert.equal(ui.container.querySelectorAll("article.entry.assistant").length, 1);
    assert.equal(answer.textContent?.match(/Found one result\./g)?.length, 1,
      "polling the same saved history must not append the streamed answer again");

    const afterClear: TaskActivity[] = [
      { event: "task_activity", task_id: "child", activation: 1, sequence: 5, timestamp: 5, payload: { type: "tool_call", id: "find-next", name: "lookup", arguments: { query: "fresh" } } },
      { event: "task_activity", task_id: "child", activation: 1, sequence: 6, timestamp: 6, payload: { type: "tool_result", id: "find-next", name: "lookup", result: "Fresh result" } },
      { event: "task_activity", task_id: "child", activation: 1, sequence: 7, timestamp: 7, payload: { type: "response_metadata", response_id: "child-response-next", response_session_id: childSession, run_id: "child-run-next", revision: 1, round_number: 0, history_id: "", status: "responding", summary: "", summary_truncated: false, summary_expired: false, usage_reported: false } },
      { event: "task_activity", task_id: "child", activation: 1, sequence: 8, timestamp: 8, payload: { type: "text_chunk", response_id: "child-response-next", response_session_id: childSession, run_id: "child-run-next", chunk: "Fresh response." } },
    ];
    team.detail = detail([], afterClear, 4);
    await render();
    assert.notEqual(ui.container.querySelector(`[data-record-key="${initialToolKey}"]`), tool,
      "a cleared history must not preserve the old tool DOM node, even if a new row reuses its key");
    assert.notEqual(ui.container.querySelector(`[data-record-key="${initialAnswerKey}"]`), answer,
      "a cleared history must not preserve the old answer DOM node, even if a new row reuses its key");
    const freshTool = [...ui.container.querySelectorAll<HTMLElement>("article.entry.tool")].find(row => row.textContent?.includes("Fresh result"));
    const freshAnswer = [...ui.container.querySelectorAll<HTMLElement>("article.entry.assistant")].find(row => row.textContent?.includes("Fresh response."));
    assert.ok(freshTool, "new tool activity delivered alongside history clear must remain visible");
    assert.ok(freshAnswer, "new response delivered alongside history clear must remain visible");
    team.detail = detail([], afterClear, 4);
    await render();
    assert.equal(ui.container.querySelector(`[data-record-key="${freshTool.dataset.recordKey}"]`), freshTool);
    assert.equal(ui.container.querySelector(`[data-record-key="${freshAnswer.dataset.recordKey}"]`), freshAnswer);
    assert.equal(ui.container.querySelectorAll("article.entry.tool").length, 1);
    assert.equal(ui.container.querySelectorAll("article.entry.assistant").length, 1);
  } finally { await ui.close(); }
});
