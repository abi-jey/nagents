import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { Snapshot, WireEvent } from "../../types.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { Conversation } from "./Conversation.js";
import { commandOutcomeDescription, executionStatus } from "./executionPresentation.js";
import { applySnapshot, LiveSessions } from "./liveTranscript.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";

function resultEntry(result: unknown, extra: Partial<WireEvent> = {}): Entry {
  return appendEvent([], { event: "tool_result", name: "shell", id: "command", result, error: null, ...extra })[0];
}

for (const [result, label, description] of [
  [{ output: "check did not match", exit_code: 7, timed_out: false }, "Exit 7", "Command exited with code 7"],
  [{ output: "partial output", exit_code: -15, timed_out: true }, "Timed out", "Command timed out (exit code -15)"],
  [{ output: "partial output", timed_out: true }, "Timed out", "Command timed out"],
] as const) test(`structured native shell result renders ${label} without inventing a framework error`, () => {
  const entry = resultEntry(result);
  assert.equal(entry.state, "Completed", "The tool returned; its process outcome is distinct from the framework lifecycle");
  assert.equal(entry.error, "");
  assert.deepEqual(JSON.parse(entry.result!), result, "Original structured output remains intact");
  assert.deepEqual(executionStatus(entry), { label, tone: "error" });
  assert.equal(commandOutcomeDescription(entry), description);
  const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry, open: true })));
  try {
    assert.equal(dom.window.document.querySelector(".tool-record")?.getAttribute("data-tone"), "error");
    assert.equal(dom.window.document.querySelector(".execution-state")?.textContent, label);
    assert.equal(dom.window.document.querySelector(".execution-state")?.getAttribute("title"), description);
    assert.ok(dom.window.document.querySelector('[data-panel="metadata"]')?.textContent?.includes(description));
    assert.equal(dom.window.document.querySelector(".tool-error"), null);
  } finally { dom.window.close(); }
});

test("success, framework failures, cancellation, unrelated tools and task status keep their own meanings", () => {
  const success = resultEntry({ exit_code: 0, timed_out: false });
  assert.deepEqual(executionStatus(success), { label: "Completed", tone: "complete" });
  assert.equal(commandOutcomeDescription(success), "Command exited with code 0");
  const failed = resultEntry({ exit_code: 7, timed_out: true }, { error: "Tool transport failed" });
  assert.equal(failed.error, "Tool transport failed");
  assert.deepEqual(executionStatus(failed), { label: "Error", tone: "error" });
  assert.equal(commandOutcomeDescription(failed), "");
  for (const name of ["delegate", "read_file", "custom_tool"]) {
    const entry = resultEntry({ exit_code: 7, timed_out: true }, { name });
    assert.equal(executionStatus(entry).tone, "complete");
    assert.equal(commandOutcomeDescription(entry), "");
  }
  assert.deepEqual(executionStatus({ ...success, state: "Cancelled" }), { label: "Cancelled", tone: "pending" });
  assert.deepEqual(executionStatus({ ...failed, kind: "task", error: "", state: "Running" }), { label: "Running", tone: "active" });
});

