import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { RetainedTask, Snapshot, WireEvent } from "../../types.js";
import { defaultAppearance, type ChatAppearance } from "../settings/appearance.js";
import { appendEvent, fromHistory, groupTranscript, type Entry } from "./transcript.js";
import { applySnapshot, LiveSessions } from "./liveTranscript.js";
import { TranscriptItems } from "./Conversation.js";
import { responseMetadata, type ResponseMetadata } from "./responseMetadata.js";
import { taskEntries } from "../team/TaskConversation.js";
import type { TaskActivity, TaskDetail } from "../team/model.js";

const reportedCounters = { prompt_reported: true, completion_reported: true, total_reported: true };
const metadata = (extra: Partial<ResponseMetadata> = {}): ResponseMetadata => ({
  response_id: "response-one", response_session_id: "root", run_id: "run", revision: 1, round_number: 0, history_id: "",
  status: "thinking", summary: "The provider is checking the available evidence.", summary_truncated: false,
  summary_expired: false, usage_reported: false,
  usage: { ...reportedCounters, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, reasoning_tokens: 0,
    reasoning_reported: false, reasoning_in_output: "unknown" }, ...extra,
});
const update = (value = metadata(), extra: Partial<WireEvent> = {}): WireEvent => ({ event: "response_metadata", run_id: "run", ...value, ...extra });
const text = (event: "text_chunk" | "text_done", value: string, id = "response-one", extra: Partial<WireEvent> = {}): WireEvent => ({
  event, run_id: "run", [event === "text_chunk" ? "chunk" : "text"]: value, extra: { response_id: id }, ...extra,
});
const row = (value: ResponseMetadata, content = "Answer"): Snapshot["history"][number] => ({
  role: value.history_id ? "assistant" : "response_metadata", history_id: value.history_id || `response-${value.response_id}`,
  response_id: value.response_id, response_metadata: value, content, name: "", tool_call_id: "", tool_calls: [],
});
const snapshot = (history: Snapshot["history"]): Snapshot => ({ session_id: "root", history, sessions: [], retained_tasks: [] });
const render = (entries: Entry[], disclosures = new Map<string, boolean>(), appearance?: ChatAppearance) => new JSDOM(renderToStaticMarkup(createElement(TranscriptItems, {
  items: groupTranscript(entries), names: new Map(), disclosures, toggle() {}, appearance,
}))).window.document;

test("reported thinking precedes one answer, with a separate collapsed summary and late usage", () => {
  let entries = appendEvent([], update());
  assert.equal(entries.length, 1); assert.equal(entries[0].text, "");
  const thinking = render(entries);
  assert.equal(thinking.querySelector("details")?.open, false);
  assert.match(thinking.querySelector("summary")?.getAttribute("aria-label") || "", /^Show reasoning:/);
  assert.equal(thinking.querySelector('[role="status"]')?.textContent, "Thinking");
  entries = appendEvent(entries, text("text_chunk", "The "));
  entries = appendEvent(entries, update(metadata({ revision: 2, status: "responding" })));
  entries = appendEvent(entries, text("text_done", "The answer."));
  const done = metadata({ revision: 3, status: "completed", history_id: "42", usage_reported: true,
    usage: { ...reportedCounters, prompt_tokens: 1200, completion_tokens: 800, total_tokens: 2000, reasoning_tokens: 600, reasoning_reported: true, reasoning_in_output: "included" } });
  entries = appendEvent(entries, update(done));
  assert.equal(entries.length, 1); assert.equal(entries[0].text, "The answer.");
  const doc = render(entries);
  assert.equal(doc.querySelector(".entry.assistant > .entry-content")?.textContent, "The answer.");
  assert.equal(doc.querySelector("details")?.open, false);
  assert.equal(doc.querySelector(".response-usage"), null);
  assert.equal(doc.querySelector(".response-reasoning-title")?.textContent, "Reasoning");
  assert.equal(doc.querySelector(".response-reasoning-preview")?.textContent, "The provider is checking the available evidence.");
  assert.equal(doc.querySelector(".response-reasoning > summary")?.getAttribute("aria-label"),
    "Show reasoning: Reasoning. The provider is checking the available evidence.");
  assert.equal(doc.querySelector('[role="status"]'), null, "completed reasoning no longer announces an active thinking state");
  assert.match(doc.querySelector(".response-reasoning-content")?.textContent || "", /The provider is checking the available evidence/);
  assert.doesNotMatch(doc.querySelector(".response-details")?.textContent || "", /600|1,200|800/);
  const expanded = render(entries, new Map([["reasoning:response-one", true]]));
  assert.equal(expanded.querySelector(".response-reasoning")?.hasAttribute("open"), true);
  assert.equal(entries[0].responseMetadata?.usage?.total_tokens, 2000, "Reasoning never inflates the provider total");
  assert.doesNotMatch(entries[0].text, /checking the available evidence/);
});

