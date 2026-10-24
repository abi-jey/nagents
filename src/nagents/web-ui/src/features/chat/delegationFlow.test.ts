import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { defaultAppearance } from "../settings/appearance.js";
import { taskAgent, type TaskActivity, type TeamAgent } from "../team/model.js";
import type { RetainedTask, Snapshot } from "../../types.js";
import { Conversation } from "./Conversation.js";
import { DelegationFlow, delegationPreview } from "./delegationFlow.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";

const runId = "root-run";
const prompt = "Check the session read and close paths.";

function agent(id: string, parentId = "", phase: TeamAgent["phase"] = "running"): TeamAgent {
  return {
    id, name: id === "child" ? "gentle willow" : "bright reed", profile: id === "child" ? "mini" : "verifier",
    parentId, prompt, phase, reason: "", provider: "", providerKind: "", model: "", startedAt: 1, updatedAt: 2,
    canMessage: true, messageReason: "",
  };
}

function options(agents: TeamAgent[], activity: TaskActivity[] = [], parentId = "") {
  return { agents, activity, parentId, inspect: (_id: string) => undefined };
}

function delegateCall(entries: Entry[], id = "delegate-call", taskId = ""): Entry[] {
  return appendEvent(entries, {
    event: "tool_call", run_id: runId, ...(taskId ? { task_id: taskId } : {}), id, name: "delegate",
    arguments: { agent: "mini", prompt },
  });
}

function delegateResult(entries: Entry[], taskId = "child", id = "delegate-call", parentTaskId = ""): Entry[] {
  return appendEvent(entries, {
    event: "tool_result", run_id: runId, ...(parentTaskId ? { task_id: parentTaskId } : {}), id, name: "delegate",
    result: { task_id: taskId, name: taskId === "child" ? "gentle willow" : "bright reed", status: "running" },
  });
}

function taskStarted(entries: Entry[], taskId = "child", parentTaskId = ""): Entry[] {
  return appendEvent(entries, {
    event: "task_started", run_id: runId, task_id: taskId, parent_task_id: parentTaskId,
    activation: 0, name: taskId === "child" ? "gentle willow" : "bright reed", prompt,
  });
}

function taskCompleted(entries: Entry[], taskId = "child", parentTaskId = ""): Entry[] {
  return appendEvent(entries, {
    event: "task_completed", run_id: runId, task_id: taskId, parent_task_id: parentTaskId,
    activation: 0, name: taskId === "child" ? "gentle willow" : "bright reed",
    status: "completed", result: "The focused checks passed.",
  });
}

function activity(taskId: string, sequence: number, payload: TaskActivity["payload"], activation = 0): TaskActivity {
  return { event: "task_activity", task_id: taskId, activation, sequence, timestamp: sequence, payload };
}

function delegationRows(entries: Entry[]): Entry[] {
  return entries.filter(entry => entry.delegation !== undefined);
}

function savedContext(taskId: string): Entry {
  const payload = { tasks: [{ task_id: taskId, activation: 0, followup: 0, trigger: "delegation" }], human_messages: [] };
  return { id: "saved-context", kind: "context", text: `BACKGROUND TASK NOTIFICATION: saved task evidence.\n${JSON.stringify(payload)}` };
}

