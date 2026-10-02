import assert from "node:assert/strict";
import test from "node:test";
import type { Snapshot, WireEvent } from "../../types.js";
import { applySnapshot, LiveSessions } from "./liveTranscript.js";
import { appendEvent, fromHistory, groupLiveCaptions, type Entry } from "./transcript.js";

type Row = Snapshot["history"][number];
const row = (history_id: string, role: string, content: string, extra: Partial<Row> = {}): Row => ({
  history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra,
});
const caption = (seq: number, extra: Partial<WireEvent> = {}): WireEvent => ({
  event: "live_caption", source: "live_caption", history_id: `live-caption:${seq}`, voice_session_id: "voice-a",
  caption_seq: seq, speaker: "user", text: `fragment-${seq}`, start_ms: (seq - 1) * 100, end_ms: seq * 100,
  anchor_history_id: "", ...extra,
});
const captionRow = (seq: number, extra: Partial<Row> = {}): Row => row(`live-caption:${seq}`, "live_caption", `fragment-${seq}`, {
  source: "live_caption", voice_session_id: "voice-a", caption_seq: seq, speaker: "user",
  start_ms: (seq - 1) * 100, end_ms: seq * 100, anchor_history_id: "", ...extra,
});
const snapshot = (history: Row[]): Snapshot => ({ session_id: "root", history, sessions: [], retained_tasks: [] });

test("speech fragments have stable identities independent of equal typed or assistant text", () => {
  let entries = appendEvent([], { event: "user_message", history_id: "typed", text: "Same words" });
  entries = appendEvent(entries, { event: "text_done", history_id: "answer", text: "Same words" });
  const record = caption(1, { text: "Same words" });
  entries = appendEvent(entries, record);
  assert.deepEqual(entries.map((entry) => entry.kind), ["user", "assistant", "live_caption"]);
  assert.equal(entries[2].id, "live-caption:1"); assert.equal(entries[2].historyId, "live-caption:1");
  assert.deepEqual(entries[2].liveCaption, { sessionId: "voice-a", seq: 1, speaker: "user", start: 0, end: 100, anchorHistoryId: "" });
  assert.equal(appendEvent(entries, record), entries, "identical replay is a no-op");
  entries = appendEvent(entries, caption(2, { text: "Same words" }));
  assert.equal(entries.length, 4, "equal text from different fragments is never an identity");
  const conflict = appendEvent(entries, caption(1, { history_id: "live-caption:99" }));
  assert.equal(conflict, entries, "the same call/sequence cannot acquire a conflicting saved-row identity");
  assert.equal(appendEvent(entries, caption(1, { voice_session_id: "another-call" })), entries);
});

test("malformed caption records never become speech badges or ordinary model messages", () => {
  const invalid: Partial<WireEvent>[] = [
    { source: undefined }, { source: "voice" }, { history_id: "regular-row" }, { history_id: "live-caption:0" },
    { voice_session_id: "" }, { voice_session_id: 42 }, { caption_seq: 0 }, { caption_seq: 1.5 }, { caption_seq: Number.MAX_SAFE_INTEGER + 1 },
    { speaker: "system" }, { text: "" }, { text: { html: "<script>" } }, { start_ms: undefined }, { end_ms: undefined },
    { start_ms: -1 }, { start_ms: Infinity }, { start_ms: NaN }, { end_ms: -1 }, { start_ms: 101, end_ms: 100 },
    { anchor_history_id: undefined }, { anchor_history_id: 10 },
  ];
  const previous: Entry[] = [{ id: "typed", kind: "user", text: "Keep typed text" }];
  for (const extra of invalid) assert.equal(appendEvent(previous, caption(1, extra)), previous);
  const invalidSaved = { ...captionRow(1), end_ms: NaN };
  assert.deepEqual(fromHistory(snapshot([invalidSaved])), []);
  const fractional = appendEvent([], caption(1, { start_ms: 0.25, end_ms: 99.75 }));
  assert.equal(fractional[0].liveCaption?.start, 0.25, "valid provider fractional timing remains exact");
});