test("cumulative metadata replaces by revision without repeating summaries or counting twice", () => {
  let entries = appendEvent([], update());
  const newer = update(metadata({ revision: 3, summary: "A complete cumulative summary." }));
  entries = appendEvent(entries, newer);
  const accepted = entries;
  for (const duplicate of [newer, update(), update(metadata({ revision: 2, summary: "Earlier" }))]) entries = appendEvent(entries, duplicate);
  assert.equal(entries, accepted);
  assert.equal(entries[0].responseMetadata?.summary, "A complete cumulative summary.");
});

test("missing reasoning differs from an actual zero in metadata while neither creates a count-only row", () => {
  const counts = metadata({ status: "completed", summary: "", usage_reported: true });
  let doc = render(appendEvent([], update(counts)));
  assert.equal(doc.querySelector("article"), null, "Input/output-only metadata must not leave an empty assistant row");
  assert.equal(doc.querySelector("details"), null);
  const zero = responseMetadata({ ...counts, usage: { ...counts.usage!, reasoning_reported: true } });
  assert.equal(responseMetadata(counts)?.usage?.reasoning_reported, false);
  assert.equal(zero?.usage?.reasoning_reported, true);
  assert.equal(zero?.usage?.reasoning_tokens, 0);
  doc = render(appendEvent([], update(zero!)));
  assert.equal(doc.querySelector("article"), null);
  assert.equal(doc.querySelector(".response-usage"), null);
  assert.equal(render(appendEvent([], update())).querySelector(".response-reasoning-count"), null);
  assert.equal(render(appendEvent([], update({ ...counts, usage_reported: false, usage: { ...counts.usage!, reasoning_reported: true } }))).querySelector("article"), null);
  assert.equal(render(appendEvent([], update(metadata({ status: "responding", summary: "" })))).querySelector("article"), null);
});

test("reasoning-only usage is retained without an inline count or an empty response row", () => {
  const value = metadata({ status: "completed", summary: "", usage_reported: true,
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, reasoning_tokens: 7,
      prompt_reported: false, completion_reported: false, total_reported: false,
      reasoning_reported: true, reasoning_in_output: "unknown" } });
  const entries = appendEvent([], update(value));
  assert.equal(entries[0].responseMetadata?.usage?.reasoning_tokens, 7);
  assert.equal(entries[0].responseMetadata?.usage?.prompt_reported, false);
  assert.equal(entries[0].responseMetadata?.usage?.completion_reported, false);
  const doc = render(entries);
  assert.equal(doc.querySelector("article"), null);
  assert.equal(doc.querySelector(".response-usage"), null);
  const actualZero = { ...value, usage: { ...value.usage!, ...reportedCounters } };
  assert.equal(render(appendEvent([], update(actualZero))).querySelector("article"), null);
  assert.equal(render(appendEvent([], update({ ...value, usage: { ...value.usage!, reasoning_reported: false } }))).querySelector("article"), null);
});

