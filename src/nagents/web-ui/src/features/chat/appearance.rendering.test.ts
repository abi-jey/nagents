import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { appearancePresets, defaultAppearance } from "../settings/appearance.js";
import { Conversation } from "./Conversation.js";
import type { ResponseMetadata } from "./responseMetadata.js";
import { appendEvent, type Entry, type TaskThread } from "./transcript.js";
import type { WireEvent } from "../../types.js";
import type { LocalDelivery } from "./deliveries.js";

const entry = (id: string, kind: Entry["kind"], text = "", extra: Partial<Entry> = {}): Entry => ({ id, kind, text, ...extra });
const user = (id: string, text: string) => entry(id, "user", text, { messageId: `message-${id}` });
const tool = (id: string, values: Partial<Entry> = {}) => entry(id, "tool", "", {
  title: "run_shell_command", inputs: JSON.stringify({ command: id }), callId: id, state: "Running", streaming: true,
  runId: "run", ...values,
});
const preset = (id: string) => appearancePresets.find(value => value.id === id)!.value;

function browser(strict = false) {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const render = (entries: Entry[], appearance = defaultAppearance, renderTask?: (thread: TaskThread) => ReactNode) => act(async () => {
    const conversation = createElement(Conversation, {
      entries, appearance, renderTask, sessionId: "root", demo: false, canSubmit: false, submit() {},
    });
    root.render(strict ? createElement(StrictMode, null, conversation) : conversation);
  });
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

function groupRecords(group: HTMLDetailsElement): HTMLElement[] {
  const controls = group.querySelector("summary")?.getAttribute("aria-controls")?.split(" ") || [];
  assert.equal(controls.length, Number(group.querySelector(".chat-activity-count")?.textContent));
  return controls.map(id => {
    const record = group.ownerDocument.getElementById(id)!;
    assert.ok(record, "each group member has an accessible controlled record");
    assert.equal(record.dataset.activityGroup, group.dataset.recordKey);
    assert.equal(record.hidden, !group.open, "closed groups hide every member without unmounting it");
    return record;
  });
}
function groupElements<T extends Element>(group: HTMLDetailsElement, selector: string): T[] {
  return groupRecords(group).flatMap(record => [...record.querySelectorAll<T>(selector)]);
}

const toolsDetails = (details: "collapsed" | "expanded") => ({
  ...defaultAppearance,
  activity: { ...defaultAppearance.activity, tools: { placement: "grouped" as const, details } },
});

test("appearance defaults update mounted disclosures while explicit opens survive later changes", async () => {
  const ui = browser();
  const prompt = user("question", "Run the check");
  const command = tool("command");
  try {
    await ui.render([prompt, command], toolsDetails("expanded"));
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')!;
    const article = ui.container.querySelector<HTMLElement>('[data-record-key="command"]')!;
    const details = article.querySelector<HTMLDetailsElement>("details.execution-record")!;
    assert.equal(group.open, true);
    assert.equal(details.open, true);

    // With no user toggles, changing the default disclosure policy affects the
    // mounted nodes and does not get mistaken for a user preference.
    await ui.render([prompt, command], toolsDetails("collapsed"));
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), group);
    assert.equal(ui.container.querySelector('[data-record-key="command"]'), article);
    assert.equal(article.querySelector("details.execution-record"), details);
    assert.equal(group.open, false);
    assert.equal(details.open, false);

    await ui.render([prompt, command], toolsDetails("expanded"));
    assert.equal(group.open, true, "an untouched group follows the expanded default again");
    assert.equal(details.open, true, "an untouched tool disclosure follows the expanded default again");
    await ui.render([prompt, command], toolsDetails("collapsed"));
    assert.equal(group.open, false);
    assert.equal(details.open, false);

    // Simulate native user toggles and let React flush the toggle handlers.
    await act(async () => {
      group.open = true;
      group.dispatchEvent(new ui.dom.window.Event("toggle"));
    });
    await act(async () => {
      details.open = true;
      details.dispatchEvent(new ui.dom.window.Event("toggle"));
    });
    assert.equal(group.open, true);
    assert.equal(details.open, true);
    await ui.render([prompt, command], toolsDetails("expanded"));
    await ui.render([prompt, command], toolsDetails("collapsed"));
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), group);
    assert.equal(ui.container.querySelector('[data-record-key="command"]'), article);
    assert.equal(group.open, true, "an explicit open overrides later collapsed defaults");
    assert.equal(details.open, true, "an explicit tool open overrides later collapsed defaults");
  } finally { await ui.close(); }
});