function browserFixture() {
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
  const render = (entries: Entry[], agents: TeamAgent[] = [agent("child")]) => act(async () => root.render(createElement(Conversation, {
    entries, sessionId: "root", demo: false, canSubmit: false, submit() {}, appearance: defaultAppearance,
    delegation: options(agents),
  })));
  return {
    container,
    render,
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "requestAnimationFrame", "cancelAnimationFrame"] as const) {
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
        else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

function historyTask(id: string, name: string, profile: string, result: string): RetainedTask {
  return {
    id, name, status: "completed", result, error: "", session_id: "root", prompt: `Work for ${name}`,
    child_session_id: `session-${id}`, parent_task_id: "", parent_session_id: "root", depth: 1,
    profile, mode: "build", followups: 0, activation: 0, trigger: "delegation",
  };
}

function savedDelegationHistory(): Snapshot["history"] {
  return [
    { role: "user", content: "Check two things.", name: "", tool_call_id: "", tool_calls: [] },
    { role: "assistant", content: "I will delegate both checks.", name: "", tool_call_id: "", tool_calls: [
      { id: "call-willow", name: "delegate", arguments: { agent: "mini", prompt: "Check the session reader." } },
      { id: "call-reed", name: "delegate", arguments: { agent: "verifier", prompt: "Check the session writer." } },
    ] },
    { role: "tool", content: JSON.stringify({ task_id: "task-willow", name: "gentle willow", status: "running" }),
      name: "delegate", tool_call_id: "call-willow", tool_calls: [] },
    { role: "tool", content: JSON.stringify({ task_id: "task-reed", name: "bright reed", status: "running" }),
      name: "delegate", tool_call_id: "call-reed", tool_calls: [] },
  ];
}

test("cold history joins string delegate acknowledgements to completed retained tasks without synthetic notices", () => {
  const tasks = [
    historyTask("task-willow", "gentle willow", "mini", "The session reader passed."),
    historyTask("task-reed", "bright reed", "verifier", "The session writer passed."),
  ];
  const entries = fromHistory({ session_id: "root", history: savedDelegationHistory(), retained_tasks: tasks });
  const projected = new DelegationFlow().project(entries, options(tasks.map(taskAgent)));
  const rows = delegationRows(projected);
  const starts = rows.filter(entry => entry.delegation?.kind === "start");
  const results = rows.filter(entry => entry.delegation?.kind === "result");
  assert.equal(rows.length, 4);
  assert.equal(starts.length, 2);
  assert.equal(results.length, 2);
  assert.deepEqual(results.map(entry => [entry.delegation?.name, entry.delegation?.profile, entry.delegation?.text]), [
    ["gentle willow", "mini", "The session reader passed."],
    ["bright reed", "verifier", "The session writer passed."],
  ]);
  assert.equal(projected.some(entry => entry.kind === "notification"), false);
  assert.equal(projected.some(entry => entry.kind === "context"), false);
});

test("a cold retained-task update enriches the existing delegate-call article", async () => {
  const ui = browserFixture();
  const history = savedDelegationHistory().slice(0, 3);
  const cold = fromHistory({ session_id: "root", history, retained_tasks: [] });
  const coldCall = cold.find(entry => entry.kind === "tool" && entry.title === "delegate")!;
  const retained = historyTask("task-willow", "gentle willow", "mini", "The session reader passed.");
  try {
    await ui.render(cold, []);
    const article = ui.container.querySelector<HTMLElement>(`article[data-record-key="${coldCall.id}"]`);
    assert.ok(article, "the saved delegate call is visible before retained task data is loaded");

    const restored = fromHistory({ session_id: "root", history, retained_tasks: [retained] });
    await ui.render(restored, [taskAgent(retained)]);
    assert.equal(ui.container.querySelector(`article[data-record-key="${coldCall.id}"]`), article);
    assert.equal(article.querySelector(".delegation-activity")?.getAttribute("data-phase"), "done");
    assert.match(article.textContent || "", /gentle willow/);
    assert.match(article.closest(".conversation")?.textContent || "", /The session reader passed\./);
  } finally { await ui.close(); }
});

test("the known delegate call owns one stable start row before acknowledgement and after lifecycle evidence arrives", () => {
  const flow = new DelegationFlow();
  let entries: Entry[] = [{ id: "request", kind: "user", text: "Check the session paths." }];
  entries = delegateCall(entries);
  const callId = entries[1].id;
  let projected = flow.project(entries, options([agent("child")]));
  let rows = delegationRows(projected);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, callId);
  assert.equal(rows[0].delegation?.kind, "start");
  assert.equal(rows[0].delegation?.taskId, "");

  entries = delegateResult(entries);
  entries = taskStarted(entries);
  projected = flow.project(entries, options([agent("child")]));
  rows = delegationRows(projected);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, callId);
  assert.equal(rows[0].delegation?.taskId, "child");
  assert.equal(rows[0].delegation?.name, "gentle willow");
  assert.equal(rows[0].delegation?.profile, "mini");
  assert.equal(rows[0].delegation?.sources.length, 2);
});