test("old positive counts can be known without flags, but absent zero counts stay unknown", () => {
  const parsed = responseMetadata({ ...metadata(), usage_reported: true, usage: {
    prompt_tokens: 12, completion_tokens: 0, total_tokens: 12, reasoning_tokens: 0,
    reasoning_reported: false, reasoning_in_output: "unknown",
  } });
  assert.equal(parsed?.usage?.prompt_reported, true);
  assert.equal(parsed?.usage?.completion_reported, false);
  const doc = render(appendEvent([], update(parsed!)));
  assert.equal(doc.querySelector(".response-usage"), null);
  assert.equal(doc.querySelector(".response-reasoning-count"), null);
  assert.equal(doc.querySelector(".response-reasoning-title")?.textContent, "Reasoning");
  assert.equal(doc.querySelector('.response-reasoning [role="status"]')?.textContent, "Thinking");
});

test("provider output semantics remain in metadata without decorating the visible reasoning summary", () => {
  for (const meaning of ["included", "separate", "unknown"] as const) {
    const entries = appendEvent([], update(metadata({ status: "completed", usage_reported: true,
      usage: { ...reportedCounters, prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, reasoning_tokens: 3, reasoning_reported: true, reasoning_in_output: meaning } })));
    assert.equal(entries[0].responseMetadata?.usage?.reasoning_in_output, meaning);
    assert.equal(entries[0].responseMetadata?.usage?.total_tokens, 15);
    const doc = render(entries);
    assert.equal(doc.querySelector(".response-reasoning-title")?.textContent, "Reasoning");
    assert.equal(doc.querySelector(".response-usage"), null);
    assert.doesNotMatch(doc.body.textContent || "", /Input|Output|Total|Context/);
  }
});

test("reasoning phases retain their text and status while usage-only terminal frames leave no empty rows", () => {
  for (const summary of ["A provider-supplied summary.", ""]) {
    for (const [status, label] of [["thinking", "Thinking"], ["cancelled", "Stopped"], ["failed", "Interrupted"]] as const) {
      const value = metadata({ status, summary, usage_reported: true,
        usage: { ...reportedCounters, prompt_tokens: 10, completion_tokens: 5, total_tokens: 15,
          reasoning_tokens: 1, reasoning_reported: true, reasoning_in_output: "included" } });
      const doc = render(appendEvent([], update(value)));
      if (summary || status === "thinking" || status === "cancelled" || status === "failed") {
        assert.match(doc.querySelector(".response-details")?.textContent || "", new RegExp(label));
      } else {
        assert.equal(doc.querySelector("article"), null);
      }
      assert.equal(doc.querySelector(".response-details")?.getAttribute("data-phase"), status === "thinking" ? "thinking" : status);
      if (status === "thinking") {
        assert.equal(doc.querySelector('.response-details [role="status"]')?.textContent, "Thinking");
        if (summary) assert.match(doc.querySelector(".response-reasoning > summary")?.getAttribute("aria-label") || "",
          /^Show reasoning: Reasoning\. A provider-supplied summary\.$/);
      } else {
        assert.equal(doc.querySelector(".response-interrupted")?.getAttribute("role"), "status");
        assert.equal(doc.querySelector(".response-interrupted")?.textContent, label);
      }
      assert.equal(doc.querySelector(".response-reasoning-count"), null);
      assert.equal(doc.querySelector(".response-usage"), null);
      assert.equal(!!doc.querySelector(".response-thinking"), status === "thinking");
    }
  }
});

