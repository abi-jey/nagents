import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { appearancePresets, defaultAppearance } from "../settings/appearance.js";
import { Conversation } from "./Conversation.js";
import type { ResponseMetadata } from "./responseMetadata.js";
import type { Entry, TaskThread } from "./transcript.js";
import type { LocalDelivery } from "./deliveries.js";

const entry = (id: string, kind: Entry["kind"], text = "", extra: Partial<Entry> = {}): Entry => ({ id, kind, text, ...extra });
const user = (id: string, text: string) => entry(id, "user", text, { messageId: `message-${id}` });
const tool = (id: string, values: Partial<Entry> = {}) => entry(id, "tool", "", {
  title: "run_shell_command", inputs: JSON.stringify({ command: id }), callId: id, state: "Running", streaming: true,
  runId: "run", ...values,
});
const preset = (id: string) => appearancePresets.find(value => value.id === id)!.value;

function browser() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const render = (entries: Entry[], appearance = defaultAppearance, renderTask?: (thread: TaskThread) => ReactNode) => act(async () => root.render(createElement(Conversation, {
    entries, appearance, renderTask, sessionId: "root", demo: false, canSubmit: false, submit() {},
  })));
  return {
    dom, container, render,
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"])
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    },
  };
}

test("Compact keeps messages and preserves grouped tool rows, open details, and scroll through completion and hydration", async () => {
  const ui = browser();
  const prompt = user("question", "Keep the user request visible");
  const answer = entry("answer", "assistant", "Answer stays visible");
  const first = tool("tool-one");
  try {
    await ui.render([prompt, answer, first]);
    assert.equal(ui.container.querySelector('[data-record-key="question"]')?.textContent?.includes(prompt.text), true);
    assert.equal(ui.container.querySelector('[data-record-key="answer"]')?.textContent?.includes(answer.text), true);
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="shell"]')!;
    assert.equal(group.open, false, "Compact groups start collapsed");
    await act(async () => { group.open = true; group.dispatchEvent(new ui.dom.window.Event("toggle")); });
    const firstArticle = ui.container.querySelector<HTMLElement>('[data-record-key="tool-one"]')!;
    const firstDetails = firstArticle.querySelector<HTMLDetailsElement>("details.execution-record")!;
    await act(async () => { firstDetails.open = true; firstDetails.dispatchEvent(new ui.dom.window.Event("toggle")); });

    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    Object.defineProperty(feed, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(feed, "scrollHeight", { configurable: true, value: 500 });
    feed.scrollTop = 150;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));

    const second = tool("tool-two");
    await ui.render([prompt, answer, first, second]);
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="shell"]'), group);
    assert.equal(group.open, true);
    assert.equal(ui.container.querySelector('[data-record-key="tool-one"]'), firstArticle);
    assert.equal(firstDetails.open, true);
    assert.equal(group.querySelectorAll("article.entry.tool").length, 2);
    assert.equal(feed.scrollTop, 150);

    const completed = { ...first, state: "Completed", streaming: false, text: "first output\n", result: "first output\n" };
    await ui.render([prompt, answer, completed, second]);
    assert.equal(ui.container.querySelector('[data-record-key="tool-one"]'), firstArticle);
    assert.equal(firstDetails.open, true);
    assert.match(firstArticle.textContent || "", /first output/);

    const hydrated = { ...completed, recorded: true, historyId: "saved-call-one", resultHistoryId: "saved-result-one" };
    await ui.render([prompt, answer, hydrated, second]);
    assert.equal(ui.container.querySelector('[data-record-key="tool-one"]'), firstArticle,
      "History hydration must preserve the mounted row identity");
    assert.equal(firstDetails.open, true);
    assert.equal(group.open, true);
    assert.equal(feed.scrollTop, 150);
  } finally { await ui.close(); }
});

test("a later failure changes the collapsed activity group to Needs attention and opens it", async () => {
  const ui = browser();
  const prompt = user("question", "Check this command");
  const first = tool("first-command", { state: "Completed", streaming: false });
  try {
    await ui.render([prompt, first]);
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="shell"]')!;
    assert.equal(group.open, false);
    const error = tool("later-error", { state: "Failed", error: "exit 4", streaming: false });
    await ui.render([prompt, first, error]);
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="shell"]'), group);
    assert.equal(group.dataset.state, "Needs attention");
    assert.equal(group.open, true, "An important failure overrides the collapsed preference");
    assert.ok(group.querySelector('[data-record-key="later-error"]'));
  } finally { await ui.close(); }
});