test("direct-child reasoning, tool arguments, and output update one line without adopting nested activity", () => {
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  const callId = entries[0].id;
  const flow = new DelegationFlow();

  const reasoning = [activity("child", 1, { type: "text_done", text: "Tracing the session read path." })];
  let row = delegationRows(flow.project(entries, options([agent("child"), agent("nested", "child")], reasoning)))[0];
  assert.equal(row.id, callId);
  assert.equal(delegationPreview(row.delegation!).text, "Tracing the session read path.");

  const tool = [...reasoning, activity("child", 2, {
    type: "tool_call", id: "read-call", name: "read_file", arguments: { path: "src/session.py" },
  })];
  row = delegationRows(flow.project(entries, options([agent("child"), agent("nested", "child")], tool)))[0];
  assert.equal(delegationPreview(row.delegation!).tool, "read_file");
  assert.ok(delegationPreview(row.delegation!).text.includes("src/session.py"));

  const output = [...tool,
    activity("child", 3, { type: "tool_result", id: "read-call", name: "read_file", result: "Read completed." }),
    activity("child", 4, { type: "text_done", text: "The read finishes before connection closure." }),
    activity("nested", 1, { type: "text_done", text: "Nested verifier private activity." }),
  ];
  row = delegationRows(flow.project(entries, options([agent("child"), agent("nested", "child")], output)))[0];
  assert.equal(delegationPreview(row.delegation!).text, "The read finishes before connection closure.");
  assert.doesNotMatch(delegationPreview(row.delegation!).text, /Nested verifier/);
});

test("completion is appended at task_completed after intervening main and user messages", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = appendEvent(entries, { event: "user_message", run_id: runId, history_id: "h2", message_id: "u2", text: "Any progress?" });
  entries = appendEvent(entries, { event: "text_done", run_id: runId, text: "The child is checking the read path." });
  const answerId = entries.at(-1)!.id;
  entries = appendEvent(entries, { event: "user_message", run_id: runId, history_id: "h3", message_id: "u3", text: "Thanks, continue." });
  const followUpId = entries.at(-1)!.id;
  entries = taskCompleted(entries);

  const projected = flow.project(entries, options([agent("child", "", "done")]));
  const rows = delegationRows(projected);
  assert.deepEqual(rows.map(entry => entry.delegation?.kind), ["start", "result"]);
  assert.ok(projected.findIndex(entry => entry.id === rows[1].id) > projected.findIndex(entry => entry.id === answerId));
  assert.ok(projected.findIndex(entry => entry.id === rows[1].id) > projected.findIndex(entry => entry.id === followUpId));
  assert.equal(rows[1].delegation?.text, "The focused checks passed.");
});

test("initial completion context and delivery fold into two stable delegation rows", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = taskCompleted(entries);
  entries.push(savedContext("child"));
  entries = appendEvent(entries, {
    event: "task_notification", run_id: runId, notification_id: "completion-notice", source_task_id: "child",
    recipient_task_id: "", cause: "completion", text: "The focused checks passed.",
  });
  assert.equal(entries.length, 4);

  const projected = flow.project(entries, options([agent("child", "", "done")]));
  const rows = delegationRows(projected);
  assert.equal(projected.length, 2);
  assert.deepEqual(rows.map(entry => entry.delegation?.kind), ["start", "result"]);
  assert.equal(rows[0].delegation?.sources.length, 4);

  entries = taskCompleted(entries);
  entries = appendEvent(entries, {
    event: "task_notification", run_id: runId, notification_id: "completion-notice", source_task_id: "child",
    recipient_task_id: "", cause: "completion", text: "The focused checks passed.",
  });
  const replayed = flow.project(entries, options([agent("child", "", "done")]));
  assert.deepEqual(delegationRows(replayed).map(entry => entry.id), rows.map(entry => entry.id));
  assert.equal(replayed.length, 2);
});

