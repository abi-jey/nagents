import assert from "node:assert/strict";
import test from "node:test";
import type { Snapshot, WireEvent } from "../../types.js";
import { applySnapshot, LiveSessions, pendingApprovals } from "./liveTranscript.js";
import { appendEvent, fromHistory } from "./transcript.js";

const partial = "Processing the synthetic message. ";
const complete = partial + "Complete.";
type Row = Snapshot["history"][number];
const row = (history_id: string, role: string, content: string, extra: Partial<Row> = {}): Row => ({
  history_id, role, content, name: "", tool_call_id: "", tool_calls: [], source_verified: false, ...extra,
});
const source = { version: 1, channel: "telegram", conversation_id: "synthetic-chat", message_id: "telegram-message",
  sender_id: "synthetic-user", text: "A message from Telegram.", thread_id: "", reply_to: "", event_type: "message", metadata: {}, attachments: [] };
function held(events = true): Snapshot {
  return {
    session_id: "root", sessions: [], retained_tasks: [],
    history: [
      row("history-user-1", "user", "Install the synthetic connector.", { message_id: "web-1", ingress_id: "ingress-1", source_verified: true }),
      row("history-call-1", "assistant", "", { tool_calls: [{ id: "call", name: "shell", arguments: { command: "synthetic" } }] }),
      row("history-result-1", "tool", "Installed.", { tool_call_id: "call", name: "shell" }),
      row("history-answer-1", "assistant", complete),
      row("history-user-2", "user", source.text, { message_id: source.message_id, ingress_id: "ingress-2", source_verified: true, source }),
      row("history-answer-2", "assistant", complete),
      row("history-user-3", "user", "Work from the browser in the main session.", { message_id: "web-3", ingress_id: "ingress-3", source_verified: true }),
    ],
    active_run: { id: "run-3", status: "running", message_id: "web-3", ingress_id: "ingress-3",
      ...(events ? { events: [
        { event: "user_message", run_id: "run-3", history_id: "history-user-3", ingress_id: "ingress-3", message_id: "web-3", source_verified: true, text: "Work from the browser in the main session." },
        { event: "text_chunk", run_id: "run-3", chunk: partial },
      ] } : {}),
      records: [{ event: "text_chunk", run_id: "run-3", chunk: partial }],
    },
  };
}

for (const replay of [true, false]) test(`cold three-turn history keeps identical completed answers and positions the active partial in turn three (events=${replay})`, () => {
  const snapshot = held(replay);
  const cache = new LiveSessions();
  const http = applySnapshot({ entries: [] }, snapshot, false);
  cache.set("root", http);
  const assertCorrect = (entries: typeof http.entries) => {
    assert.deepEqual(entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, partial]);
    const current = entries.findIndex((entry) => entry.messageId === "web-3");
    assert.equal(entries[current + 1]?.kind, "assistant"); assert.equal(entries[current + 1]?.text, partial);
    assert.equal(entries[current + 1]?.runId, "run-3"); assert.equal(entries[current + 1]?.streaming, true);
    assert.equal(entries[current + 1]?.historyId, undefined, "An unfinished stream has no invented saved-row ID");
    assert.deepEqual(entries.filter((entry) => entry.kind === "assistant").slice(0, 2).map((entry) => entry.historyId), ["history-answer-1", "history-answer-2"]);
    assert.equal(entries[current].historyId, "history-user-3"); assert.equal(entries[current].ingressId, "ingress-3");
    assert.equal(entries.find((entry) => entry.messageId === source.message_id)?.origin, "telegram");
    assert.equal(entries.find((entry) => entry.kind === "tool")?.result, "Installed.");
  };
  assertCorrect(http.entries);
  const ws = cache.receive({ type: "snapshot", session_id: "root", cursor: 7, epoch: "same-process", snapshot });
  assertCorrect(ws.entries); assert.deepEqual(ws.entries.map((entry) => entry.id), http.entries.map((entry) => entry.id));
  const live = cache.receive({ type: "event", session_id: "root", cursor: 8, epoch: "same-process", record: { event: "text_chunk", run_id: "run-3", chunk: "Still working." } });
  assert.deepEqual(live.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, partial + "Still working."]);
});