test("multiple tool rounds bind late usage to the exact earlier response", () => {
  let entries = appendEvent([], update(metadata({ summary: "First round" })));
  entries = appendEvent(entries, text("text_done", "I will inspect it."));
  entries = appendEvent(entries, { event: "tool_call", run_id: "run", id: "call", name: "read_file", arguments: { path: "a" }, extra: { response_id: "response-one" } });
  entries = appendEvent(entries, update(metadata({ response_id: "response-two", round_number: 1, summary: "Second round" })));
  entries = appendEvent(entries, text("text_done", "The result.", "response-two"));
  entries = appendEvent(entries, update(metadata({ revision: 2, status: "completed", summary: "First round", usage_reported: true,
    usage: { ...reportedCounters, prompt_tokens: 40, completion_tokens: 9, total_tokens: 49, reasoning_tokens: 7, reasoning_reported: true, reasoning_in_output: "included" } })));
  assert.deepEqual(entries.filter(entry => entry.kind === "assistant").map(entry => [entry.responseId, entry.text, entry.responseMetadata?.summary]), [
    ["response-one", "I will inspect it.", "First round"], ["response-two", "The result.", "Second round"],
  ]);
  assert.equal(entries.find(entry => entry.responseId === "response-two")?.responseMetadata?.usage_reported, false);
});

test("reasoning metadata does not split a saved tool call from its result across checkpoints", () => {
  const first = metadata({ response_id: "tool-response", history_id: "2", status: "completed", summary: "Inspecting the workspace." });
  const final = metadata({ response_id: "answer-response", history_id: "4", round_number: 1, status: "completed" });
  let entries = appendEvent([], { event: "user_message", run_id: "run", history_id: "1", text: "Inspect workspace" });
  entries = appendEvent(entries, update({ ...first, history_id: "", status: "thinking" }));
  entries = appendEvent(entries, { event: "tool_call", run_id: "run", id: "call", name: "list_files", arguments: {}, transcript_event_id: "call-event" });
  entries = appendEvent(entries, { event: "transcript_anchor", run_id: "run", history_id: "2", calls: [{ event_id: "call-event", call_position: 0 }] });
  entries = appendEvent(entries, { event: "tool_result", run_id: "run", history_id: "3", id: "call", name: "list_files", result: "README.md" });
  entries = appendEvent(entries, update({ ...first, revision: 2 }));
  entries = appendEvent(entries, text("text_done", "Found README.md.", "answer-response"));
  entries = appendEvent(entries, update(final));
  const saved = snapshot([
    { role: "user", history_id: "1", content: "Inspect workspace", name: "", tool_call_id: "", tool_calls: [] },
    { ...row({ ...first, revision: 2 }, ""), tool_calls: [{ id: "call", name: "list_files", arguments: {} }] },
    { role: "tool", history_id: "3", content: "README.md", name: "list_files", tool_call_id: "call", tool_calls: [] },
    row(final, "Found README.md."),
  ]);
  const cold = fromHistory(saved);
  let hydrated = applySnapshot({ entries }, saved);
  hydrated = applySnapshot(hydrated, saved);
  for (const transcript of [cold, hydrated.entries]) {
    const tools = transcript.filter(entry => entry.kind === "tool");
    assert.equal(tools.length, 1);
    assert.equal(tools[0].title, "list_files");
    assert.equal(tools[0].result, "README.md");
    assert.equal(tools[0].historyId, "2");
    assert.equal(tools[0].resultHistoryId, "3");
    assert.equal(transcript.filter(entry => entry.kind === "assistant").length, 2);
  }
});

test("history reload and repeated active checkpoints retain one response and the newest usage", () => {
  const finished = metadata({ revision: 4, status: "completed", history_id: "42", usage_reported: true,
    usage: { ...reportedCounters, prompt_tokens: 20, completion_tokens: 8, total_tokens: 28, reasoning_tokens: 0, reasoning_reported: true, reasoning_in_output: "included" } });
  const replay = [update(), text("text_chunk", "Answer"), text("text_done", "Answer"), update(finished)];
  const frame = { ...snapshot([row(finished)]), active_run: { id: "run", status: "running", events: replay } };
  let state = applySnapshot({ entries: [] }, frame);
  const id = state.entries[0].id;
  for (let count = 0; count < 3; count++) state = applySnapshot(state, frame);
  assert.equal(state.entries.length, 1); assert.equal(state.entries[0].id, id);
  assert.equal(state.entries[0].responseMetadata?.revision, 4);
  assert.deepEqual(fromHistory(snapshot([row(finished)])).map(entry => [entry.text, entry.responseMetadata]), [["Answer", finished]]);
  state = applySnapshot(state, snapshot([row({ ...finished, revision: 3, usage_reported: false })]));
  assert.equal(state.entries[0].responseMetadata?.usage_reported, true, "An older HTTP snapshot cannot roll back late usage");
});

