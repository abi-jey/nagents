import assert from "node:assert/strict";
import test from "node:test";
import type { Snapshot, WireEvent } from "../../types.js";
import { applySnapshot, LiveSessions } from "./liveTranscript.js";

type Row = Snapshot["history"][number];
const answer = "The same legitimate answer.";
const row = (history_id: string, role: string, content: string, extra: Partial<Row> = {}): Row => ({
  history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra,
});
const snapshot = (history: Row[], events?: WireEvent[]): Snapshot => ({
  session_id: "root", sessions: [], retained_tasks: [], history,
  active_run: events ? { id: "run", status: "running", events,
    records: events.at(-1)?.event === "text_chunk" ? [events.at(-1)!] : [] } : null,
});
const replies = (cache: LiveSessions) => cache.get("root").entries.filter(entry => entry.kind === "assistant");

for (const cold of [false, true]) for (const voiceHistory of [false, true]) {
  test(`committed-before-final-event checkpoint keeps one response on both clients (cold=${cold}, voice=${voiceHistory})`, () => {
    const clients = [new LiveSessions(), new LiveSessions()];
    const history = [row("1", "user", "Previous request", { message_id: "previous" }), row("2", "assistant", answer)];
    if (voiceHistory) history.push(row("live-caption:1", "live_caption", answer, {
      source: "live_caption", voice_session_id: "ended-voice", caption_seq: 1, speaker: "assistant",
      start_ms: 10, end_ms: 20, anchor_history_id: "2",
    }));
    const user = row("3", "user", "New typed request after voice", { message_id: "current" });
    const input = { event: "user_message", run_id: "run", history_id: "3", message_id: "current", text: user.content };
    const chunk = { event: "text_chunk", run_id: "run", chunk: "The same " };
    const saved = row("4", "assistant", answer);
    for (const cache of clients) {
      if (!cold) cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 1,
        snapshot: snapshot([...history, user], [input, chunk]) });
      const checkpoint = { type: "snapshot" as const, session_id: "root", epoch: "server", cursor: 2,
        snapshot: snapshot([...history, user, saved], [input, chunk]) };
      cache.receive(checkpoint);
      cache.receive(checkpoint); // Concurrent subscriptions can deliver the same checkpoint.
      assert.equal(replies(cache).length, 2);
      assert.equal(replies(cache).at(-1)?.historyId, "4");
      assert.equal(replies(cache).at(-1)?.streaming, true);
      const done = { type: "event" as const, session_id: "root", epoch: "server", cursor: 3,
        record: { event: "text_done", run_id: "run", text: answer } };
      cache.receive(done);
      cache.receive(done);
      cache.receive({ type: "event", session_id: "root", epoch: "server", cursor: 4,
        record: { event: "run_finished", run_id: "run", status: "completed" } });
      cache.receive({ type: "snapshot", session_id: "root", epoch: "server", cursor: 5,
        snapshot: snapshot([...history, user, saved]) });
      cache.receive(checkpoint); // A stale reconnect cannot reopen the finished stream.
      assert.deepEqual(replies(cache).map(entry => [entry.historyId, entry.text, !!entry.streaming]),
        [["2", answer, false], ["4", answer, false]]);
      assert.equal(cache.get("root").entries.filter(entry => entry.kind === "live_caption").length, Number(voiceHistory));
    }
    assert.deepEqual(clients[0].get("root"), clients[1].get("root"));
  });
}

test("a saved follow-up reply ahead of replay is claimed by its own later stream, without consuming the earlier equal answer", () => {
  const first = row("1", "user", "First", { message_id: "first" });
  const second = row("3", "user", "Second", { message_id: "second" });
  const history = [first, row("2", "assistant", answer), second, row("4", "assistant", answer)];
  const events: WireEvent[] = [
    { event: "user_message", run_id: "run", history_id: "1", message_id: "first", text: "First" },
    { event: "text_done", run_id: "run", text: answer },
    { event: "user_message", run_id: "run", history_id: "3", message_id: "second", text: "Second" },
  ];
  let state = applySnapshot({ entries: [] }, snapshot(history, events));
  state = applySnapshot(state, snapshot(history, [...events, { event: "text_chunk", run_id: "run", chunk: "The same " }]));
  assert.equal(state.entries.filter(entry => entry.kind === "assistant").length, 2);
  state = applySnapshot(state, snapshot(history, [...events, { event: "text_done", run_id: "run", text: answer }]));
  state = applySnapshot(state, snapshot(history));
  assert.deepEqual(state.entries.filter(entry => entry.kind === "assistant").map(entry => [entry.historyId, entry.text]),
    [["2", answer], ["4", answer]]);
});

test("an empty tool-only completion cannot claim the next saved response slot", () => {
  const history = [row("1", "user", "Look it up", { message_id: "input" }),
    row("2", "assistant", "", { tool_calls: [{ id: "call", name: "read_file", arguments: {} }] }),
    row("3", "tool", "Verified fact", { tool_call_id: "call", name: "read_file" }), row("4", "assistant", answer)];
  const events: WireEvent[] = [
    { event: "user_message", run_id: "run", history_id: "1", message_id: "input", text: "Look it up" },
    { event: "text_done", run_id: "run", text: "" },
    { event: "tool_call", run_id: "run", id: "call", name: "read_file", history_id: "2", call_position: 0 },
    { event: "tool_result", run_id: "run", id: "call", name: "read_file", history_id: "3", result: "Verified fact" },
    { event: "text_chunk", run_id: "run", chunk: "The same " },
  ];
  let state = applySnapshot({ entries: [] }, snapshot(history, events));
  state = applySnapshot(state, snapshot(history, [...events.slice(0, -1), { event: "text_done", run_id: "run", text: answer }]));
  state = applySnapshot(state, snapshot(history));
  assert.deepEqual(state.entries.filter(entry => entry.kind === "assistant").map(entry => [entry.historyId, entry.text]), [["4", answer]]);
  assert.equal(state.entries.filter(entry => entry.kind === "tool").length, 1);
});