test("Compact keeps messages and preserves grouped tool rows, open details, and scroll through completion and hydration", async () => {
  const ui = browser();
  const prompt = user("question", "Keep the user request visible");
  const answer = entry("answer", "assistant", "Answer stays visible");
  const first = tool("tool-one");
  try {
    await ui.render([prompt, answer, first]);
    assert.equal(ui.container.querySelector('[data-record-key="question"]')?.textContent?.includes(prompt.text), true);
    assert.equal(ui.container.querySelector('[data-record-key="answer"]')?.textContent?.includes(answer.text), true);
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')!;
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
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), group);
    assert.equal(group.open, true);
    assert.equal(ui.container.querySelector('[data-record-key="tool-one"]'), firstArticle);
    assert.equal(firstDetails.open, true);
    assert.equal(groupElements(group, "article.entry.tool").length, 2);
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

test("undisplayed response metadata does not split same-scope shell activity, while visible reasoning remains a boundary", async () => {
  const ui = browser();
  const first = tool("round-one-tool", { state: "Completed", streaming: false });
  const hiddenResponse = entry("round-two-response", "assistant", "", {
    runId: "run",
    responseId: "round-two",
    responseMetadata: {
      response_id: "round-two", response_session_id: "root", run_id: "run", revision: 1, round_number: 1,
      history_id: "", status: "completed", summary: "", summary_truncated: false, summary_expired: false,
      usage_reported: true,
      usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16, reasoning_tokens: 0,
        prompt_reported: true, completion_reported: true, total_reported: true, reasoning_reported: false,
        reasoning_in_output: "unknown" },
    },
  });
  const second = tool("round-two-tool");
  try {
    await ui.render([first]);
    let groups = [...ui.container.querySelectorAll<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')];
    assert.equal(groups.length, 1);
    const group = groups[0];
    const firstArticle = ui.container.querySelector<HTMLElement>('[data-record-key="round-one-tool"]')!;
    const firstDetails = firstArticle.querySelector<HTMLDetailsElement>("details.execution-record")!;
    const firstSummary = firstDetails.querySelector("summary")!;
    await act(async () => { group.open = true; group.dispatchEvent(new ui.dom.window.Event("toggle")); });
    await act(async () => { firstDetails.open = true; firstDetails.dispatchEvent(new ui.dom.window.Event("toggle")); });
    firstSummary.focus();

    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    Object.defineProperty(feed, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(feed, "scrollHeight", { configurable: true, value: 500 });
    feed.scrollTop = 150;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));

    await ui.render([first, hiddenResponse]);
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), group);
    assert.equal(ui.container.querySelector('[data-record-key="round-two-response"]'), null);

    await ui.render([first, hiddenResponse, second]);
    groups = [...ui.container.querySelectorAll<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')];
    assert.equal(groups.length, 1);
    assert.equal(groups[0], group);
    assert.equal(group.querySelector(".chat-activity-count")?.textContent, "2");
    assert.equal(group.dataset.state, "Running");
    assert.deepEqual(groupElements<HTMLElement>(group, "article.entry.tool").map(row => row.dataset.recordKey),
      ["round-one-tool", "round-two-tool"]);
    assert.equal(groupElements(group, "details.execution-record").length, 2, "each tool keeps its inspection controls");
    assert.equal(group.open, true);
    assert.equal(firstDetails.open, true);
    assert.equal(ui.dom.window.document.activeElement, firstSummary);
    assert.equal(feed.scrollTop, 150);

    const completedSecond = { ...second, state: "Completed", streaming: false, text: "round two output\n", result: "round two output\n" };
    await ui.render([first, hiddenResponse, completedSecond]);
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), group);
    assert.equal(group.dataset.state, "Done");
    assert.equal(firstDetails.open, true);
    assert.equal(ui.dom.window.document.activeElement, firstSummary);
    assert.equal(feed.scrollTop, 150);

    const visibleResponse = { ...hiddenResponse, responseMetadata: {
      ...hiddenResponse.responseMetadata!, revision: 2, summary: "The provider supplied a plan.",
    } };
    await ui.render([first, visibleResponse, completedSecond]);
    groups = [...ui.container.querySelectorAll<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')];
    assert.equal(groups.length, 2, "a displayable reasoning row separates the tool groups");
    assert.equal(groups[0], group, "the first group retains its mounted identity when the visible boundary appears");
    assert.ok(ui.container.querySelector('[data-record-key="round-two-response"]'));

    const hiddenReasoning = { ...defaultAppearance, activity: {
      ...defaultAppearance.activity, reasoning: { ...defaultAppearance.activity.reasoning, placement: "hidden" as const },
    } };
    await ui.render([first, visibleResponse, completedSecond], hiddenReasoning);
    groups = [...ui.container.querySelectorAll<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')];
    assert.equal(groups.length, 1, "changing reasoning visibility removes its layout boundary");
    assert.equal(groups[0], group);
    assert.equal(groups[0].querySelector(".chat-activity-count")?.textContent, "2");

    await ui.render([
      { ...first, recorded: true, historyId: "saved-first" },
      hiddenResponse,
      { ...completedSecond, recorded: true, historyId: "saved-second" },
    ]);
    groups = [...ui.container.querySelectorAll<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')];
    assert.equal(groups.length, 1, "saved history preserves the same visible grouping");
    assert.equal(groups[0], group);
    assert.equal(groups[0].querySelector(".chat-activity-count")?.textContent, "2");
    assert.equal(firstDetails.open, true);
    assert.equal(ui.dom.window.document.activeElement, firstSummary);
    assert.equal(feed.scrollTop, 150);
  } finally { await ui.close(); }
});