test("history restores backend-ordered captions alongside typed text, assistant output and tools", () => {
  const history = [
    captionRow(1),
    row("1", "user", "A typed question"),
    captionRow(2, { anchor_history_id: "1", speaker: "assistant", content: "Thinking aloud" }),
    row("2", "assistant", "Thinking aloud", { tool_calls: [{ id: "lookup", name: "search", arguments: {} }] }),
    captionRow(3, { anchor_history_id: "2", content: "A spoken correction" }),
    row("3", "tool", "Verified search result", { tool_call_id: "lookup", name: "search" }),
    row("4", "user", "Delegated request", { voice_verified: true, voice_session_id: "voice-a" }),
    row("5", "assistant", "A regular assistant result"),
    captionRow(4, { anchor_history_id: "5", speaker: "assistant", content: "A spoken answer" }),
  ];
  const entries = fromHistory(snapshot(history));
  assert.deepEqual(entries.map((entry) => entry.kind), ["live_caption", "user", "live_caption", "assistant", "tool", "live_caption", "user", "assistant", "live_caption"]);
  assert.deepEqual(entries.filter((entry) => entry.liveCaption).map((entry) => entry.historyId), ["live-caption:1", "live-caption:2", "live-caption:3", "live-caption:4"]);
  assert.equal(entries.find((entry) => entry.kind === "tool")?.result, "Verified search result");
  const delegated = entries.find((entry) => entry.historyId === "4")!;
  assert.equal(delegated.voice, true); assert.equal(delegated.voiceSessionId, "voice-a");
  assert.equal(entries.find((entry) => entry.historyId === "1")?.voiceSessionId, undefined);
  assert.equal(entries.filter((entry) => entry.kind === "assistant").length, 2);
});

test("render grouping keeps speaker, call, timing, real-message and anchor boundaries without mutating source fragments", () => {
  let entries: Entry[] = [];
  for (const event of [
    caption(1, { text: "Hello" }), caption(2, { text: " there" }),
    caption(3, { speaker: "assistant", text: "Hi" }),
    caption(4, { speaker: "assistant", text: " again", start_ms: 1_900, end_ms: 2_000 }),
    caption(5, { speaker: "assistant", voice_session_id: "voice-b", text: "New call", start_ms: 2_001, end_ms: 2_100 }),
    { event: "text_done", text: "Actual assistant answer", history_id: "assistant-row" },
    caption(6, { speaker: "assistant", voice_session_id: "voice-b", text: "After model answer", start_ms: 2_101, end_ms: 2_200 }),
    caption(7, { speaker: "assistant", voice_session_id: "voice-b", text: "New anchor", anchor_history_id: "assistant-row", start_ms: 2_201, end_ms: 2_300 }),
  ]) entries = appendEvent(entries, event);
  const original = structuredClone(entries), display = groupLiveCaptions(entries);
  assert.equal(display.length, 7); assert.equal(display[0].text, "Hello there");
  assert.deepEqual(display[0].liveCaption?.fragmentIds, ["live-caption:1", "live-caption:2"]);
  assert.deepEqual(display[1].liveCaption?.fragmentIds, ["live-caption:3"]);
  assert.equal(display[0].liveCaption?.end, 200);
  assert.deepEqual(entries, original, "only the derived render entries are grouped");
  const long = appendEvent(appendEvent([], caption(1, { text: "a".repeat(1_990) })), caption(2, { text: "b".repeat(11) }));
  assert.equal(groupLiveCaptions(long).length, 2);
  const late = appendEvent(appendEvent([], caption(2)), caption(1));
  assert.equal(groupLiveCaptions(late).length, 2, "late/out-of-order speech retains its own fragment");
});

test("saved speech refreshes and websocket replays keep IDs without consuming an active assistant stream", () => {
  const cache = new LiveSessions();
  const first = snapshot([row("typed", "user", "Question", { message_id: "input" }), captionRow(1)]);
  first.active_run = { id: "run", status: "running", events: [
    { event: "user_message", history_id: "typed", message_id: "input", text: "Question" },
    { event: "text_chunk", chunk: "fragment-1" },
  ] };
  let state = cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 1, snapshot: first });
  const speechId = state.entries.find((entry) => entry.liveCaption)?.id;
  state = cache.receive({ type: "event", session_id: "root", epoch: "server", cursor: 2, record: caption(1) });
  state = cache.receive({ type: "event", session_id: "root", epoch: "server", cursor: 3, record: caption(2) });
  const liveId = state.entries.find((entry) => entry.liveCaption?.seq === 2)?.id;
  const refreshed = { ...first, history: [...first.history, captionRow(2)] };
  state = cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 4, snapshot: refreshed });
  state = cache.receive({ type: "event", session_id: "root", epoch: "server", cursor: 5, record: caption(2) });
  assert.deepEqual(state.entries.filter((entry) => entry.liveCaption).map((entry) => entry.id), [speechId, liveId]);
  assert.equal(state.entries.filter((entry) => entry.kind === "assistant").length, 1);
  assert.equal(state.entries.find((entry) => entry.kind === "assistant")?.streaming, true);
  assert.equal(state.entries.find((entry) => entry.kind === "assistant")?.text, "fragment-1");
});