test("cancelled reasoning-only response hydrates as display evidence without creating answer text", () => {
  let entries = appendEvent([], update());
  entries = appendEvent(entries, { event: "run_finished", run_id: "run", status: "cancelled" });
  assert.equal(entries[0].responseMetadata?.status, "cancelled");
  const cancelled = metadata({ revision: 2, status: "cancelled" });
  entries = appendEvent(entries, update(cancelled));
  const hydrated = applySnapshot({ entries }, snapshot([row(cancelled, "")]));
  assert.equal(hydrated.entries.length, 1); assert.equal(hydrated.entries[0].text, "");
  assert.equal(render(hydrated.entries).querySelector(".response-reasoning-title")?.textContent, "Reasoning");
  assert.equal(render(hydrated.entries).querySelector(".response-interrupted")?.textContent, "Stopped");
  assert.equal(render(hydrated.entries).querySelector(".response-thinking"), null);
});

test("response identity separates parallel root and child work and later activations", () => {
  let entries: Entry[] = [];
  for (const [response_id, response_session_id, session_id, activation] of [["root-answer", "root", "", 0], ["child-answer", "child", "worker", 0], ["child-next", "child", "worker", 1]] as const) {
    entries = appendEvent(entries, update(metadata({ response_id, response_session_id, summary: response_id }), { session_id, activation }));
    entries = appendEvent(entries, text("text_done", `Answer ${response_id}`, response_id, { session_id, activation }));
  }
  entries = appendEvent(entries, update(metadata({ response_id: "child-answer", response_session_id: "child", revision: 2, status: "completed", summary: "Late child summary" }), { session_id: "worker", activation: 0 }));
  assert.equal(entries.length, 3);
  assert.deepEqual(entries.map(entry => [entry.text, entry.responseMetadata?.summary]), [
    ["Answer root-answer", "root-answer"], ["Answer child-answer", "Late child summary"], ["Answer child-next", "child-next"],
  ]);
});

test("session subscription caches cannot transfer reasoning metadata to another conversation", () => {
  const sessions = new LiveSessions();
  sessions.receive({ type: "event", session_id: "root", cursor: 1, epoch: "host", record: update() });
  sessions.receive({ type: "event", session_id: "other", cursor: 1, epoch: "host", record: update(metadata({ response_id: "other-response", response_session_id: "other", summary: "Other summary" })) });
  assert.equal(sessions.get("root").entries[0].responseMetadata?.summary, metadata().summary);
  assert.equal(sessions.get("other").entries[0].responseMetadata?.summary, "Other summary");
});

