import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { Snapshot } from "../../types.js";
import { defaultAppearance, type ChatAppearance } from "../settings/appearance.js";
import { agentLabels } from "./agentLabels.js";
import { Conversation } from "./Conversation.js";
import { fromHistory, type Entry } from "./transcript.js";

const assistant = (id: string, text: string, extra: Partial<Entry> = {}): Entry => ({
  id, kind: "assistant", text, ...extra,
});
const user = (id: string, text: string, extra: Partial<Entry> = {}): Entry => ({
  id, kind: "user", text, ...extra,
});

function render(entries: Entry[], extra: Partial<Parameters<typeof Conversation>[0]> = {}) {
  const html = renderToStaticMarkup(createElement(Conversation, {
    entries,
    sessionId: "root",
    demo: false,
    canSubmit: false,
    submit() {},
    ...extra,
  }));
  return new JSDOM(html).window.document;
}

function visibleBylines(document: Document): Array<[string, string]> {
  return [...document.querySelectorAll<HTMLElement>(".entry.assistant > .entry-label:not([hidden])")]
    .map(label => [label.closest<HTMLElement>(".entry.assistant")?.dataset.recordKey || "", label.textContent?.trim() || ""]);
}

test("one visible byline spans assistant blocks and queued notes, then returns for the next admitted turn", () => {
  const entries = [
    user("user-one", "First question"),
    assistant("answer-one-a", "First response block", { runId: "run-one" }),
    assistant("answer-one-b", "Second response block", { runId: "run-one" }),
    user("queued-note", "More detail while the response is running", { queued: true, admission: "queued" }),
    assistant("answer-one-c", "Continued response", { runId: "run-one" }),
    user("user-two", "Next question"),
    assistant("answer-two", "Next turn response", { runId: "run-two" }),
  ];
  const document = render(entries, { agentName: "Configured Architect" });

  assert.deepEqual(visibleBylines(document), [
    ["answer-one-a", "Configured Architect"],
    ["answer-two", "Configured Architect"],
  ]);
  assert.ok(document.querySelector('[data-record-key="answer-one-b"] .entry-label[hidden]'));
  assert.ok(document.querySelector('[data-record-key="answer-one-c"] .entry-label[hidden]'));
});

test("hidden reasoning does not consume the visible byline for its admitted run", () => {
  const hiddenReasoning: ChatAppearance = {
    ...defaultAppearance,
    activity: {
      ...defaultAppearance.activity,
      reasoning: { placement: "hidden", details: "collapsed" },
    },
  };
  const entries = [
    user("user", "Question"),
    assistant("reasoning-only", "", {
      runId: "run-one",
      responseId: "response-one",
      responseMetadata: {
        response_id: "response-one", response_session_id: "root", run_id: "run-one", revision: 1,
        round_number: 0, history_id: "", status: "thinking", summary: "A provider summary",
        summary_truncated: false, summary_expired: false, usage_reported: false,
      },
    }),
    assistant("answer", "Visible response", { runId: "run-one" }),
  ];
  const document = render(entries, { agentName: "Configured Reviewer", appearance: hiddenReasoning });

  assert.deepEqual(visibleBylines(document), [["answer", "Configured Reviewer"]]);
  assert.equal(document.querySelector('[data-record-key="reasoning-only"]'), null);
});

test("child and nested child labels name their immediate parent and target", () => {
  const entries = [
    assistant("child-answer", "Research result", {
      taskId: "research-task", parentTaskId: "assistant", runId: "child-run",
    }),
    assistant("nested-answer", "Security result", {
      taskId: "security-task", parentTaskId: "research-task", runId: "nested-run",
    }),
  ];
  const labels = agentLabels(entries, "Coordinator", new Map([
    ["research-task", "Researcher"],
    ["security-task", "Security Reviewer"],
  ]));

  assert.equal(labels.get("child-answer"), "Coordinator → Researcher");
  assert.equal(labels.get("nested-answer"), "Researcher → Security Reviewer");

  const childDocument = render([assistant("direct-child", "Child answer", { runId: "child-run" })], {
    agentName: "Researcher",
    parentAgentName: "Coordinator",
  });
  assert.deepEqual(visibleBylines(childDocument), [["direct-child", "Coordinator → Researcher"]]);
});