test("Messages only hides routine activity while errors, warnings, and approvals stay visible; Detailed separates it", async () => {
  const ui = browser();
  const prompt = user("user", "The request remains visible");
  const answer = entry("assistant", "assistant", "The answer remains visible");
  const routine = tool("routine", { state: "Completed", streaming: false });
  const failed = tool("failure", { state: "Failed", error: "Command failed", streaming: false });
  const warning = entry("warning", "status", "A warning remains visible", { level: "warning" });
  const approval = tool("approval", { state: "Waiting for approval", approvalId: "approval-id", streaming: false });
  const denied = tool("denied", { state: "Approval denied", streaming: false });
  const interrupted = tool("interrupted", { state: "Interrupted", streaming: false });
  const missing = tool("missing", { state: "No result recorded", streaming: false });
  try {
    await ui.render([prompt, answer, routine, failed, warning, approval, denied, interrupted, missing], preset("messages"));
    assert.ok(ui.container.querySelector('[data-record-key="user"]'));
    assert.ok(ui.container.querySelector('[data-record-key="assistant"]'));
    assert.equal(ui.container.querySelector('[data-record-key="routine"]'), null);
    for (const id of ["failure", "warning", "approval", "denied", "interrupted", "missing"]) {
      const visible = ui.container.querySelector<HTMLElement>(`[data-record-key="${id}"]`);
      assert.ok(visible, `${id} should remain visible`);
      assert.equal(visible.closest(".chat-activity-group"), null, `${id} stays at the lowest visible level`);
    }

    await ui.render([prompt, answer, routine, tool("routine-two", { streaming: false, state: "Completed" })], preset("detailed"));
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="shell"]'), null);
    assert.ok(ui.container.querySelector('[data-record-key="routine"]'));
    assert.ok(ui.container.querySelector('[data-record-key="routine-two"]'));
    assert.equal(ui.container.querySelector<HTMLDetailsElement>('[data-record-key="routine"] details.execution-record')?.open, true,
      "Detailed opens command details by default");
  } finally { await ui.close(); }
});

test("reasoning stays collapsed until opened, then keeps disclosure through completion and history; run usage is a sibling", async () => {
  const ui = browser();
  const usage = { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20, reasoning_tokens: 3,
    prompt_reported: true, completion_reported: true, total_reported: true, reasoning_reported: true,
    reasoning_in_output: "included" as const, response_count: 2, usage_response_count: 2,
    reasoning_response_count: 2, prompt_response_count: 2, completion_response_count: 2,
    total_response_count: 2, complete: true };
  const responding: ResponseMetadata = { response_id: "response", response_session_id: "root", run_id: "root-run",
    revision: 1, round_number: 0, history_id: "", status: "responding", summary: "A provider explanation.",
    summary_truncated: false, summary_expired: false, usage_reported: true, run_usage: usage };
  const current = entry("current-answer", "assistant", "Partial answer", { runId: "root-run", responseId: "response",
    streaming: true, responseMetadata: responding });
  const child = entry("child-answer", "assistant", "A child result", { runId: "root-run", taskId: "worker" });
  const uiEntries = [user("question", "Question"), current, tool("root-tool", { runId: "root-run", state: "Completed", streaming: false }), child];
  try {
    await ui.render(uiEntries);
    const article = ui.container.querySelector<HTMLElement>('[data-record-key="current-answer"]')!;
    const reasoning = article.querySelector<HTMLDetailsElement>("details.response-reasoning")!;
    assert.equal(reasoning.open, false, "Compact reasoning starts collapsed");
    const footer = ui.container.querySelector<HTMLDetailsElement>("details.response-run-usage")!;
    const childArticle = ui.container.querySelector<HTMLElement>('[data-record-key="child-answer"]')!;
    assert.equal(footer.closest("article.entry"), null, "Run statistics are not nested in a response row");
    const rootActivity = ui.container.querySelector('.chat-activity-group[data-category="shell"]')!;
    assert.ok(rootActivity.compareDocumentPosition(footer) & ui.dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
      "Run statistics follow root activity in their own row");
    assert.ok(footer.compareDocumentPosition(childArticle) & ui.dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
      "Root run statistics do not move into the child response");

    await act(async () => { reasoning.open = true; reasoning.dispatchEvent(new ui.dom.window.Event("toggle")); });
    assert.equal(reasoning.open, true);
    const completedMetadata = { ...responding, revision: 2, history_id: "answer-row", status: "completed" as const };
    const completed = { ...current, text: "The final answer", streaming: false, historyId: "answer-row", responseMetadata: completedMetadata };
    await ui.render([user("question", "Question"), completed, uiEntries[2], child]);
    assert.equal(ui.container.querySelector('[data-record-key="current-answer"]'), article);
    assert.equal(article.textContent?.includes("The final answer"), true);
    assert.equal(article.querySelector("details.response-reasoning"), reasoning);
    assert.equal(reasoning.open, true);

    const hydrated = { ...completed, recorded: true, historyIndex: 1 };
    await ui.render([user("question", "Question"), hydrated, uiEntries[2], child]);
    assert.equal(ui.container.querySelector('[data-record-key="current-answer"]'), article);
    assert.equal(reasoning.open, true, "History hydration preserves the user's disclosure choice");
    assert.equal(ui.container.querySelectorAll("details.response-run-usage").length, 1);
  } finally { await ui.close(); }
});