test("legacy history without row IDs still bounds live reconciliation by the originating user turn", () => {
  const snapshot = held();
  snapshot.history = snapshot.history.map(({ history_id: _id, ingress_id: _ingress, ...message }) => message);
  snapshot.active_run!.events = snapshot.active_run!.events!.map(({ history_id: _id, ingress_id: _ingress, ...record }) => record as WireEvent);
  delete snapshot.active_run!.ingress_id;
  let state = applySnapshot({ entries: [] }, snapshot);
  state = applySnapshot(state, snapshot);
  assert.deepEqual(state.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, partial]);
  assert.equal(state.entries.at(-1)?.runId, "run-3");
});

test("finishing the held turn reconciles its committed answer once without changing earlier repeated answers or disclosure IDs", () => {
  const snapshot = held();
  let state = applySnapshot({ entries: [] }, snapshot);
  const ids = state.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.id);
  const final: Snapshot = { ...snapshot, active_run: null, history: [...snapshot.history, row("history-answer-3", "assistant", complete)] };
  state = applySnapshot(state, final);
  state = applySnapshot(state, final);
  const answers = state.entries.filter((entry) => entry.kind === "assistant");
  assert.deepEqual(answers.map((entry) => entry.text), [complete, complete, complete]);
  assert.deepEqual(answers.map((entry) => entry.id), ids);
  assert.deepEqual(answers.map((entry) => entry.historyId), ["history-answer-1", "history-answer-2", "history-answer-3"]);
  assert.ok(answers.every((entry) => !entry.streaming));
});

test("repeated tool call IDs are scoped to saved user turns, including an orphan result in a later turn", () => {
  const snapshot: Snapshot = { session_id: "root", sessions: [], retained_tasks: [], history: [
    row("u1", "user", "First", { message_id: "first" }),
    row("c1", "assistant", "", { tool_calls: [{ id: "reused", name: "shell", arguments: { turn: 1 } }] }),
    row("r1", "tool", "first result", { tool_call_id: "reused" }),
    row("u2", "user", "Second", { message_id: "second" }),
    row("c2", "assistant", "", { tool_calls: [{ id: "reused", name: "shell", arguments: { turn: 2 } }] }),
    row("r2", "tool", "second result", { tool_call_id: "reused" }),
    row("u3", "user", "Orphan result", { message_id: "third" }),
    row("r3", "tool", "orphan result", { tool_call_id: "reused" }),
    row("r4", "tool", "another orphan result", { tool_call_id: "reused" }),
  ] };
  const entries = fromHistory(snapshot);
  assert.deepEqual(entries.filter((entry) => entry.kind === "tool").map((entry) => entry.result), ["first result", "second result", "orphan result", "another orphan result"]);
  assert.equal(entries.filter((entry) => entry.kind === "tool").at(-1)?.provisional, true);
  assert.deepEqual(entries.filter((entry) => entry.kind === "tool").map((entry) => [entry.historyId, entry.resultHistoryId]), [["c1", "r1"], ["c2", "r2"], ["r3", "r3"], ["r4", "r4"]]);
});

test("a cold active turn containing completed repeated responses keeps its unfinished stream separate", () => {
  const snapshot = held();
  const input = snapshot.active_run!.events![0];
  snapshot.history.push(
    row("current-answer-1", "assistant", complete),
    row("current-call", "assistant", "", { tool_calls: [{ id: "call", name: "shell", arguments: {} }] }),
    row("current-result", "tool", "current result", { tool_call_id: "call" }),
    row("current-answer-2", "assistant", complete),
  );
  snapshot.active_run!.events = [input,
    { event: "text_done", run_id: "run-3", text: complete },
    { event: "tool_call", run_id: "run-3", call_id: "call", name: "shell", arguments: {} },
    { event: "tool_result", run_id: "run-3", call_id: "call", result: "current result" },
    { event: "text_done", run_id: "run-3", text: complete },
    { event: "text_chunk", run_id: "run-3", chunk: partial },
  ];
  let state = applySnapshot({ entries: [] }, snapshot);
  state = applySnapshot(state, snapshot);
  assert.deepEqual(state.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, complete, complete, partial]);
  assert.deepEqual(state.entries.filter((entry) => entry.kind === "tool").map((entry) => entry.result), ["Installed.", "current result"]);
});

