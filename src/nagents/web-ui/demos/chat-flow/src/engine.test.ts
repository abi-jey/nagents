import assert from "node:assert/strict";
import { test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { appendEvent, type Entry } from "../../../src/features/chat/transcript.js";
import { Conversation } from "../../../src/features/chat/Conversation.js";
import { appearancePresets } from "../../../src/features/settings/appearance.js";
import type { ChatAppearance } from "../../../src/features/settings/appearance.js";
import { DURATION, frameAt, nextTime, reasoningParts, timeline } from "./engine.js";

test("scrubbing backwards and replaying reconstructs the exact same event state", () => {
  const complete = frameAt(DURATION);
  const stepped = timeline.reduce((entries, item) => appendEvent(entries, item.event), frameAt(0).entries);
  assert.deepEqual(complete.entries, stepped);
  assert.deepEqual(frameAt(0).entries, []);
  assert.deepEqual(frameAt(DURATION), complete);
  assert.equal(frameAt(-10).time, 0);
  assert.equal(frameAt(99).time, DURATION);
  assert.equal(nextTime(4.3), timeline.find(item => item.at > 4.3001)?.at);
});
test("real transcript reducer keeps the same tool identity from partial arguments to result", () => {
  const partial = frameAt(4.8).entries.find(entry => entry.callId === "call-1")!;
  assert.equal(partial.state, "Preparing");
  assert.throws(() => JSON.parse(partial.inputs!));
  const starting = frameAt(6.4).entries.filter(entry => entry.callId === "call-1");
  const finished = frameAt(8).entries.filter(entry => entry.callId === "call-1");
  assert.equal(starting.length, 1);
  assert.equal(finished.length, 1);
  assert.equal(starting[0].id, partial.id);
  assert.equal(finished[0].id, partial.id);
  assert.equal(starting[0].state, "Starting");
  assert.equal(finished[0].state, "Completed");
  assert.equal(JSON.parse(finished[0].inputs!).path, "src/features/chat/ChatMedia.tsx");
  assert.ok(finished[0].result?.includes("original file"));
});
test("a follow-up waits through running tools and is admitted at the next response boundary", () => {
  const during = frameAt(15);
  const queued = during.entries.find(entry => entry.messageId === "keyboard-note")!;
  assert.equal(queued.queued, true);
  assert.equal(during.entries.find(entry => entry.callId === "call-3")?.result, undefined);
  assert.equal(frameAt(28.8).entries.find(entry => entry.messageId === "keyboard-note")?.queued, true);
  const admitted = frameAt(29.2).entries.filter(entry => entry.messageId === "keyboard-note");
  assert.equal(admitted.length, 1);
  assert.equal(admitted[0].id, queued.id);
  assert.equal(admitted[0].queued, false);
  assert.equal(admitted[0].runId, "keyboard-followup");
});
test("streamed command output and assistant text fill existing rows rather than making messages per chunk", () => {
  const output = frameAt(21).entries.find(entry => entry.callId === "call-4")!;
  const final = frameAt(DURATION);
  assert.equal(output.state, "Receiving output");
  assert.ok(output.text.includes("image is visible"));
  assert.equal(final.entries.filter(entry => entry.callId === "call-4").length, 1);
  assert.equal(final.entries.filter(entry => entry.responseId === "finish").length, 1);
  assert.ok(final.entries.find(entry => entry.responseId === "finish")?.text.includes("tests passed"));
  assert.equal(final.entries.filter(entry => entry.kind === "user").length, 2);
  assert.equal(final.entries.some(entry => entry.streaming), false);
});
test("reasoning heading is extracted once and body is never invented for heading-only snapshots", () => {
  assert.deepEqual(reasoningParts("**Inspect the image**\n\nRead the component."), { title: "Inspect the image", body: "Read the component." });
  assert.deepEqual(reasoningParts("## Inspect the image"), { title: "Inspect the image", body: "" });
  assert.equal(frameAt(2).entries.find(entry => entry.responseId === "inspect")?.responseMetadata?.status, "thinking");
  assert.equal(frameAt(4).entries.find(entry => entry.responseId === "inspect")?.responseMetadata?.status, "responding");
  assert.equal(frameAt(27).entries.find(entry => entry.responseId === "finish")?.streaming, true);
});

test("Explorer works under one short session identity, returns once, and notifies the continuing parent", () => {
  const spawned = frameAt(6.7);
  const explorerRows = (entries: Entry[]) => entries.filter(entry => entry.taskId === "s1");
  const taskRows = spawned.entries.filter(entry => entry.kind === "task" && entry.taskId === "s1");
  assert.equal(taskRows.length, 1);
  assert.equal(taskRows[0].state, "Running");
  assert.deepEqual([...new Set(explorerRows(spawned.entries).map(entry => entry.taskId))], ["s1"]);

  const childWork = frameAt(10.4);
  assert.equal(explorerRows(childWork.entries).filter(entry => entry.kind === "tool" && entry.title === "find").length, 1);
  assert.equal(explorerRows(childWork.entries).filter(entry => entry.responseId === "explore-result").length, 1);

  const returned = frameAt(11.6);
  const completed = returned.entries.filter(entry => entry.kind === "task" && entry.taskId === "s1");
  assert.equal(completed.length, 1, "task start and completion update the same transcript task row");
  assert.equal(completed[0].id, taskRows[0].id);
  assert.equal(completed[0].state, "Completed");
  assert.match(completed[0].result || "", /shared attachment styles/);
  assert.equal(explorerRows(returned.entries).filter(entry => entry.responseId === "explore-result").length, 1,
    "the child answer stays in the child transcript once");
  const delivered = returned.entries.filter(entry => entry.kind === "notification" && entry.sourceTaskId === "s1");
  assert.equal(delivered.length, 1);
  assert.match(delivered[0].text, /narrow-screen footer wrapping rule/);

  const continuing = frameAt(14.8);
  assert.ok(continuing.entries.some(entry => entry.responseId === "plan" && !entry.taskId && entry.text.includes("image renderer")),
    "the parent continues its answer after the child begins");
  assert.equal(continuing.entries.filter(entry => entry.messageId === "keyboard-note").length, 1);
  assert.equal(continuing.entries.find(entry => entry.messageId === "keyboard-note")?.queued, true);
  assert.equal(continuing.entries.filter(entry => entry.callId === "call-3").length, 1,
    "the parent's edit tool keeps one call identity while the follow-up waits");
});

test("production Conversation renders the same replay frame at all five appearance levels", () => {
  const frame = frameAt(21.6);
  const render = (appearance: ChatAppearance) => new JSDOM(renderToStaticMarkup(createElement(Conversation, {
    entries: frame.entries,
    appearance,
    agentName: "Assistant",
    sessionId: "chat-flow-demo",
    demo: false,
    canSubmit: false,
    submit() {},
  }))).window.document;
  const preset = (id: string) => appearancePresets.find(value => value.id === id)!.value;
  const preserveMessages = (document: Document) => {
    for (const entry of frame.entries.filter(value => value.kind === "user" ||
      value.kind === "assistant" && !value.taskId && !!value.text)) {
      const article = document.querySelector<HTMLElement>(`[data-record-key="${entry.id}"]`);
      assert.ok(article, `${entry.kind} message ${entry.id} remains visible`);
      assert.ok(article.textContent?.includes(entry.text), `${entry.kind} text remains readable`);
    }
  };

  const messages = render(preset("messages"));
  preserveMessages(messages);
  assert.equal(messages.querySelector(".chat-activity-group"), null);
  assert.equal(messages.querySelector("article.entry.tool"), null);
  assert.equal(messages.querySelector("details.response-reasoning"), null);

  const quiet = render(preset("quiet"));
  preserveMessages(quiet);
  assert.deepEqual([...new Set([...quiet.querySelectorAll<HTMLElement>(".chat-activity-group")].map(group => group.dataset.category))].sort(),
    ["agents", "files"]);
  assert.equal(quiet.querySelector('.chat-activity-group[data-category="shell"]'), null);
  assert.equal(quiet.querySelector('.chat-activity-group[data-category="tools"]'), null);
  assert.ok([...quiet.querySelectorAll<HTMLDetailsElement>(".chat-activity-group")].every(group => !group.open));

  const compact = render(preset("compact"));
  preserveMessages(compact);
  assert.ok(compact.querySelectorAll(".chat-activity-group").length > 0);
  assert.ok([...compact.querySelectorAll<HTMLDetailsElement>(".chat-activity-group")].every(group => !group.open));
  assert.ok([...compact.querySelectorAll<HTMLDetailsElement>("details.execution-record")].every(details => !details.open));
  assert.ok([...compact.querySelectorAll<HTMLDetailsElement>("details.response-reasoning")].every(details => !details.open));

  const detailed = render(preset("detailed"));
  preserveMessages(detailed);
  assert.equal(detailed.querySelector('.chat-activity-group[data-category="shell"]'), null);
  assert.equal(detailed.querySelector('.chat-activity-group[data-category="files"]'), null);
  for (const callId of ["call-3", "call-4"]) {
    const row = frame.entries.find(entry => entry.callId === callId)!;
    const article = detailed.querySelector<HTMLElement>(`[data-record-key="${row.id}"]`);
    assert.ok(article?.querySelector<HTMLDetailsElement>("details.execution-record")?.open, `${callId} opens by default`);
  }

  const everything = render(preset("everything"));
  preserveMessages(everything);
  assert.equal(everything.querySelector(".chat-activity-group"), null);
  assert.ok([...everything.querySelectorAll<HTMLDetailsElement>("details.execution-record")].every(details => details.open));
  assert.ok([...everything.querySelectorAll<HTMLDetailsElement>("details.response-reasoning")].every(details => details.open));
});