test("Messages only keeps compact failure outcomes even without assistant text", async () => {
  const ui = browser();
  try {
    for (const [status, visibleStatus] of [["cancelled", "Stopped"], ["failed", "Interrupted"]] as const) {
      const metadata: ResponseMetadata = { response_id: `${status}-response`, response_session_id: "root", run_id: `${status}-run`,
        revision: 1, round_number: 0, history_id: "", status, summary: "A summary hidden by this preset.",
        summary_truncated: false, summary_expired: false, usage_reported: false };
      await ui.render([entry(`${status}-answer`, "assistant", "", { runId: `${status}-run`, responseId: `${status}-response`, responseMetadata: metadata })], preset("messages"));
      const article = ui.container.querySelector<HTMLElement>(`[data-record-key="${status}-answer"]`)!;
      assert.ok(article, `${status} outcome remains visible without an answer body`);
      assert.match(article.textContent || "", new RegExp(visibleStatus));
      assert.doesNotMatch(article.textContent || "", /A summary hidden by this preset|Reasoning summary|Thinking/);
      assert.equal(article.querySelector("details.response-reasoning"), null);
    }
  } finally { await ui.close(); }
});

test("delivery, upload, caption, and registry-only rows stay out of activity groups", async () => {
  const ui = browser();
  const delivery: LocalDelivery = { delivery_id: "delivery", session_id: "root", channel: "builtin.web", text: "Shared file",
    sequence: 1, anchor_message_id: "message", call_position: 0, earlier: false, assets: [] };
  const upload = { upload_id: "upload", filename: "report.pdf", media_type: "application/pdf", byte_length: 1024 };
  const caption: NonNullable<Entry["liveCaption"]> = { sessionId: "voice", seq: 1, speaker: "assistant", start: 0, end: 100, anchorHistoryId: "" };
  const mediaEntries = [
    tool("delivery-tool", { delivery, text: "Shared file" }),
    entry("upload-status", "status", "Attached report", { uploads: [upload], runId: "run" }),
    tool("caption-tool", { liveCaption: caption, text: "Spoken answer" }),
  ];
  try {
    await ui.render(mediaEntries);
    assert.equal(ui.container.querySelectorAll(".chat-activity-group").length, 0);
    for (const id of ["delivery-tool", "upload-status", "caption-tool"]) {
      const article = ui.container.querySelector<HTMLElement>(`[data-record-key="${id}"]`)!;
      assert.ok(article);
      assert.equal(article.closest(".chat-activity-group"), null);
    }
    assert.ok(ui.container.querySelector(".local-delivery"));
    assert.match(ui.container.textContent || "", /report\.pdf/);
    assert.match(ui.container.textContent || "", /Spoken answer/);

    await ui.render([entry("registry", "retained_tasks", "Retained tasks")], defaultAppearance, () => null);
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="agents"]'), null,
      "An empty retained-task registry cannot create an empty Agent activity group");
  } finally { await ui.close(); }
});
