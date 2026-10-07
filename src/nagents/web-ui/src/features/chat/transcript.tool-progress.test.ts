import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { Snapshot, WireEvent } from "../../types.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { executionStatus } from "./executionPresentation.js";
import { applyFrame, applySnapshot, pendingApprovals, type LiveTranscript } from "./liveTranscript.js";
import { appendEvent, type Entry } from "./transcript.js";

const progress = (extra: Partial<WireEvent> = {}): WireEvent => ({
  event: "tool_call_progress", run_id: "run", generation_id: "generation", index: 0,
  id: "", name: "shell", arguments_text: "", status: "streaming", ...extra,
});
const canonical = (extra: Partial<WireEvent> = {}): WireEvent => ({
  event: "tool_call", run_id: "run", id: "call", name: "shell", arguments: { command: "check" },
  extra: { generation_id: "generation", index: 0 }, ...extra,
});
const snapshot = (events?: WireEvent[], records?: WireEvent[]): Snapshot => ({
  session_id: "root", sessions: [], history: [], retained_tasks: [],
  active_run: { id: "run", status: "running", events, records },
});

test("a recoverable child delivery warning preserves the root's Preparing tool preview", () => {
  let entries = appendEvent([], progress({ arguments_text: '{"command":"root' }));
  const root = entries[0];
  entries = appendEvent(entries, progress({ task_id: "child", activation: 1, followup: 2, arguments_text: "child draft" }));
  entries = appendEvent(entries, {
    event: "error", run_id: "run", code: "TASK_DELIVERY_SKIPPED", recoverable: true,
    task_id: "child", activation: 1, followup: 2,
    message: "The immediate parent stopped; its descendant result was not forwarded to Main.",
  });
  const remaining = entries.find(entry => entry.id === root.id);
  assert.equal(remaining, root);
  assert.equal(remaining?.state, "Preparing");
  assert.equal(remaining?.toolProgress, "streaming");
  assert.equal(remaining?.inputs, '{"command":"root');
  assert.equal(entries.find(entry => entry.kind === "tool" && entry.taskId === "child")?.state, "Interrupted");
  assert.equal(entries.find(entry => entry.kind === "error")?.taskId, "child");
});

test("parallel proposed calls appear before complete JSON and late provider IDs retain their cards", () => {
  let entries = appendEvent([], progress({ arguments_text: '{"command":"' }));
  const first = entries[0].id;
  entries = appendEvent(entries, progress({ index: 1, name: "read_file", arguments_text: '{"path":' }));
  const second = entries[1].id;
  entries = appendEvent(entries, progress({ id: "call", arguments_text: '{"command":"check' }));
  assert.equal(entries.length, 2);
  assert.deepEqual(entries.map(entry => entry.id), [first, second]);
  assert.equal(entries[0].callId, "call");
  assert.equal(entries[0].inputs, '{"command":"check');
  assert.equal(entries[0].state, "Preparing");
  assert.equal(entries[0].started, undefined);
  assert.equal(entries[0].result, undefined);
  entries = appendEvent(entries, progress({ status: "ready", id: "call", arguments_text: '{"command":"check"}' }));
  assert.equal(entries[0].state, "Preparing", "Validated arguments are not evidence of execution");
  entries = appendEvent(entries, canonical());
  assert.equal(entries[0].id, first);
  assert.equal(entries[0].state, "Requested");
  assert.equal(entries[0].toolProgress, undefined);
  assert.equal(entries[0].inputs, JSON.stringify({ command: "check" }, null, 2));
  assert.equal(entries[1].state, "Preparing");
  assert.equal(appendEvent(entries, progress({ arguments_text: "late preview" })), entries);
});

