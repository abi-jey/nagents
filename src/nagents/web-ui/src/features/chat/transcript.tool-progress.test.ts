import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { Snapshot, WireEvent } from "../../types.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { executionStatus } from "./executionPresentation.js";
import { applyFrame, applySnapshot, type LiveTranscript } from "./liveTranscript.js";
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
