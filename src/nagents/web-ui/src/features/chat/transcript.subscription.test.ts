import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Snapshot, WireEvent } from "../../types.js";
import { LiveSessions, applySnapshot, pendingApprovals, type LiveTranscript } from "./liveTranscript.js";
import { INBOUND_PREFIX, channelMessage } from "./channelMessage.js";
import { appendEvent, fromHistory } from "./transcript.js";
import { Conversation } from "./Conversation.js";

const history = (texts: string[], root = "root", messageIds: string[] = []): Snapshot => ({ session_id: root, sessions: [], retained_tasks: [],
  history: texts.map((content, index) => ({ role: "user", content, tool_calls: [], name: "", tool_call_id: "", message_id: messageIds[index], source_verified: !!messageIds[index] })) });
function event(cache: LiveSessions, cursor: number, record: WireEvent, root = "root") {
  return cache.receive({ type: "event", cursor, epoch: "one", session_id: root, record });
}
test("queued optimism reconciles WS echoes and saved history without consuming a previous identical prompt", () => {
  const cache = new LiveSessions(); cache.set("root", applySnapshot({ entries: [] }, history(["repeat"])));
  const queued = { session_id: "root", message_id: "uuid", prompt: "repeat" };
  cache.enqueue(queued);
  cache.set("root", applySnapshot(cache.get("root"), history(["repeat"])));
  assert.equal(cache.get("root").entries.length, 2); assert.equal(cache.get("root").entries[1].queued, true);
  event(cache, 1, { event: "user_message", text: "repeat", message_id: "uuid", run_id: "run" });
  event(cache, 1, { event: "user_message", text: "repeat", message_id: "uuid", run_id: "run" });
  assert.equal(cache.get("root").entries.length, 2);
  cache.set("root", applySnapshot(cache.get("root"), history(["repeat", "repeat"], "root", ["", "uuid"])));
  assert.equal(cache.get("root").entries.length, 2); assert.equal(cache.get("root").entries[1].messageId, "uuid");
  cache.enqueue(queued); assert.equal(cache.get("root").entries.length, 2, "A late ack never inserts a second user message");
});
test("snapshot-before-echo admission race retains the known optimistic identity", () => {
  const cache = new LiveSessions(); cache.set("root", applySnapshot({ entries: [] }, history([])));
  cache.enqueue({ session_id: "root", prompt: "new", message_id: "uuid" });
  cache.set("root", applySnapshot(cache.get("root"), history(["new"], "root", ["uuid"])));
  event(cache, 2, { event: "user_message", text: "new", message_id: "uuid" });
  assert.equal(cache.get("root").entries.length, 1); assert.equal(cache.get("root").entries[0].messageId, "uuid");
});
test("selected-root caches isolate history, replay cursors and pending messages", () => {
  const cache = new LiveSessions();
  cache.receive({ type: "snapshot", session_id: "root", epoch: "one", cursor: 10, snapshot: history(["web"]) });
  cache.receive({ type: "snapshot", session_id: "telegram", epoch: "one", cursor: 20, snapshot: history(["channel"], "telegram") });
  cache.enqueue({ session_id: "root", prompt: "queued in original selection", message_id: "id" });
  assert.deepEqual(cache.get("telegram").entries.map((entry) => entry.text), ["channel"]);
  assert.equal(cache.get("root").position?.cursor, 10); assert.equal(cache.get("telegram").position?.cursor, 20);
  event(cache, 21, { event: "text_done", text: "answer", run_id: "telegram-run" }, "telegram");
  assert.equal(cache.get("root").entries.at(-1)?.messageId, "id");
});
test("active snapshot restores scoped child approval, provisional activation and output without replay duplication", () => {
  const records: WireEvent[] = [
    { event: "run_started", run_id: "run" },
    { event: "user_message", run_id: "run", text: "question", message_id: "uuid" },
    { event: "text_chunk", run_id: "run", chunk: "partial" },
    { event: "task_started", run_id: "run", task_id: "parent", activation: 1, name: "Parent", parent_task_id: "" },
    { event: "approval", run_id: "run", task_id: "child", activation: 3, call_id: "call", approval_id: "approval", tool: "shell" },
    { event: "tool_output", run_id: "run", task_id: "child", activation: 3, call_id: "call", text: "child partial" },
  ];
  const snapshot = { ...history(["question"], "root", ["uuid"]), active_run: { id: "run", status: "running", events: records } };
  let state = applySnapshot({ entries: [] }, snapshot);
  assert.equal(state.entries.filter((entry) => entry.kind === "user").length, 1);
  const child = state.entries.find((entry) => entry.callId === "call")!;
  assert.equal(child.taskId, "child"); assert.equal(child.activation, 3); assert.equal(child.provisional, true);
  assert.equal(pendingApprovals(state.activeRun)[0].approval_id, "approval");
  const before = state.entries.map((entry) => entry.id);
  state = applySnapshot(state, snapshot);
  assert.deepEqual(state.entries.map((entry) => entry.id), before);
  assert.equal(state.entries.find((entry) => entry.callId === "call")?.text, "child partial");
  assert.equal(state.entries.filter((entry) => entry.kind === "assistant").length, 1);
});
test("reconnect into active run with already loaded HTTP history does not duplicate its saved user message", () => {
  let state: LiveTranscript = applySnapshot({ entries: [] }, history(["question"], "root", ["id"]));
  state = applySnapshot(state, { ...history(["question"], "root", ["id"]), active_run: { id: "run", status: "running", events: [
    { event: "user_message", run_id: "run", text: "question", message_id: "id" },
    { event: "text_chunk", run_id: "run", chunk: "partial" },
  ] } });
  assert.equal(state.entries.filter((entry) => entry.kind === "user").length, 1);
});
test("out-of-band snapshot keeps finished scoped child evidence and stable open tool IDs", () => {
  const cache = new LiveSessions(); cache.set("root", applySnapshot({ entries: [] }, history(["initial"])));
  event(cache, 1, { event: "tool_call", run_id: "r", task_id: "child", activation: 2, call_id: "c", name: "shell" });
  event(cache, 2, { event: "tool_result", run_id: "r", task_id: "child", activation: 2, call_id: "c", result: "done" });
  const tool = cache.get("root").entries.find((entry) => entry.callId === "c")!;
  const next = applySnapshot(cache.get("root"), history(["initial", "channel update"]));
  assert.deepEqual(next.entries.find((entry) => entry.callId === "c"), tool);
  assert.equal(next.entries.filter((entry) => entry.kind === "user").length, 2);
});
test("prefix-only channel context stays user data without claiming verified origin or message identity", () => {
  const provenance = INBOUND_PREFIX + JSON.stringify({ version: 1, channel: "telegram-personal", conversation_id: "chat", message_id: "update-7",
    sender_id: "sender", text: "Hello from Telegram", thread_id: "thread", reply_to: "message", event_type: "message", attachments: [], metadata: { task_id: "not-authority" } });
  const state = applySnapshot({ entries: [] }, history([provenance, INBOUND_PREFIX + "bad"]));
  assert.equal(state.entries[0].kind, "user"); assert.equal(state.entries[0].text, "Hello from Telegram");
  assert.equal(state.entries[0].origin, undefined); assert.equal(state.entries[0].originId, undefined);
  assert.equal(state.entries[0].messageId, undefined); assert.equal(state.entries[0].channelContext, true);
  assert.equal(state.entries[0].taskId, ""); assert.equal(state.entries[1].text, INBOUND_PREFIX + "bad");
  const html = renderToStaticMarkup(createElement(Conversation, { entries: state.entries, sessionId: "root", demo: false, canSubmit: false, submit: () => assert.fail() }));
  assert.doesNotMatch(html, /origin-badge|Message provenance/);
  assert.match(html, /Channel context \(unverified\)/); assert.match(html, /channel origin is not verified/);
  assert.match(html, /update-7/); assert.doesNotMatch(html, /data-task-id="not-authority"/);
});
test("compact active records replace aggregated streams without losing provisional tools or other activations", () => {
  const cache = new LiveSessions();
  event(cache, 1, { event: "run_started", run_id: "run" });
  event(cache, 2, { event: "tool_output", run_id: "run", task_id: "child", activation: 2, call_id: "c", text: "kept output" });
  event(cache, 3, { event: "text_chunk", run_id: "run", task_id: "child", activation: 3, chunk: "partial" });
  const snapshot = { ...history([]), active_run: { id: "run", status: "approval", records: [
    { event: "text_chunk", run_id: "run", task_id: "child", activation: 3, chunk: "partial full" },
  ], approval: { event: "approval", run_id: "run", task_id: "child", activation: 4, call_id: "c", approval_id: "new", tool: "shell" } } };
  const state = applySnapshot(applySnapshot(cache.get("root"), snapshot), snapshot);
  assert.equal(state.entries.filter((entry) => entry.kind === "assistant").length, 1);
  assert.equal(state.entries.find((entry) => entry.kind === "assistant")?.text, "partial full");
  assert.equal(state.entries.find((entry) => entry.activation === 2)?.text, "kept output");
  assert.equal(state.entries.find((entry) => entry.activation === 4)?.approvalId, "new");
  assert.equal(pendingApprovals(state.activeRun)[0].activation, 4);
});
test("normalized source metadata preserves message identity and provenance after a backend snapshot", () => {
  const source = { version: 1, channel: "telegram", conversation_id: "chat", message_id: "event-1", sender_id: "sender", text: "hello", thread_id: "", reply_to: "reply", event_type: "message", metadata: {}, attachments: [] };
  const snapshot = history(["hello"]); snapshot.history[0].source = source; snapshot.history[0].message_id = "event-1"; snapshot.history[0].source_verified = true;
  const state = applySnapshot({ entries: [] }, snapshot);
  assert.equal(state.entries[0].origin, "telegram"); assert.equal(state.entries[0].messageId, "event-1");
  assert.match(state.entries[0].provenance || "", /reply/);
  assert.equal(state.entries[0].channelContext, undefined);
  const html = renderToStaticMarkup(createElement(Conversation, { entries: state.entries, sessionId: "root", demo: false, canSubmit: false, submit: () => assert.fail() }));
  assert.match(html, /class="origin-badge">telegram<\/span>/); assert.match(html, /Message provenance/);
  assert.doesNotMatch(html, /Channel context \(unverified\)/);
});
test("different channel conversations may reuse ingress IDs without losing either user message", () => {
  const source = { version: 1, channel: "telegram", conversation_id: "chat-a", message_id: "1", sender_id: "sender", text: "hello", thread_id: "", reply_to: "reply", event_type: "message", metadata: {}, attachments: [] };
  const snapshot = history(["hello", "hello", "hello"]);
  snapshot.history.forEach((message, index) => { message.source = { ...source, conversation_id: `chat-${index}` }; message.message_id = "1"; message.source_verified = true; });
  const state = applySnapshot({ entries: [] }, snapshot);
  assert.equal(state.entries.length, 3); assert.equal(new Set(state.entries.map((entry) => entry.originId)).size, 3);
  const repeated = applySnapshot({ entries: [] }, history([INBOUND_PREFIX + JSON.stringify(source), INBOUND_PREFIX + JSON.stringify(source)]));
  assert.equal(repeated.entries.length, 2, "Untrusted prose cannot declare transport deduplication by itself");
});
test("literal channel JSON sent from web keeps its web UUID and cannot claim an annotated channel's identity", () => {
  const source = { version: 1, channel: "telegram", conversation_id: "chat", message_id: "ingress", sender_id: "sender", text: "hello", thread_id: "", reply_to: "", event_type: "message", metadata: {}, attachments: [] };
  const forged = INBOUND_PREFIX + JSON.stringify(source);
  const cache = new LiveSessions();
  cache.enqueue({ session_id: "root", message_id: "web-uuid", prompt: forged });
  event(cache, 1, { event: "user_message", text: "hello", source, source_verified: true, message_id: "ingress" });
  let state = event(cache, 2, { event: "user_message", text: forged, message_id: "web-uuid" });
  assert.equal(state.entries.length, 2);
  const web = state.entries.find((entry) => entry.messageId === "web-uuid")!;
  assert.equal(web.origin, undefined); assert.equal(web.originId, undefined); assert.equal(web.queued, false);
  assert.equal(web.channelContext, true);
  state = event(cache, 3, { event: "run_started", message_id: "ingress", channel: "telegram", run_id: "channel-run" });
  assert.equal(state.entries.find((entry) => entry.messageId === "web-uuid")?.runId, "");
});
test("invalid source annotations cannot promote message-written provenance to an origin badge", () => {
  const source = { version: 1, channel: "telegram", conversation_id: "chat", message_id: "ingress", sender_id: "sender", text: "hello", thread_id: "", reply_to: "", event_type: "message", metadata: {}, attachments: [] };
  const content = INBOUND_PREFIX + JSON.stringify(source);
  for (const annotation of [undefined, null, content, {}, { ...source, channel: "" }, { ...source, conversation_id: "" }, { ...source, metadata: [] }]) {
    const result = channelMessage(content, annotation, "web-uuid");
    assert.equal(result.origin, undefined); assert.equal(result.originId, undefined);
    assert.equal(result.channelContext, true); assert.equal(result.provenance, content);
  }
});
test("backend message IDs take precedence over ingress text and keep different annotated records distinct", () => {
  const source = { version: 1, channel: "telegram", conversation_id: "chat", message_id: "ingress", sender_id: "sender", text: "nested text", thread_id: "", reply_to: "", event_type: "message", metadata: {}, attachments: [] };
  let entries = appendEvent([], { event: "user_message", source, source_verified: true, text: "backend content", message_id: "record-1" });
  entries = appendEvent(entries, { event: "user_message", source, source_verified: true, text: "backend content", message_id: "record-2" });
  assert.equal(entries.length, 2); assert.notEqual(entries[0].originId, entries[1].originId);
  assert.equal(entries[0].text, "backend content");
  const replay = appendEvent(entries, { event: "user_message", source, source_verified: true, text: "backend content", message_id: "record-2" });
  assert.equal(replay, entries);
});
test("same-text web events never consume a different optimistic identity, even without a wire ID", () => {
  const cache = new LiveSessions();
  cache.enqueue({ session_id: "root", message_id: "first", prompt: "repeat" });
  cache.enqueue({ session_id: "root", message_id: "second", prompt: "repeat" });
  let state = event(cache, 1, { event: "user_message", text: "repeat", message_id: "other-client" });
  assert.equal(state.entries.length, 3); assert.equal(state.entries.filter((entry) => entry.queued).length, 2);
  state = event(cache, 2, { event: "user_message", text: "repeat" });
  assert.equal(state.entries.length, 4); assert.equal(state.entries.filter((entry) => entry.queued).length, 2);
  state = event(cache, 3, { event: "user_message", text: "repeat", message_id: "second" });
  assert.equal(state.entries.length, 4);
  assert.equal(state.entries.find((entry) => entry.messageId === "first")?.queued, true);
  assert.equal(state.entries.find((entry) => entry.messageId === "second")?.queued, false);
});
test("unidentified saved rows cannot acknowledge matching queued text; later UUID annotations reconcile it", () => {
  const cache = new LiveSessions();
  cache.enqueue({ session_id: "root", message_id: "pending", prompt: "repeat" });
  const snapshot = history(["repeat", "repeat"]);
  const state = applySnapshot(cache.get("root"), snapshot);
  assert.equal(state.entries.length, 3); assert.equal(state.entries.find((entry) => entry.messageId === "pending")?.queued, true);
  const refreshed = applySnapshot(state, snapshot);
  assert.deepEqual(refreshed.entries.map((entry) => entry.id), state.entries.map((entry) => entry.id));
  snapshot.history[1].message_id = "pending";
  const annotated = applySnapshot(refreshed, snapshot);
  assert.equal(annotated.entries.length, 2);
  assert.equal(annotated.entries.find((entry) => entry.messageId === "pending")?.queued, false);
});
test("snapshot identity matching follows persisted order without duplicating reordered optimistic messages", () => {
  const cache = new LiveSessions();
  const first = cache.enqueue({ session_id: "root", message_id: "first", prompt: "repeat" }).entries[0].id;
  const second = cache.enqueue({ session_id: "root", message_id: "second", prompt: "repeat" }).entries[1].id;
  const state = applySnapshot(cache.get("root"), history(["repeat", "repeat"], "root", ["second", "first"]));
  assert.deepEqual(state.entries.map((entry) => entry.messageId), ["second", "first"]);
  assert.deepEqual(state.entries.map((entry) => entry.id), [second, first]);
});
test("same-run replay maps user entries by UUID rather than identical text", () => {
  const cache = new LiveSessions();
  cache.enqueue({ session_id: "root", message_id: "first", prompt: "repeat" });
  cache.enqueue({ session_id: "root", message_id: "second", prompt: "repeat" });
  event(cache, 1, { event: "user_message", run_id: "run", message_id: "first", text: "repeat" });
  event(cache, 2, { event: "user_message", run_id: "run", message_id: "second", text: "repeat" });
  const ids = cache.get("root").entries.map((entry) => entry.id);
  const snapshot = { ...history(["repeat", "repeat"], "root", ["second", "first"]), active_run: {
    id: "run", status: "running", events: [
      { event: "user_message", run_id: "run", message_id: "second", text: "repeat" },
      { event: "user_message", run_id: "run", message_id: "first", text: "repeat" },
    ],
  } };
  assert.deepEqual(applySnapshot(cache.get("root"), snapshot).entries.map((entry) => entry.id), [ids[1], ids[0]]);
});
test("replayed user data without identity cannot borrow the identity of equal saved prose", () => {
  const snapshot = history(["repeat"]);
  const initial = applySnapshot({ entries: [] }, snapshot);
  const state = applySnapshot(initial, { ...snapshot, active_run: { id: "run", status: "running", events: [
    { event: "user_message", run_id: "run", text: "repeat", message_id: "new-message" },
  ] } });
  assert.equal(state.entries.length, 2);
  assert.equal(state.entries[0].id, initial.entries[0].id);
  assert.equal(state.entries[1].messageId, "new-message");
  assert.equal(fromHistory(history(["repeat", "repeat"])).length, 2);
});