test("an unlinked wakeup appends conservatively and reconciles only newly committed history when finished", () => {
  const snapshot = held(false);
  snapshot.history.pop();
  snapshot.active_run = { id: "wakeup-run", status: "running", records: [{ event: "text_chunk", run_id: "wakeup-run", chunk: partial }] };
  let state = applySnapshot({ entries: [] }, snapshot);
  state = applySnapshot(state, snapshot);
  assert.deepEqual(state.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, partial]);
  const saved = { ...snapshot, history: [...snapshot.history, row("wakeup-answer", "assistant", complete)] };
  state = applySnapshot(state, saved);
  state = applySnapshot(state, { ...saved, active_run: null });
  state = applySnapshot(state, { ...saved, active_run: null });
  assert.deepEqual(state.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, complete]);
});

test("child approvals sharing an old call ID retain their execution scope across cold history reconciliation", () => {
  const snapshot = held();
  snapshot.active_run!.approval = { event: "approval", run_id: "run-3", task_id: "child", activation: 4, followup: 2, call_id: "call", approval_id: "approval-current", tool: "shell" };
  let state = applySnapshot({ entries: [] }, snapshot);
  const child = state.entries.find((entry) => entry.approvalId === "approval-current")!;
  assert.equal(child.taskId, "child"); assert.equal(child.activation, 4); assert.equal(child.followup, 2);
  state = applySnapshot(state, snapshot);
  assert.equal(state.entries.find((entry) => entry.approvalId === "approval-current")?.id, child.id);
  assert.equal(pendingApprovals(state.activeRun)[0].approval_id, "approval-current");
  const closed = appendEvent(state.entries, { event: "approval_closed", run_id: "run-3", approval_id: "approval-current", decision: "deny" });
  assert.equal(closed.find((entry) => entry.kind === "tool" && !entry.taskId)?.result, "Installed.");
});

test("ingress-only active input links to its saved user without guessing from equal text", () => {
  const snapshot = held();
  snapshot.history[6].ingress_id = "303";
  delete snapshot.history[6].message_id;
  delete snapshot.active_run!.message_id;
  snapshot.active_run!.ingress_id = 303;
  snapshot.active_run!.events![0] = { event: "user_message", run_id: "run-3", ingress_id: 303, source_verified: true, text: "Work from the browser in the main session." };
  const state = applySnapshot(applySnapshot({ entries: [] }, snapshot), snapshot);
  assert.equal(state.entries.filter((entry) => entry.kind === "user").length, 3);
  assert.deepEqual(state.entries.filter((entry) => entry.kind === "assistant").map((entry) => entry.text), [complete, complete, partial]);
  assert.equal(state.entries.find((entry) => entry.historyId === "history-user-3")?.ingressId, "303");
});

test("aggregated active records retain exact followup scopes and stable IDs when refreshed out of order", () => {
  const snapshot = held(false);
  snapshot.active_run!.records!.push(
    { event: "text_chunk", run_id: "run-3", task_id: "child", activation: 4, followup: 1, chunk: "first followup" },
    { event: "text_chunk", run_id: "run-3", task_id: "child", activation: 4, followup: 2, chunk: "second followup" },
  );
  const first = applySnapshot({ entries: [] }, snapshot);
  snapshot.active_run!.records!.reverse();
  const second = applySnapshot(first, snapshot);
  const old = first.entries.filter((entry) => entry.taskId === "child");
  const children = second.entries.filter((entry) => entry.taskId === "child");
  assert.deepEqual(children.map((entry) => [entry.id, entry.followup, entry.text]), old.map((entry) => [entry.id, entry.followup, entry.text]));
  assert.ok(children.every((entry) => entry.streaming));
});