const historyRow = (
  history_id: string,
  role: string,
  content: string,
  extra: Partial<Snapshot["history"][number]> = {},
): Snapshot["history"][number] => ({
  history_id, role, content, name: "", tool_call_id: "", tool_calls: [], ...extra,
});

test("saved history uses the previous agent before the change and current agent after it", () => {
  const entries = fromHistory({
    session_id: "root",
    retained_tasks: [],
    history: [
      historyRow("1", "user", "First request"),
      historyRow("2", "assistant", "First response block"),
      historyRow("3", "assistant", "Second response block"),
      historyRow("4", "user", "Second request"),
      historyRow("agent-change-1", "agent_changed", "", { agent: "Reviewer", previous_agent: "Assistant" }),
      historyRow("5", "assistant", "Reviewed response"),
    ],
  });
  const assistants = entries.filter(entry => entry.kind === "assistant");
  const document = render(entries, { agentName: "Reviewer" });

  assert.deepEqual(assistants.map(entry => entry.historyTurn), [0, 0, 3]);
  assert.deepEqual(visibleBylines(document), [
    [assistants[0].id, "Assistant"],
    [assistants[2].id, "Reviewer"],
  ]);
  assert.equal(entries.find(entry => entry.kind === "agent_change")?.text, "Agent: Reviewer");
});


test("non-displayed response metadata and whitespace messages cannot consume the answer's byline", () => {
  for (const hiddenText of ["", " \n\t"]) for (const summary of ["", " \n\t", "##", "** **"]) {
    const entries = [user("user", "Question"), assistant("metadata", hiddenText, { runId: "run", responseId: "response",
      responseMetadata: { response_id: "response", response_session_id: "root", run_id: "run", revision: 1,
        round_number: 0, history_id: "", status: "thinking", summary, summary_truncated: false,
        summary_expired: false, usage_reported: true } }), assistant("answer", "The visible answer.", { runId: "run" })];
    const document = render(entries, { agentName: "Configured Reviewer" });
    assert.equal(document.querySelector('[data-record-key="metadata"]'), null);
    assert.deepEqual(visibleBylines(document), [["answer", "Configured Reviewer"]]);
    assert.equal(entries[1]!.text, hiddenText);
  }
});

test("whitespace assistant text does not hide attachments live content or interruption outcomes", () => {
  const upload = { upload_id: "upload", filename: "report.pdf", media_type: "application/pdf", byte_length: 1024 };
  const entries: Entry[] = [
    assistant("attachment", " \n", { runId: "attachment-run", uploads: [upload] }),
    { id: "caption", kind: "live_caption", text: " \n", liveCaption: { sessionId: "voice", seq: 1, speaker: "assistant", start: 0, end: 100, anchorHistoryId: "" } },
    ...(["cancelled", "failed"] as const).map(status => assistant(status, " \n", { runId: `${status}-run`,
      responseMetadata: { response_id: status, response_session_id: "root", revision: 1, round_number: 0, history_id: "",
        status, summary: "", summary_truncated: false, summary_expired: false, usage_reported: false } })),
  ];
  const document = render(entries);
  const attachment = document.querySelector('[data-record-key="attachment"]')!;
  assert.ok(attachment);
  assert.match(attachment.textContent || "", /report\.pdf/);
  assert.equal(attachment.querySelector(".entry-content"), null);
  assert.equal(attachment.classList.contains("reasoning-only"), false);
  assert.ok(document.querySelector('[data-record-key="caption"] .chat-live-badge'));
  for (const [id, text] of [["cancelled", "Stopped"], ["failed", "Interrupted"]]) {
    const article = document.querySelector(`[data-record-key="${id}"]`)!;
    assert.ok(article);
    assert.match(article.textContent || "", new RegExp(text!));
    assert.equal(article.querySelector(".entry-content"), null);
    assert.equal(article.classList.contains("reasoning-only"), true);
  }
});