function channelHistory(ids: string[]): Snapshot {
  const snapshot = history(ids.map((id) => `Message ${id}`), "root", ids);
  snapshot.history.forEach((message, index) => { message.source = {
    version: 1, channel: "telegram", conversation_id: "chat", message_id: ids[index], sender_id: "sender", text: message.content,
    thread_id: "", reply_to: "", event_type: "message", metadata: {}, attachments: [],
  }; });
  return snapshot;
}
const activity = (state: LiveTranscript) => state.entries.reduce((latest, entry) => Math.max(latest, entry.activity || 0), 0);

test("late initial history hydration and its matching WS snapshot do not announce restored channel messages as new activity", () => {
  const snapshot = channelHistory(["saved-1", "saved-2"]);
  assert.ok(fromHistory(snapshot).every((entry) => entry.activity === undefined));
  const cache = new LiveSessions();
  // The conversation may already have mounted with an empty selected-root view.
  assert.equal(activity(cache.get("root")), 0);
  cache.set("root", applySnapshot(cache.get("root"), snapshot, false));
  assert.equal(activity(cache.get("root")), 0);
  const restored = cache.receive({ type: "snapshot", session_id: "root", cursor: 10, epoch: "one", snapshot });
  assert.equal(activity(restored), 0);
  const cold = new LiveSessions().receive({ type: "snapshot", session_id: "root", cursor: 10, epoch: "one", snapshot });
  assert.equal(activity(cold), 0, "A WS-only initial snapshot also establishes a silent baseline");
});