test("nested work stays out of the root chat and appears in the selected parent's conversation", () => {
  const agents = [agent("child"), agent("nested", "child")];
  let entries = delegateCall([]);
  entries = delegateResult(entries, "child");
  entries = taskStarted(entries, "child");
  entries = delegateCall(entries, "nested-delegate", "child");
  entries = delegateResult(entries, "nested", "nested-delegate", "child");
  entries = taskStarted(entries, "nested", "child");
  const nestedActivity = [activity("nested", 1, { type: "text_done", text: "Nested verifier result." })];
  const root = new DelegationFlow().project(entries, options(agents, nestedActivity));
  assert.deepEqual(delegationRows(root).map(entry => entry.delegation?.taskId), ["child"]);
  assert.doesNotMatch(JSON.stringify(root), /Nested verifier result/);

  const childConversation = entries.filter(entry => entry.taskId === "child" || entry.taskId === "nested");
  const child = new DelegationFlow().project(childConversation, options(agents, nestedActivity, "child"));
  const childRows = delegationRows(child);
  assert.deepEqual(childRows.map(entry => entry.delegation?.taskId), ["nested"]);
  assert.equal(delegationPreview(childRows[0].delegation!).text, "Nested verifier result.");
});

test("genuine later wake-up and information notifications remain after the completion", () => {
  const flow = new DelegationFlow();
  let entries = delegateCall([]);
  entries = delegateResult(entries);
  entries = taskStarted(entries);
  entries = taskCompleted(entries);
  entries = appendEvent(entries, {
    event: "task_notification", run_id: runId, notification_id: "wake-notice", source_task_id: "child",
    recipient_task_id: "", cause: "wakeup", text: "A scheduled check woke up.",
  });
  entries = appendEvent(entries, {
    event: "task_notification", run_id: runId, notification_id: "info-notice", source_task_id: "child",
    recipient_task_id: "", cause: "additional_information", text: "The follow-up found no issues.",
  });

  const projected = flow.project(entries, options([agent("child", "", "done")]));
  assert.deepEqual(projected.filter(entry => entry.kind === "notification").map(entry => [entry.cause, entry.text]), [
    ["wakeup", "A scheduled check woke up."],
    ["additional_information", "The follow-up found no issues."],
  ]);
  assert.deepEqual(delegationRows(projected).map(entry => entry.delegation?.kind), ["start", "result"]);
});

test("errors and pending approvals remain visible while delegate calls are projected", () => {
  let entries = delegateCall([]);
  entries = appendEvent(entries, {
    event: "approval", run_id: runId, call_id: "delegate-call", tool: "delegate", approval_id: "approval-1",
    arguments: { agent: "mini", prompt },
  });
  entries = appendEvent(entries, { event: "error", run_id: runId, message: "The parent run also encountered an error." });
  const projected = new DelegationFlow().project(entries, options([agent("child")]));
  const row = delegationRows(projected)[0];
  assert.equal(row.approvalId, "approval-1");
  assert.equal(row.state, "Waiting for approval");
  assert.ok(projected.some(entry => entry.kind === "error" && entry.text === "The parent run also encountered an error."));
});

test("a streamed tool becoming a delegate keeps its article mounted and leaves one visible line", async () => {
  const ui = browserFixture();
  let entries: Entry[] = [];
  entries = appendEvent(entries, {
    event: "tool_call_progress", run_id: runId, generation_id: "delegate-generation", index: 0,
    id: "delegate-call", name: "unresolved tool", status: "streaming",
    arguments_text: JSON.stringify({ agent: "mini", prompt }),
  });
  const sourceId = entries[0].id;
  try {
    await ui.render(entries);
    const article = ui.container.querySelector<HTMLElement>(`article[data-record-key="${sourceId}"]`);
    assert.ok(article, "the streamed tool line is visible before its canonical name arrives");
    assert.equal(article.querySelectorAll(".delegation-line").length, 0);

    entries = appendEvent(entries, {
      event: "tool_call", run_id: runId, generation_id: "delegate-generation", index: 0,
      id: "delegate-call", name: "delegate", arguments: { agent: "mini", prompt },
    });
    await ui.render(entries);
    const updated = ui.container.querySelector<HTMLElement>(`article[data-record-key="${sourceId}"]`);
    assert.equal(updated, article, "canonical delegation metadata must not remount the visible source row");
    assert.equal(ui.container.querySelectorAll("article.delegation-entry").length, 1);
    assert.equal(article.querySelectorAll(".delegation-line").length, 1);
  } finally { await ui.close(); }
});