test("call promotion, invocation, approval, output and result preserve identity without premature running", () => {
  let entries = appendEvent([], progress({ id: "call" }));
  const id = entries[0].id;
  const send = (event: WireEvent) => { entries = appendEvent(entries, { run_id: "run", call_id: "call", ...event }); };
  send(canonical());
  assert.equal(entries[0].state, "Requested");
  send({ event: "tool_execution_started", name: "shell" });
  assert.equal(entries[0].state, "Starting");
  send({ event: "approval", approval_id: "approval", tool: "shell" });
  assert.equal(executionStatus(entries[0]).tone, "pending");
  send({ event: "approval_closed", approval_id: "approval", decision: "allow" });
  send({ event: "tool_output", text: "first\n" });
  send({ event: "tool_output", text: "second\n" });
  assert.equal(entries[0].state, "Receiving output");
  send({ event: "tool_result", result: { exit_code: 0 }, duration_ms: 20 });
  send(canonical());
  send({ event: "tool_execution_started", name: "shell" });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].id, id);
  assert.equal(entries[0].state, "Completed");
  assert.equal(entries[0].text, "first\nsecond\n");
  assert.equal(entries[0].approval, "Allowed once");
});

test("direct approval before queued previews or calls keeps one exact card and never rolls back the decision", () => {
  for (const before of ["preview", "ready", "call", "decision"]) {
    let entries: Entry[] = [];
    const request = { event: "approval", run_id: "run", generation_id: "generation", index: 0, id: "call",
      tool: "shell", approval_id: "nonce", arguments: { command: "check", timeout: 30 } };
    const send = (event: WireEvent) => { entries = appendEvent(entries, event); };
    if (before !== "preview") send(progress());
    const first = entries[0]?.id;
    if (before === "call") send(progress({ id: "call", status: "ready" }));
    send(request);
    const id = entries[0].id;
    if (first) assert.equal(id, first);
    assert.equal(entries.length, 1);
    assert.equal(entries[0].state, "Waiting for approval");
    if (before === "decision") send({ event: "approval_closed", run_id: "run", approval_id: "nonce", decision: "allow_tool" });
    for (const event of [progress(), progress({ id: "call", status: "ready", arguments_text: "queued preview" }), canonical(),
      { event: "tool_execution_started", run_id: "run", id: "call", extra: { generation_id: "generation", index: 0 } }]) send(event);
    assert.equal(entries.length, 1);
    assert.equal(entries[0].id, id);
    assert.equal(entries[0].approvalId, "nonce");
    assert.equal(entries[0].state, before === "decision" ? "Awaiting execution result" : "Waiting for approval");
    assert.equal(entries[0].inputs, JSON.stringify(request.arguments, null, 2), "Late model arguments cannot overwrite the executor's approved inputs");
    if (before === "decision") assert.equal(entries[0].approval, "Always allowed in workspace");
    else send({ event: "approval_closed", run_id: "run", approval_id: "nonce", decision: "allow" });
    send({ event: "tool_output", run_id: "run", call_id: "call", text: "running" });
    send({ event: "tool_result", run_id: "run", id: "call", result: "done" });
    assert.equal(entries.length, 1);
    assert.equal(entries[0].state, "Completed");
    assert.equal(entries[0].approvalId, "nonce");
  }
});

test("early approval binds only its generation, task, activation and followup when IDs are reused", () => {
  let entries = appendEvent([], progress({ id: "call" }));
  entries = appendEvent(entries, progress({ id: "call", status: "abandoned" }));
  entries = appendEvent(entries, progress({ generation_id: "retry", extra: { task_id: "child", activation: 2, followup: 1 } }));
  entries = appendEvent(entries, progress({ generation_id: "retry", extra: { task_id: "child", activation: 2, followup: 2 } }));
  entries = appendEvent(entries, progress({ generation_id: "retry", extra: { task_id: "other", activation: 2, followup: 1 } }));
  const target = entries[2].id;
  entries = appendEvent(entries, { event: "approval", run_id: "run", task_id: "child", activation: 2, followup: 2,
    generation_id: "retry", index: 0, id: "call", tool: "shell", approval_id: "current" });
  entries = appendEvent(entries, canonical({ extra: { generation_id: "retry", index: 0, task_id: "child", activation: 2, followup: 2 } }));
  assert.equal(entries.length, 4);
  assert.deepEqual(entries.map(entry => entry.state), ["Not run", "Preparing", "Waiting for approval", "Preparing"]);
  assert.equal(entries[2].id, target);
  assert.equal(entries.filter(entry => entry.approvalId === "current").length, 1);
  assert.equal(entries[0].approvalId, undefined);
});