test("a genuine channel arrival after an empty baseline announces once, including snapshot/echo reconciliation", () => {
  const cache = new LiveSessions();
  cache.set("root", applySnapshot(cache.get("root"), channelHistory([])));
  const snapshot = channelHistory(["arrived"]);
  let state = cache.receive({ type: "snapshot", session_id: "root", cursor: 1, epoch: "one", snapshot });
  assert.equal(activity(state), 1, "Do not silence the first actual arrival just because the transcript was empty");
  assert.equal(state.entries.find((entry) => entry.messageId === "arrived")?.activity, 1);
  state = event(cache, 2, { event: "user_message", message_id: "arrived", source_verified: true, text: "Message arrived", source: snapshot.history[0].source });
  assert.equal(activity(state), 1);
  state = cache.receive({ type: "snapshot", session_id: "root", cursor: 3, epoch: "one", snapshot });
  assert.equal(activity(state), 1, "A matching snapshot must not reset or increment the live activity counter");
  state = cache.receive({ type: "snapshot", session_id: "root", cursor: 4, epoch: "one", snapshot: channelHistory(["arrived", "next"]) });
  assert.equal(activity(state), 2);
});

test("explicit history reload stays quiet but a subsequent live source event still announces", () => {
  let state = applySnapshot({ entries: [] }, channelHistory(["old"]));
  state = applySnapshot(state, channelHistory(["old", "restored-on-refresh"]), false);
  assert.equal(activity(state), 0);
  const cache = new LiveSessions(); cache.set("root", state);
  const message = channelHistory(["live"]).history[0];
  state = event(cache, 5, { event: "user_message", message_id: message.message_id, source_verified: true, text: message.content, source: message.source });
  assert.equal(activity(state), 1); assert.equal(state.entries.at(-1)?.messageId, "live");
  state = cache.receive({ type: "snapshot", session_id: "root", cursor: 6, epoch: "one", snapshot: channelHistory(["old", "restored-on-refresh", "live"]) });
  assert.equal(activity(state), 1);
  assert.ok(state.entries.slice(0, 2).every((entry) => !entry.activity));
});