test("row IDs and ingress IDs do not turn explicitly unverified or legacy text into channel provenance", () => {
  for (const source_verified of [undefined, false, "invalid-flag", 1]) {
    const entries = appendEvent([], { event: "user_message", history_id: "row", ingress_id: "ingress", source_verified, source, text: "Unverified input" });
    assert.equal(entries[0].origin, undefined); assert.equal(entries[0].originId, undefined);
  }
  const legacy = fromHistory({ session_id: "root", retained_tasks: [], history: [{ role: "user", content: "Legacy input", tool_calls: [], name: "", tool_call_id: "" }] });
  assert.equal(legacy[0].historyId, undefined); assert.equal(legacy[0].ingressId, undefined);
  assert.equal(legacy[0].sourceVerified, undefined); assert.equal(legacy[0].origin, undefined);
});

test("false or missing verification removes an old source claim even when authoritative identity still matches", () => {
  for (const source_verified of [false, undefined]) {
    const record = { event: "user_message", history_id: "verified-row", ingress_id: "verified-ingress", message_id: source.message_id, source, text: source.text };
    let entries = appendEvent([], { ...record, source_verified: true });
    const id = entries[0].id;
    assert.equal(entries[0].origin, "telegram");
    entries = appendEvent(entries, { ...record, source_verified });
    assert.equal(entries.length, 1); assert.equal(entries[0].id, id);
    assert.equal(entries[0].origin, undefined); assert.equal(entries[0].originId, undefined);
    assert.equal(entries[0].sourceVerified, source_verified);
    const snapshot: Snapshot = { session_id: "root", sessions: [], retained_tasks: [], history: [
      row("verified-row", "user", source.text, { ingress_id: "verified-ingress", message_id: source.message_id, source, source_verified }),
    ] };
    const state = applySnapshot({ entries }, snapshot);
    assert.equal(state.entries.length, 1); assert.equal(state.entries[0].origin, undefined); assert.equal(state.entries[0].originId, undefined);
  }
});

test("verified linked web input without a source object deduplicates by UUID then adopts history and ingress IDs", () => {
  const cache = new LiveSessions();
  cache.enqueue({ session_id: "root", message_id: "web-uuid", prompt: "Same web message" });
  const optimisticId = cache.get("root").entries[0].id;
  const record = { event: "user_message", run_id: "web-run", message_id: "web-uuid", source_verified: true, text: "Same web message" };
  let state = cache.receive({ type: "event", session_id: "root", cursor: 1, epoch: "process", record });
  assert.equal(state.entries.length, 1); assert.equal(state.entries[0].id, optimisticId);
  assert.equal(state.entries[0].queued, false); assert.equal(state.entries[0].sourceVerified, true);
  assert.equal(state.entries[0].origin, undefined); assert.equal(state.entries[0].originId, undefined);
  const snapshot: Snapshot = { session_id: "root", sessions: [], retained_tasks: [], history: [
    row("web-history", "user", "Same web message", { message_id: "web-uuid", ingress_id: "web-ingress", source_verified: true }),
  ] };
  state = cache.receive({ type: "snapshot", session_id: "root", cursor: 2, epoch: "process", snapshot });
  state = cache.receive({ type: "event", session_id: "root", cursor: 3, epoch: "process", record: { ...record, history_id: "web-history", ingress_id: "web-ingress" } });
  assert.equal(state.entries.length, 1); assert.equal(state.entries[0].id, optimisticId);
  assert.equal(state.entries[0].historyId, "web-history"); assert.equal(state.entries[0].ingressId, "web-ingress");
  assert.equal(state.entries[0].origin, undefined); assert.equal(state.entries[0].originId, undefined);
});