test("saved automatic approval can precede every queued provider event without duplicate cards", () => {
  let entries = appendEvent([], { event: "notice", policy: "workspace_tool_allow", run_id: "run",
    generation_id: "generation", index: 0, call_id: "call", tool: "shell", text: "Allowed by saved policy" });
  const id = entries.find(entry => entry.kind === "tool")!.id;
  entries = appendEvent(entries, progress());
  entries = appendEvent(entries, canonical());
  const calls = entries.filter(entry => entry.kind === "tool");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].id, id);
  assert.equal(calls[0].approval, "Always allowed in workspace");
  assert.equal(calls[0].state, "Awaiting execution result");
  assert.equal(calls[0].inputs, JSON.stringify({ command: "check" }, null, 2));
});

test("legacy early approvals and saved grants cannot overwrite a completed call reusing its ID", () => {
  for (const automatic of [false, true]) {
    let entries = appendEvent([], canonical({ extra: {} }));
    entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "call", result: "old result" });
    const old = entries[0];
    entries = appendEvent(entries, automatic
      ? { event: "notice", policy: "workspace_tool_allow", run_id: "run", call_id: "call", tool: "shell", text: "Saved policy" }
      : { event: "approval", run_id: "run", id: "call", tool: "shell", approval_id: "new", arguments: { command: "new command" } });
    entries = appendEvent(entries, canonical({ extra: {}, arguments: { command: "new command" } }));
    const calls = entries.filter(entry => entry.kind === "tool");
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[0], old);
    assert.equal(calls[1].state, automatic ? "Awaiting execution result" : "Waiting for approval");
    assert.equal(calls[1].inputs, JSON.stringify({ command: "new command" }, null, 2));
  }
});

test("a later direct approval cannot steal an earlier generation's queued output or result", () => {
  let entries = appendEvent([], canonical());
  entries = appendEvent(entries, { event: "approval", run_id: "run", generation_id: "generation", index: 0,
    id: "call", tool: "shell", approval_id: "old" });
  entries = appendEvent(entries, { event: "approval_closed", run_id: "run", approval_id: "old", decision: "allow" });
  entries = appendEvent(entries, { event: "approval", run_id: "run", generation_id: "new-generation", index: 0,
    id: "call", tool: "shell", approval_id: "new" });
  entries = appendEvent(entries, { event: "tool_output", run_id: "run", call_id: "call", text: "old output", extra: { generation_id: "generation", index: 0 } });
  entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "call", result: "old result", extra: { generation_id: "generation", index: 0 } });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].result, "old result");
  assert.equal(entries[0].text, "old output");
  assert.equal(entries[1].result, undefined);
  assert.equal(entries[1].state, "Waiting for approval");
  assert.equal(entries[1].approvalId, "new");
  entries = appendEvent(entries, { event: "tool_output", run_id: "run", call_id: "call", text: " late old tail",
    extra: { generation_id: "generation", index: 0 } });
  assert.equal(entries[0].text, "old output late old tail");
  assert.equal(entries[0].state, "Completed");
  assert.equal(entries[1].text, "");
  entries = appendEvent(entries, canonical({ extra: { generation_id: "new-generation", index: 0 } }));
  entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "call", result: "new result",
    extra: { generation_id: "new-generation", index: 0 } });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].result, "old result");
  assert.equal(entries[1].result, "new result");
});

test("ambiguous legacy output stays inspectable without completing either reused-ID call", () => {
  let entries = appendEvent([], canonical());
  entries = appendEvent(entries, { event: "approval", run_id: "run", generation_id: "new-generation", index: 0,
    id: "call", tool: "shell", approval_id: "new" });
  entries = appendEvent(entries, { event: "tool_output", run_id: "run", call_id: "call", text: "unbound output" });
  entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "call", result: "unbound result" });
  assert.equal(entries.length, 3);
  assert.equal(entries[0].result, undefined);
  assert.equal(entries[1].result, undefined);
  assert.equal(entries[1].state, "Waiting for approval");
  assert.equal(entries[2].unattributed, true);
  assert.equal(entries[2].state, "Unattributed result");
  assert.equal(entries[2].text, "unbound output");
  assert.equal(entries[2].result, "unbound result");
  assert.equal(executionStatus(entries[2]).tone, "neutral");
});