test("a later failure changes the collapsed activity group to Needs attention and opens it", async () => {
  const ui = browser();
  const prompt = user("question", "Check this command");
  const first = tool("first-command", { state: "Completed", streaming: false });
  try {
    await ui.render([prompt, first]);
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')!;
    assert.equal(group.open, false);
    const error = tool("later-error", { state: "Failed", error: "exit 4", streaming: false });
    await ui.render([prompt, first, error]);
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), group);
    assert.equal(group.dataset.state, "Needs attention");
    assert.equal(group.open, true, "An important failure overrides the collapsed preference");
    assert.equal(groupElements<HTMLElement>(group, '[data-record-key="later-error"]').length, 1);
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
    assert.equal(ui.container.querySelector('.chat-activity-group[data-category="tools"]'), null);
    assert.ok(ui.container.querySelector('[data-record-key="routine"]'));
    assert.ok(ui.container.querySelector('[data-record-key="routine-two"]'));
    assert.equal(ui.container.querySelector<HTMLDetailsElement>('[data-record-key="routine"] details.execution-record')?.open, true,
      "Detailed opens command details by default");
  } finally { await ui.close(); }
});

test("reasoning stays collapsed through completion and history without duplicate run counters", async () => {
  const ui = browser();
  const usage = { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20, reasoning_tokens: 3,
    prompt_reported: true, completion_reported: true, total_reported: true, reasoning_reported: true,
    reasoning_in_output: "included" as const, response_count: 2, usage_response_count: 2,
    reasoning_response_count: 2, prompt_response_count: 2, completion_response_count: 2,
    total_response_count: 2, complete: true };
  const responding: ResponseMetadata = { response_id: "response", response_session_id: "root", run_id: "root-run",
    revision: 1, round_number: 0, history_id: "", status: "responding", summary: "A provider explanation. The remaining details stay in the opened note.",
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
    assert.equal(article.querySelector(".entry-label:not([hidden])")?.textContent?.trim(), "Assistant",
      "The response keeps its single agent byline");
    assert.equal(reasoning.querySelector(".response-reasoning-title")?.textContent, "Reasoning");
    assert.equal(reasoning.querySelector(".response-reasoning-preview")?.textContent, "A provider explanation.");
    assert.equal(reasoning.querySelector("summary")?.getAttribute("aria-label"),
      "Show reasoning: Reasoning. A provider explanation.");
    assert.equal(ui.container.querySelector("details.response-run-usage"), null);
    assert.equal(ui.container.querySelector('[aria-label="Provider-reported run tokens"]'), null);

    await act(async () => { reasoning.open = true; reasoning.dispatchEvent(new ui.dom.window.Event("toggle")); });
    assert.equal(reasoning.open, true);
    assert.match(reasoning.querySelector(".response-reasoning-content")?.textContent || "", /The remaining details stay in the opened note/);
    const completedMetadata = { ...responding, revision: 2, history_id: "answer-row", status: "completed" as const };
    const completed = { ...current, text: "The final answer", streaming: false, historyId: "answer-row", responseMetadata: completedMetadata };
    await ui.render([user("question", "Question"), completed, uiEntries[2], child]);
    assert.equal(ui.container.querySelector('[data-record-key="current-answer"]'), article);
    assert.equal(article.textContent?.includes("The final answer"), true);
    assert.equal(article.querySelector("details.response-reasoning"), reasoning);
    assert.equal(reasoning.open, true);
    assert.equal(article.querySelector(".response-run-usage"), null);
    assert.equal(completed.responseMetadata?.run_usage?.total_tokens, 20);

    const hydrated = { ...completed, recorded: true, historyIndex: 1 };
    await ui.render([user("question", "Question"), hydrated, uiEntries[2], child]);
    assert.equal(ui.container.querySelector('[data-record-key="current-answer"]'), article);
    assert.equal(reasoning.open, true, "History hydration preserves the user's disclosure choice");
    assert.equal(ui.container.querySelector("details.response-run-usage"), null);
    assert.equal(hydrated.responseMetadata?.run_usage?.total_tokens, 20, "History retains usage metadata without rendering it twice");
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
      assert.doesNotMatch(article.textContent || "", /A summary hidden by this preset|Reasoning|Thinking/);
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

const diagnosticResponse: ResponseMetadata = {
  response_id: "diagnostic-between-rounds", response_session_id: "root", run_id: "run", revision: 1,
  round_number: 1, history_id: "", status: "completed", summary: "", summary_truncated: false,
  summary_expired: false, usage_reported: false,
};

test("late reasoning preserves the second tool across visible and hidden boundaries", async () => {
  const ui = browser();
  const first = tool("diagnostic-first", { state: "Completed", streaming: false });
  const second = tool("diagnostic-second", { state: "Completed", streaming: false });
  const hidden = entry("diagnostic-response", "assistant", "", { runId: "run", responseId: diagnosticResponse.response_id,
    responseMetadata: diagnosticResponse });
  const visible = { ...hidden, responseMetadata: { ...diagnosticResponse, revision: 2, summary: "A real late provider summary." } };
  const hiddenReasoning = { ...defaultAppearance, activity: { ...defaultAppearance.activity,
    reasoning: { ...defaultAppearance.activity.reasoning, placement: "hidden" as const } } };
  try {
    await ui.render([first, hidden, second]);
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')!;
    const secondArticle = ui.container.querySelector<HTMLElement>('[data-record-key="diagnostic-second"]')!;
    const secondParent = secondArticle.parentElement!;
    const transcriptParent = secondParent.parentElement;
    const secondDetails = secondArticle.querySelector<HTMLDetailsElement>("details.execution-record")!;
    const secondSummary = secondDetails.querySelector<HTMLElement>("summary")!;
    await act(async () => { group.open = true; group.dispatchEvent(new ui.dom.window.Event("toggle")); });
    await act(async () => { secondDetails.open = true; secondDetails.dispatchEvent(new ui.dom.window.Event("toggle")); });
    secondSummary.focus();
    assert.equal(ui.dom.window.document.activeElement, secondSummary);
    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    Object.defineProperty(feed, "clientHeight", { configurable: true, value: 100 });
    Object.defineProperty(feed, "scrollHeight", { configurable: true, value: 500 });
    feed.scrollTop = 150;
    feed.dispatchEvent(new ui.dom.window.Event("scroll"));
    const states: { phase: string; groups: number; sameArticle: boolean; sameParent: boolean; visible: boolean; sameDetails: boolean; open: boolean; sameFocus: boolean; scrollTop: number }[] = [];
    const capture = (phase: string) => {
      const article = ui.container.querySelector<HTMLElement>('[data-record-key="diagnostic-second"]');
      const details = article?.querySelector<HTMLDetailsElement>("details.execution-record");
      for (const header of ui.container.querySelectorAll<HTMLDetailsElement>("details.chat-activity-group")) groupRecords(header);
      states.push({ phase, groups: ui.container.querySelectorAll('.chat-activity-group[data-category="tools"]').length,
        sameParent: article?.parentElement === secondParent && secondParent.parentElement === transcriptParent, visible: !secondParent.hidden,
        sameArticle: article === secondArticle, sameDetails: details === secondDetails, open: details?.open === true,
        sameFocus: ui.dom.window.document.activeElement === secondSummary, scrollTop: feed.scrollTop });
    };
    await ui.render([first, visible, second]);
    capture("late-visible-summary");
    await ui.render([first, visible, second], hiddenReasoning);
    capture("reasoning-hidden");
    await ui.render([first, visible, second]);
    capture("reasoning-visible-again");
    assert.deepEqual(states, [
      { phase: "late-visible-summary", groups: 2, sameArticle: true, sameParent: true, visible: true, sameDetails: true, open: true, sameFocus: true, scrollTop: 150 },
      { phase: "reasoning-hidden", groups: 1, sameArticle: true, sameParent: true, visible: true, sameDetails: true, open: true, sameFocus: true, scrollTop: 150 },
      { phase: "reasoning-visible-again", groups: 2, sameArticle: true, sameParent: true, visible: true, sameDetails: true, open: true, sameFocus: true, scrollTop: 150 },
    ], "late visible boundaries and settings changes must preserve the second tool, not only the first group");
  } finally { await ui.close(); }
});

for (const strict of [false, true]) test(`coalescing a focused activity header transfers focus to the surviving group${strict ? " in StrictMode" : ""}`, async () => {
  const ui = browser(strict);
  const first = tool("first", { state: "Completed", streaming: false });
  const pending = tool("second", { title: "edit_file", toolProgress: "streaming" });
  try {
    await ui.render([first, pending]);
    const headers = [...ui.container.querySelectorAll<HTMLDetailsElement>("details.chat-activity-group")];
    assert.equal(headers.length, 2);
    headers[1].querySelector("summary")!.focus();
    const secondArticle = ui.container.querySelector('[data-record-key="second"]');
    const secondParent = secondArticle?.parentElement;
    await ui.render([first, { ...pending, title: "shell", toolProgress: undefined, streaming: false }]);
    assert.equal(ui.container.querySelectorAll("details.chat-activity-group").length, 1);
    assert.equal(ui.dom.window.document.activeElement, headers[0].querySelector("summary"));
    assert.equal(ui.container.querySelector('[data-record-key="second"]'), secondArticle);
    assert.equal(secondArticle?.parentElement, secondParent);
    assert.equal(groupRecords(headers[0]).length, 2);
  } finally { await ui.close(); }
});

test("new activity and task links reveal flat group members and retain their disclosure choice", async () => {
  const ui = browser();
  const command = tool("reveal-command", { activity: 1 });
  try {
    await ui.render([command]);
    const feed = ui.container.querySelector<HTMLElement>(".conversation")!;
    let scrolls = 0;
    Object.defineProperty(feed, "scrollTo", { value: (options: ScrollToOptions) => { feed.scrollTop = options.top || 0; scrolls++; } });
    const group = ui.container.querySelector<HTMLDetailsElement>("details.chat-activity-group")!;
    assert.equal(group.open, false);
    groupRecords(group);
    await ui.render([{ ...command, activity: 2 }]);
    const activity = ui.container.querySelector<HTMLButtonElement>(".activity-announcement button")!;
    assert.ok(activity);
    await act(async () => activity.click());
    assert.equal(group.open, true);
    groupRecords(group);
    await ui.render([{ ...command, state: "Completed", streaming: false, activity: 2 }]);
    assert.equal(group.open, true);
    let tasks: Entry[] = appendEvent([], { event: "task_started", run_id: "run", session_id: "child", activation: 0, title: "Child" });
    tasks = [...tasks, tool("task-link", { title: "inspect", delegatedTaskId: "child" })];
    await ui.render(tasks, { ...defaultAppearance, activity: { ...defaultAppearance.activity,
      tools: { placement: "separate", details: "expanded" } } });
    const taskGroup = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="agents"]')!;
    assert.equal(taskGroup.open, false);
    const link = ui.container.querySelector<HTMLAnchorElement>('a[href="#task-child"]')!;
    assert.ok(link);
    await act(async () => link.click());
    assert.equal(taskGroup.open, true);
    groupRecords(taskGroup);
    assert.equal(scrolls, 2, "both reveal paths scroll after opening their linked group members");
  } finally { await ui.close(); }
});

test("mixed shell and ordinary tools share one group while preserving command inspection", async () => {
  const ui = browser();
  const command = tool("command", { title: "shell", inputs: JSON.stringify({ command: "printf smoke" }),
    text: "smoke\n", approval: "Allowed once", state: "Receiving output" });
  const others = ["read_file", "search", "channel_configuration", "run_shell_command"].map((title, index) =>
    tool(`other-${index}`, { title, state: "Completed", streaming: false }));
  try {
    await ui.render([command, ...others], toolsDetails("expanded"));
    const group = ui.container.querySelector<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')!;
    assert.equal(ui.container.querySelectorAll(".chat-activity-group").length, 1);
    assert.equal(group.querySelector(".chat-activity-count")?.textContent, "5");
    assert.deepEqual(groupElements<HTMLElement>(group, "article.entry.tool").map(row => row.dataset.recordKey),
      ["command", ...others.map(value => value.id)]);
    const article = ui.container.querySelector<HTMLElement>('[data-record-key="command"]')!;
    assert.equal(article.querySelector(".tool-record")?.getAttribute("data-tool-family"), "command");
    assert.ok(article.querySelector(".tool-symbol svg"));
    assert.equal(article.querySelector(".execution-target")?.textContent, "printf smoke");
    assert.match(article.textContent || "", /Allowed once/);
    const output = article.querySelector('[data-panel="stream"]');
    assert.match(output?.textContent || "", /smoke/);
    const completed: Entry = { ...command, state: "Completed", streaming: false, durationMs: 1200,
      commandOutcome: { kind: "exited", exitCode: 0 },
      result: JSON.stringify({ output: "smoke\n", exit_code: 0, timed_out: false, truncated: false }) };
    await ui.render([completed, ...others], toolsDetails("expanded"));
    assert.equal(ui.container.querySelector('[data-record-key="command"]'), article);
    assert.equal(article.querySelector('[data-panel="stream"]'), output);
    assert.equal(article.querySelector(".execution-duration")?.textContent, "1.2 s");
    for (const label of ["Inputs", "Result", "Details"]) {
      const tab = [...article.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(value => value.textContent === label)!;
      assert.ok(tab);
      await act(async () => tab.click());
      assert.equal(tab.getAttribute("aria-selected"), "true");
    }
    assert.match(article.querySelector('[data-panel="result"]')?.textContent || "", /smoke/);
    assert.match(article.querySelector('[data-panel="metadata"]')?.textContent || "", /exited with code 0/i);
    assert.match(article.querySelector('[data-panel="inputs"]')?.textContent || "", /printf smoke/);
  } finally { await ui.close(); }
});

test("actual streamed successive-round calls merge into one Tools 2 live and after reload", async () => {
  const ui = browser();
  let entries: Entry[] = [];
  const send = (event: WireEvent) => { entries = appendEvent(entries, { run_id: "run", ...event }); };
  const groups = () => [...ui.container.querySelectorAll<HTMLDetailsElement>('.chat-activity-group[data-category="tools"]')];
  try {
    send({ event: "tool_call_progress", generation_id: "diagnostic-generation-one", index: 0, id: "", name: "shell",
      status: "streaming", arguments_text: '{"command":"first' });
    assert.equal(entries[0]!.toolProgress, "streaming");
    await ui.render(entries);
    const firstId = entries[0]!.id;
    send({ event: "tool_call", generation_id: "diagnostic-generation-one", index: 0, id: "diagnostic-call-one", name: "shell",
      arguments: { command: "first" } });
    send({ event: "tool_execution_started", generation_id: "diagnostic-generation-one", index: 0, id: "diagnostic-call-one", name: "shell" });
    send({ event: "tool_result", generation_id: "diagnostic-generation-one", index: 0, id: "diagnostic-call-one", name: "shell", result: "first result" });
    assert.equal(entries[0]!.id, firstId);
    assert.equal(entries[0]!.toolProgress, undefined);
    assert.equal(entries[0]!.state, "Completed");
    await ui.render(entries);
    send({ event: "response_metadata", ...diagnosticResponse });
    const metadata = entries.find(value => value.kind === "assistant")!;
    assert.equal(metadata.text, "");
    assert.equal(metadata.responseMetadata?.summary, "");
    await ui.render(entries);
    assert.equal(ui.container.querySelector(`[data-record-key="${metadata.id}"]`), null);
    send({ event: "tool_call_progress", generation_id: "diagnostic-generation-two", index: 0, id: "", name: "read_file",
      status: "streaming", arguments_text: '{"path":"second' });
    const secondId = entries.at(-1)!.id;
    assert.equal(entries.at(-1)!.toolProgress, "streaming");
    await ui.render(entries);
    assert.equal(groups().length, 1, "mixed calls already share a group while arguments stream");
    assert.equal(groups()[0].querySelector(".chat-activity-count")?.textContent, "2");
    send({ event: "tool_call", generation_id: "diagnostic-generation-two", index: 0, id: "diagnostic-call-two", name: "read_file",
      arguments: { path: "second" } });
    send({ event: "tool_execution_started", generation_id: "diagnostic-generation-two", index: 0, id: "diagnostic-call-two", name: "read_file" });
    send({ event: "tool_result", generation_id: "diagnostic-generation-two", index: 0, id: "diagnostic-call-two", name: "read_file", result: "second result" });
    assert.equal(entries.at(-1)!.id, secondId);
    assert.equal(entries.at(-1)!.toolProgress, undefined);
    assert.equal(entries.at(-1)!.state, "Completed");
    await ui.render(entries);
    const live = { groups: groups().length, counts: groups().map(group => group.querySelector(".chat-activity-count")?.textContent),
      order: [...ui.container.querySelectorAll<HTMLElement>("article.entry.tool")].map(row => row.dataset.recordKey) };
    const cold = new JSDOM(renderToStaticMarkup(createElement(Conversation, {
      entries, appearance: defaultAppearance, sessionId: "root", demo: false, canSubmit: false, submit() {},
    })));
    const coldGroups = [...cold.window.document.querySelectorAll('.chat-activity-group[data-category="tools"]')];
    const reloaded = { groups: coldGroups.length, counts: coldGroups.map(group => group.querySelector(".chat-activity-count")?.textContent),
      order: [...cold.window.document.querySelectorAll<HTMLElement>("article.entry.tool")].map(row => row.dataset.recordKey) };
    cold.window.close();
    assert.deepEqual({ live, reloaded }, {
      live: { groups: 1, counts: ["2"], order: [firstId, secondId] },
      reloaded: { groups: 1, counts: ["2"], order: [firstId, secondId] },
    }, "argument streaming must not permanently pin successive rounds into two Tools 1 wrappers");
  } finally { await ui.close(); }
});