test("live command outcome announcements agree with the badge instead of announcing successful completion", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  let entries: Entry[] = [];
  const render = () => act(async () => root.render(createElement(Conversation, {
    entries, sessionId: "root", demo: false, canSubmit: false, submit: () => assert.fail("No submit expected"),
  })));
  try {
    await render();
    entries = appendEvent(entries, { event: "tool_result", name: "shell", id: "first", result: { exit_code: 7, timed_out: false } });
    await render();
    assert.equal(container.querySelector('.activity-announcement .sr-only')?.textContent, "Command exited with code 7.");
    entries = appendEvent(entries, { event: "tool_result", name: "shell", id: "second", result: { timed_out: true } });
    await render();
    assert.equal(container.querySelector('.activity-announcement .sr-only')?.textContent, "Command timed out.");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("command outcome requires typed raw fields and never evaluates saved or live result strings", () => {
  for (const result of [
    "{'exit_code': 7, 'timed_out': True}", '{"exit_code":7,"timed_out":true}',
    { output: "exit_code=7 timed_out=true" }, { exit_code: "7", timed_out: "true" },
    { exit_code: false }, { exit_code: 0.5 }, { exit_code: Infinity }, { exit_code: NaN },
    { exit_code: Number.MAX_SAFE_INTEGER + 1 }, [{ exit_code: 7, timed_out: true }], null,
  ]) {
    const entry = resultEntry(result);
    assert.equal(entry.commandOutcome, undefined);
    assert.equal(executionStatus(entry).label, "Completed");
  }
  const saved = resultEntry({ exit_code: 7, timed_out: true }, { saved: true });
  assert.equal(saved.commandOutcome, undefined);
  assert.deepEqual(executionStatus(saved), { label: "Saved result", tone: "neutral" });
});

test("large result previews and out-of-order calls retain outcome evidence without duplicate activity", () => {
  const result = { output: "evidence\n".repeat(100_000), exit_code: 9, timed_out: false, truncated: true };
  const event: WireEvent = { event: "tool_result", run_id: "run", call_id: "call", result, error: null };
  let entries = appendEvent([], event);
  assert.deepEqual(entries[0].commandOutcome, { kind: "exited", exitCode: 9 });
  assert.equal(appendEvent(entries, event), entries, "Duplicate result replay is inert, including its activity counter");
  entries = appendEvent(entries, { event: "tool_call", run_id: "run", call_id: "call", name: "shell", arguments: { command: "check" } });
  assert.equal(entries.length, 1);
  assert.equal(executionStatus(entries[0]).label, "Exit 9");
  assert.equal(JSON.parse(entries[0].result!).output, result.output);
});

test("identical tool IDs across runs and tasks keep independent command outcomes without changing the run", () => {
  const cache = new LiveSessions();
  let cursor = 0;
  const send = (record: WireEvent) => cache.receive({ type: "event", session_id: "root", epoch: "process", cursor: ++cursor, record });
  send({ event: "run_started", run_id: "run-one" });
  send({ event: "tool_result", run_id: "run-one", call_id: "same", name: "shell", result: { exit_code: 7, timed_out: false } });
  assert.equal(cache.get("root").activeRun?.status, "running");
  send({ event: "task_started", run_id: "run-one", task_id: "child", name: "Worker", activation: 1 });
  send({ event: "tool_result", run_id: "run-one", task_id: "child", activation: 1, call_id: "same", name: "shell", result: { exit_code: 0, timed_out: false } });
  send({ event: "tool_result", run_id: "run-two", call_id: "same", name: "shell", result: { exit_code: -15, timed_out: true } });
  const entries = cache.get("root").entries;
  assert.deepEqual(entries.filter(entry => entry.kind === "tool").map(entry => executionStatus(entry).label), ["Exit 7", "Completed", "Timed out"]);
  assert.equal(entries.find(entry => entry.kind === "task")?.state, "Running");
  assert.equal(cache.get("root").activeRun?.id, "run-one");
});

test("matched replay and warm history retain observed outcomes while cold saved strings and reused IDs stay neutral", () => {
  type Row = Snapshot["history"][number];
  const row = (history_id: string, role: string, content: string, extra: Partial<Row> = {}): Row => ({
    history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra,
  });
  const history: Row[] = [
    row("u1", "user", "Check the command"),
    row("c1", "assistant", "", { tool_calls: [{ id: "reused", name: "shell", arguments: { command: "check" } }] }),
    row("r1", "tool", "{'output': 'check', 'exit_code': 7, 'timed_out': False}", { name: "shell", tool_call_id: "reused" }),
  ];
  const snapshot: Snapshot = { session_id: "root", sessions: [], retained_tasks: [], history, active_run: {
    id: "run", status: "running", events: [
      { event: "user_message", history_id: "u1", text: "Check the command" },
      { event: "tool_call", history_id: "c1", call_id: "reused", name: "shell", arguments: { command: "check" } },
      { event: "tool_result", history_id: "r1", call_id: "reused", name: "shell", result: { output: "check", exit_code: 7, timed_out: false } },
    ],
  } };
  let state = applySnapshot({ entries: [] }, snapshot);
  const id = state.entries.find(entry => entry.kind === "tool")!.id;
  state = applySnapshot(state, snapshot);
  assert.equal(state.entries.find(entry => entry.kind === "tool")!.id, id);
  assert.equal(executionStatus(state.entries.find(entry => entry.kind === "tool")!).label, "Exit 7");
  const settled = { ...snapshot, active_run: null, history: [...history,
    row("u2", "user", "Later check"),
    row("c2", "assistant", "", { tool_calls: [{ id: "reused", name: "shell", arguments: { command: "later" } }] }),
    row("r2", "tool", "{'exit_code': 0, 'timed_out': False}", { name: "shell", tool_call_id: "reused" }),
  ] };
  state = applySnapshot(state, settled);
  assert.deepEqual(state.entries.filter(entry => entry.kind === "tool").map(entry => executionStatus(entry).label), ["Exit 7", "Saved result"]);
  assert.ok(fromHistory(settled).filter(entry => entry.kind === "tool").every(entry => !entry.commandOutcome && executionStatus(entry).tone === "neutral"));
});