test("cold replay preserves an early pending approval and its exact card through later call hydration", () => {
  const approval: WireEvent = { event: "approval", run_id: "run", generation_id: "generation", index: 0,
    id: "call", tool: "shell", approval_id: "nonce", arguments: { command: "check", timeout: 30 } };
  const events = [progress(), approval, progress({ status: "ready", id: "call" }), canonical()];
  let current = applySnapshot({ entries: [] }, snapshot(events));
  const id = current.entries[0].id;
  current = applySnapshot(current, snapshot(events));
  assert.equal(current.entries.length, 1);
  assert.equal(current.entries[0].id, id);
  assert.equal(current.entries[0].state, "Waiting for approval");
  assert.equal(current.entries[0].approvalId, "nonce");
  assert.equal(current.entries[0].inputs, JSON.stringify(approval.arguments, null, 2));
  assert.equal(pendingApprovals(current.activeRun)[0].approval_id, "nonce");
});

test("abandoned attempts and task scopes cannot absorb a later call reusing the provider ID", () => {
  let entries = appendEvent([], progress({ id: "call" }));
  entries = appendEvent(entries, progress({ id: "call", status: "abandoned", arguments_text: "incomplete" }));
  assert.equal(entries[0].state, "Not run");
  assert.equal(appendEvent(entries, canonical()), entries, "A late canonical event cannot revive an abandoned attempt");
  entries = appendEvent(entries, progress({ id: "call", generation_id: "retry" }));
  entries = appendEvent(entries, canonical({ extra: { generation_id: "retry", index: 0 } }));
  entries = appendEvent(entries, progress({ id: "call", extra: { task_id: "child", activation: 2, followup: 1 } }));
  entries = appendEvent(entries, canonical({ extra: { task_id: "child", activation: 2, followup: 1, generation_id: "generation", index: 0 } }));
  entries = appendEvent(entries, progress({ id: "call", extra: { task_id: "child", activation: 2, followup: 2 } }));
  assert.equal(entries.length, 4);
  assert.deepEqual(entries.map(entry => entry.state), ["Not run", "Requested", "Requested", "Preparing"]);
  assert.equal(entries[2].followup, 1);
});

test("terminal events stop progress and invocation animations, including late output after cancellation", () => {
  for (const terminal of [
    { event: "run_finished", status: "cancelled" }, { event: "run_finished", status: "failed" },
    { event: "run_finished", status: "completed" }, { event: "client_disconnected" },
    { event: "error", message: "bad stream" }, { event: "done" }, { event: "task_completed", status: "cancelled" },
  ]) {
    let entries = appendEvent([], progress({ arguments_text: "partial" }));
    entries = appendEvent(entries, { run_id: "run", ...terminal });
    assert.notEqual(executionStatus(entries[0]).tone, "active", terminal.event);
    assert.equal(entries[0].streaming, false);
    assert.equal(entries[0].toolProgress, "abandoned");
    assert.equal(appendEvent(entries, progress()), entries);
  }
  let entries = appendEvent(appendEvent([], progress()), canonical());
  entries = appendEvent(entries, { event: "run_finished", run_id: "run", status: "cancelled" });
  entries = appendEvent(entries, { event: "tool_output", run_id: "run", call_id: "call", text: "late output" });
  assert.equal(entries[0].state, "Cancelled");
  assert.equal(entries[0].text, "late output");
});