test("focused child conversation displays only its own safe metadata and keeps later activations separate", () => {
  const task: RetainedTask = { session_id: "worker", title: "Worker", parent_session_id: "", status: "running", result: "", error: "", prompt: "Check it",
    root_session_id: "root",   depth: 1, profile: "assistant", mode: "build", followups: 0, activation: 0, trigger: "delegation" };
  const activity = (sequence: number, payload: TaskActivity["payload"], activation = 0): TaskActivity => ({ event: "task_activity", root_session_id: "root", parent_session_id: "", session_id: "worker", activation, sequence, timestamp: sequence, payload });
  const first = metadata({ response_id: "child-one", response_session_id: "worker", status: "completed", history_id: "child-row" });
  const second = metadata({ response_id: "child-two", response_session_id: "worker", summary: "New activation" });
  const detail: TaskDetail = { task, history: [row(first, "Saved child answer")], activity_cursor: 1 };
  const entries = taskEntries(detail, [
    activity(2, { type: "response_metadata", ...first }),
    activity(7, { type: "text_done", text: "Saved child answer", response_id: "child-one", response_session_id: "worker" }),
    activity(3, { type: "response_metadata", ...metadata({ response_id: "forged", response_session_id: "sibling" }) }),
    activity(4, { type: "reasoning_chunk", chunk: "Raw protocol text" }),
    activity(5, { type: "response_metadata", ...second }, 1),
    activity(6, { type: "text_done", text: "New answer", response_id: "child-two" }, 1),
  ]);
  assert.deepEqual(entries.map(entry => [entry.text, entry.responseMetadata?.summary]), [["Saved child answer", first.summary], ["New answer", second.summary]]);
  assert.ok(entries.every(entry => !entry.taskId));
});

test("reliable run usage stays in response metadata without duplicate chat counters", () => {
  const runUsage = { ...reportedCounters, prompt_tokens: 30, completion_tokens: 12, total_tokens: 42, reasoning_tokens: 7,
    reasoning_reported: true, reasoning_in_output: "included" as const, response_count: 2,
    usage_response_count: 2, reasoning_response_count: 2, prompt_response_count: 2, completion_response_count: 2, total_response_count: 2, complete: true };
  const first = metadata({ response_id: "first", history_id: "41", status: "completed", run_usage: runUsage });
  const second = metadata({ response_id: "second", history_id: "42", round_number: 1, status: "completed", run_usage: runUsage });
  const state = applySnapshot({ entries: [] }, snapshot([row(first, "Before the tool."), row(second, "Final answer.")]));
  const again = applySnapshot(state, snapshot([row(first, "Before the tool."), row(second, "Final answer.")]));
  const doc = render(again.entries, new Map(), defaultAppearance);
  assert.equal(doc.querySelector(".response-run-usage"), null);
  assert.equal(doc.querySelector('[aria-label="Provider-reported run tokens"]'), null);
  assert.equal(again.entries[1].responseMetadata?.run_usage?.total_tokens, 42);
});

test("partial run usage states coverage and never presents partial reasoning as a complete total", () => {
  const value = metadata({ status: "completed", history_id: "42", run_usage: { ...reportedCounters, prompt_tokens: 30, completion_tokens: 12,
    total_tokens: 42, reasoning_tokens: 7, reasoning_reported: false, reasoning_in_output: "unknown", response_count: 3,
    usage_response_count: 2, reasoning_response_count: 1, prompt_response_count: 2, completion_response_count: 2, total_response_count: 2, complete: false } });
  const doc = render(fromHistory(snapshot([row(value)])), new Map(), defaultAppearance);
  assert.equal(doc.querySelector(".response-run-usage"), null);
  assert.equal(doc.querySelector('[aria-label="Provider-reported run tokens"]'), null);
  assert.equal(responseMetadata(value)?.run_usage?.prompt_response_count, 2);
  assert.equal(responseMetadata(value)?.run_usage?.reasoning_response_count, 1);
});

test("run counter coverage preserves reported zero while hiding completely absent counters", () => {
  const value = metadata({ status: "completed", history_id: "42", run_usage: { ...reportedCounters,
    prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, reasoning_tokens: 7, reasoning_reported: false,
    reasoning_in_output: "unknown", response_count: 3, usage_response_count: 3, reasoning_response_count: 1,
    prompt_response_count: 2, completion_response_count: 0, total_response_count: 0, complete: false } });
  const doc = render(fromHistory(snapshot([row(value)])), new Map(), defaultAppearance);
  assert.equal(doc.querySelector('[aria-label="Provider-reported run tokens"]'), null);
  assert.equal(value.run_usage?.prompt_tokens, 0, "reported zero remains in the retained metadata");
  assert.equal(responseMetadata(value)?.run_usage?.prompt_response_count, 2);
  assert.equal(responseMetadata(value)?.run_usage?.completion_response_count, 0);
  assert.equal(responseMetadata(value)?.run_usage?.reasoning_response_count, 1);
});