test("a stale history snapshot retains newly observed speech until persistence catches up, while a known removed row disappears", () => {
  const empty = snapshot([]);
  let state = applySnapshot({ entries: [] }, empty);
  state = { ...state, entries: appendEvent(state.entries, caption(1)) };
  state = applySnapshot(state, empty);
  assert.equal(state.entries.length, 1);
  state = applySnapshot(state, snapshot([captionRow(1)]));
  assert.equal(state.entries.length, 1); assert.equal(state.entries[0].id, "live-caption:1");
  state = applySnapshot(state, empty);
  assert.deepEqual(state.entries, []);
});

test("an ordered socket snapshot removes cleared speech even before an earlier history snapshot observed it", () => {
  const cache = new LiveSessions();
  const empty = snapshot([]);
  cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 1, snapshot: empty });
  cache.receive({ type: "event", session_id: "root", epoch: "server", cursor: 2, record: caption(1) });
  let state = cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 1, snapshot: empty });
  assert.equal(state.entries.length, 1, "an older cursor cannot clear newer speech");
  state = cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 3, snapshot: empty });
  assert.deepEqual(state.entries, [], "clear/compaction cannot resurrect an event-only caption");
});

test("only verified voice user provenance can link a delegation to its speech call", () => {
  const verified = { event: "user_message", history_id: "voice-input", text: "A delegated request", voice_verified: true, voice_session_id: "voice-a" };
  let entries = appendEvent([], verified);
  assert.equal(entries[0].voiceSessionId, "voice-a");
  for (const voice_verified of [undefined, false, 1, "true"]) {
    const unverified = appendEvent(entries, { ...verified, voice_verified });
    assert.equal(unverified[0].voice, undefined); assert.equal(unverified[0].voiceSessionId, undefined);
  }
  const queued = appendEvent([], { ...verified, queued: true });
  const admitted = appendEvent(queued, { ...verified, voice_verified: false });
  assert.equal(admitted[0].voice, undefined); assert.equal(admitted[0].voiceSessionId, undefined);
  entries = applySnapshot({ entries }, snapshot([row("voice-input", "user", "A delegated request", { voice_session_id: "voice-a" })])).entries;
  assert.equal(entries[0].voice, undefined); assert.equal(entries[0].voiceSessionId, undefined);
  const typed = appendEvent([], { event: "user_message", text: JSON.stringify(caption(1)), source: "live_caption", voice_session_id: "voice-a" });
  assert.equal(typed[0].kind, "user"); assert.equal(typed[0].liveCaption, undefined); assert.equal(typed[0].voiceSessionId, undefined);
});

test("durable caption history and replay retain more than a live-view window", () => {
  const history = Array.from({ length: 260 }, (_, index) => captionRow(index + 1));
  let entries = fromHistory(snapshot(history));
  entries = appendEvent(entries, caption(1));
  entries = appendEvent(entries, caption(261));
  assert.equal(entries.length, 261); assert.equal(entries[0].text, "fragment-1");
  assert.equal(entries.at(-1)?.historyId, "live-caption:261");
});

test("fresh speech announces activity once, survives refresh and targets every grouped fragment while saved history stays silent", () => {
  const saved = snapshot([captionRow(1), captionRow(2)]);
  assert.ok(fromHistory(saved).every((entry) => entry.activity === undefined));
  let state = applySnapshot({ entries: [] }, snapshot([]));
  state = { ...state, entries: appendEvent(state.entries, { event: "notice", level: "warning", text: "Earlier activity" }) };
  state = { ...state, entries: appendEvent(state.entries, caption(1)) };
  state = { ...state, entries: appendEvent(state.entries, caption(2)) };
  assert.deepEqual(state.entries.filter((entry) => entry.liveCaption).map((entry) => entry.activity), [2, 3]);
  state = applySnapshot(state, saved);
  state = { ...state, entries: appendEvent(state.entries, caption(2)) };
  assert.deepEqual(state.entries.map((entry) => entry.activity), [2, 3]);
  const display = groupLiveCaptions(state.entries);
  assert.deepEqual(display[0].liveCaption?.fragmentIds, [state.entries[0].id, state.entries[1].id]);
  state = applySnapshot(state, snapshot([...saved.history, captionRow(3)]));
  assert.deepEqual(state.entries.map((entry) => entry.activity), [2, 3, 4], "snapshot recovery announces only a newly recovered caption");
  state = applySnapshot(state, snapshot([...saved.history, captionRow(3)]));
  assert.deepEqual(state.entries.map((entry) => entry.activity), [2, 3, 4]);
});