test("complete replay and bounded draft snapshots do not duplicate progress or lose disclosure IDs", () => {
  const first = progress({ arguments_text: '{"command":"' });
  const latest = progress({ id: "call", arguments_text: '{"command":"check' });
  let state = applySnapshot({ entries: [] }, snapshot([first]));
  const id = state.entries[0].id;
  state = applySnapshot(state, snapshot([latest]));
  assert.equal(state.entries[0].id, id);
  state = applySnapshot(state, snapshot([latest, canonical()]));
  assert.equal(state.entries[0].id, id);
  assert.equal(state.entries.length, 1);
  state = applySnapshot(state, snapshot([latest, canonical()]));
  assert.equal(state.entries.length, 1);
  assert.equal(state.entries[0].state, "Requested");
  let fallback: LiveTranscript = applySnapshot({ entries: [] }, snapshot(undefined, [latest]));
  fallback = applySnapshot(fallback, snapshot(undefined, [latest]));
  assert.equal(fallback.entries.length, 1);
  assert.equal(fallback.entries[0].inputs, latest.arguments_text);
  fallback = applySnapshot(fallback, snapshot(undefined, [canonical()]));
  assert.equal(fallback.entries.length, 1);
  assert.equal(fallback.entries[0].state, "Requested");
  const frame = { type: "event" as const, cursor: 1, epoch: "epoch", session_id: "root", record: latest };
  const framed = applyFrame({ entries: [] }, frame);
  assert.equal(applyFrame(framed, frame), framed);
});

test("cold replay reconciles a committed call with history lacking provider generation metadata", () => {
  const history: Snapshot["history"] = [
    { history_id: "user-row", role: "user", content: "Inspect", name: "", tool_call_id: "", tool_calls: [], message_id: "message" },
    { history_id: "call-row", role: "assistant", content: "", name: "", tool_call_id: "", tool_calls: [{ id: "call", name: "shell", arguments: { command: "check" } }] },
  ];
  const events: WireEvent[] = [
    { event: "run_started", run_id: "run", message_id: "message" },
    { event: "user_message", run_id: "run", message_id: "message", history_id: "user-row", text: "Inspect" },
    progress({ id: "call", status: "ready" }),
    canonical({ transcript_event_id: "event" }),
    { event: "transcript_anchor", run_id: "run", history_id: "call-row", calls: [{ event_id: "event", call_position: 0 }] },
  ];
  let current = applySnapshot({ entries: [] }, { ...snapshot(), active_run: undefined, history });
  const id = current.entries.find(entry => entry.kind === "tool")!.id;
  current = applySnapshot(current, { ...snapshot(events), history });
  assert.equal(current.entries.filter(entry => entry.kind === "tool").length, 1);
  assert.equal(current.entries.find(entry => entry.kind === "tool")?.id, id);
  current = applySnapshot(current, { ...snapshot(events), history });
  assert.equal(current.entries.filter(entry => entry.kind === "tool").length, 1);
  const draft = canonical({ history_id: "call-row", call_position: 0 });
  const cold = applySnapshot({ entries: [] }, { ...snapshot(undefined, [draft]), history });
  assert.equal(cold.entries.filter(entry => entry.kind === "tool").length, 1);
});

test("partial inputs are bounded and escaped; skeleton motion has a distinct accessible state", () => {
  const raw = '<script>alert("hi")</script>' + "🙂".repeat(17000);
  const entry = appendEvent([], progress({ arguments_text: raw }))[0];
  assert.equal(Array.from(entry.inputs || "").length, 16384);
  assert.equal(entry.inputsTruncated, true);
  const html = renderToStaticMarkup(createElement(ExecutionRecord, { entry, open: true }));
  assert.doesNotMatch(html, /<script>/);
  const dom = new JSDOM(html);
  assert.equal(dom.window.document.querySelector(".tool-record")?.getAttribute("data-busy"), "true");
  assert.equal(dom.window.document.querySelector('.execution-state[role="status"]')?.textContent, "Preparing");
  assert.match(dom.window.document.querySelector(".tool-input-note")?.textContent || "", /has not started/);
  assert.ok(dom.window.document.querySelector(".tool-argument-preview"));
  dom.window.close();
  const empty = renderToStaticMarkup(createElement(ExecutionRecord, { entry: appendEvent([], progress())[0] }));
  assert.match(empty, /class="tool-skeleton" aria-hidden="true"/);
  assert.match(empty, /Waiting for arguments…/);
  assert.doesNotMatch(empty, /No content returned/);
  for (const state of ["Waiting for approval", "Completed", "Error", "Cancelled", "Not run"]) {
    const terminal: Entry = { ...entry, state, streaming: false, toolProgress: undefined };
    assert.doesNotMatch(renderToStaticMarkup(createElement(ExecutionRecord, { entry: terminal })), /data-busy/);
  }
});