test("queued followups with reset round numbers show the latest response's cumulative run usage", () => {
  let entries: Entry[] = [];
  for (const [index, round] of [0, 1, 0].entries()) {
    const responseId = `response-${index}`;
    const value = metadata({ response_id: responseId, round_number: round, status: "completed", history_id: String(index + 40),
      run_usage: { ...reportedCounters, prompt_tokens: (index + 1) * 10, completion_tokens: (index + 1) * 5, total_tokens: (index + 1) * 15,
        reasoning_tokens: index + 1, reasoning_reported: true, reasoning_in_output: "included", response_count: index + 1,
        usage_response_count: index + 1, reasoning_response_count: index + 1, prompt_response_count: index + 1, completion_response_count: index + 1, total_response_count: index + 1, complete: true } });
    entries = appendEvent(entries, text("text_done", `Answer ${index}`, responseId));
    entries = appendEvent(entries, update(value));
  }
  // A late snapshot updates an earlier response in place; it must not take over
  // the run overview merely because its generation had a larger round number.
  entries = appendEvent(entries, update({ ...entries[1].responseMetadata!, revision: 2 }));
  const doc = render(entries, new Map(), defaultAppearance);
  assert.equal(doc.querySelector(".response-run-usage"), null);
  assert.equal(doc.querySelector('[aria-label="Provider-reported run tokens"]'), null);
  assert.equal(entries[2].responseMetadata?.run_usage?.total_tokens, 45,
    "a late older response cannot replace the newest run metadata");
});

test("bounded safe summaries never render protocol objects or fabricate content from raw reasoning", () => {
  const parsed = responseMetadata({ ...metadata(), summary: "x".repeat(17000), encrypted_content: "secret", signature: "opaque" });
  assert.equal(parsed?.summary.length, 16384); assert.equal(parsed?.summary_truncated, true);
  assert.equal(responseMetadata({ ...metadata(), summary: { encrypted_content: "secret" } }), undefined);
  assert.deepEqual(appendEvent([], { event: "reasoning_chunk", chunk: "Never render raw" }), []);
  const doc = render(appendEvent([], update(metadata({ summary: '<script>alert("x")</script>' }))));
  assert.equal(doc.querySelector("script"), null);
  assert.match(doc.querySelector("details")?.textContent || "", /<script>/);
});

test("retained usage survives expired summary without bringing the old text back on refresh", () => {
  const old = metadata({ history_id: "42", revision: 5, status: "completed", usage_reported: true,
    usage: { ...reportedCounters, prompt_tokens: 1200, completion_tokens: 800, total_tokens: 2000,
      reasoning_tokens: 600, reasoning_reported: true, reasoning_in_output: "included" } });
  const expired = { ...old, summary: "", summary_expired: true };
  const state = applySnapshot({ entries: appendEvent([], update(old)) }, snapshot([row(expired)]));
  assert.equal(state.entries[0].responseMetadata?.summary, "");
  const doc = render(state.entries);
  assert.equal(doc.querySelector("details"), null);
  assert.match(doc.querySelector(".response-reasoning-expired")?.getAttribute("title") || "", /latest 100 responses/);
  assert.equal(doc.querySelector(".response-reasoning-meta")?.textContent, "Summary no longer retained");
  assert.ok(doc.querySelector(".response-reasoning-expired"), "summary expiry remains visible without the old body");
  assert.equal(state.entries[0].responseMetadata?.usage?.reasoning_tokens, 600);
  assert.equal(doc.querySelector(".response-usage"), null);
});
