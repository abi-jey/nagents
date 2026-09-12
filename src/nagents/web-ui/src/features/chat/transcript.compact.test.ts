import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { rememberDisclosure, revealAncestors } from "../../components/disclosures.js";
import { Conversation, TranscriptItems } from "./Conversation.js";
import { ActivityRecord } from "./ActivityRecord.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { SessionSidebar } from "../sessions/SessionSidebar.js";
import { appendActivity, appendEvent, groupTranscript, type Entry } from "./transcript.js";

const entries: Entry[] = [
  { id: "parent-entry", kind: "task", taskId: "parent-full-id", parentTaskId: "", title: "Parent agent", state: "Running", text: "", inputs: "Parent request", runId: "run-full-id" },
  { id: "child-entry", kind: "task", taskId: "child-full-id", parentTaskId: "parent-full-id", title: "Child agent", state: "Completed", text: "", result: "Full child result" },
  { id: "tool-entry", kind: "tool", taskId: "child-full-id", callId: "call-full-id", title: "write_file", state: "Failed", text: "Partial tool output", inputs: "Full tool inputs", error: "Full failure", approval: "Denied", approvalId: "approval-full-id" },
  { id: "notification-entry", kind: "notification", taskId: "", taskName: "Main", sourceTaskId: "child-full-id", sourceName: "Child agent", notificationId: "notification-full-id", cause: "completion", text: "Full notification payload" },
  { id: "wakeup-entry", kind: "wakeup", taskId: "", state: "Fired", wakeupId: "wakeup-full-id", dueAt: "2026-09-12T12:00:00Z", text: "Full wake-up reason" },
];
function markup(items = entries) {
  return renderToStaticMarkup(createElement(Conversation, {
    entries: items, sessionId: "root", demo: false, canSubmit: false,
    submit: () => assert.fail("Rendering must not submit"),
  }));
}

test("compact task branches start closed with names/states in summaries and full nested evidence in inspection details", () => {
  const html = markup();
  for (const id of ["parent-full-id", "child-full-id"]) {
    const opening = new RegExp(`<details data-disclosure-key="task:${id}"([^>]*)>`).exec(html);
    assert.ok(opening);
    assert.doesNotMatch(opening[1], /\bopen\b/);
    const summary = new RegExp(`data-disclosure-key="task:${id}"[^>]*>([\\s\\S]*?)</summary>`).exec(html)?.[1] || "";
    assert.match(summary, /agent/);
    assert.match(summary, /execution-state/);
    assert.doesNotMatch(summary, /record-identity|Parent task ID|Task .*full-id/);
  }
  assert.match(html, /data-task-id="child-full-id"[^>]*data-parent-task-id="parent-full-id"/);
  for (const evidence of ["Task parent-full-id", "Task child-full-id", "Parent task ID: parent-full-id", "call-full-id", "approval-full-id", "Full tool inputs", "Partial tool output", "Full failure", "Full child result"])
    assert.ok(html.includes(evidence), evidence);
});

test("notification and wake-up payloads start collapsed with distinct delivery/firing labels and retained IDs", () => {
  const html = markup();
  for (const id of ["notification-entry", "wakeup-entry"]) {
    const opening = new RegExp(`<details class="activity-record" data-disclosure-key="${id}"([^>]*)>`).exec(html);
    assert.ok(opening);
    assert.doesNotMatch(opening[1], /\bopen\b/);
  }
  assert.match(html, /Completion notification: Child agent → Main/);
  assert.match(html, /Delivered/);
  assert.match(html, /data-state="Fired"/);
  for (const value of ["notification-full-id", "wakeup-full-id", "Full notification payload", "Full wake-up reason", "2026-09-12T12:00:00Z"])
    assert.ok(html.includes(value), value);
});

test("notifications and pending timers without activation provenance do not claim execution zero in inspection", () => {
  const notification = appendEvent([], {
    event: "task_notification", notification_id: "notice-no-activation",
    source_task_id: "source", recipient_task_id: "recipient", text: "Delivered text",
  });
  const pending = appendActivity([], {
    session_id: "root", cursor: 0, active_run_id: "", events: [], truncated: false,
    pending_wakeups: [{ wakeup_id: "timer-no-activation", task_id: "recipient", due_at: "2026-09-12T12:00:00Z", reason: "Pending timer" }],
  });
  for (const entry of [...notification, ...pending]) {
    assert.equal(entry.activation, 0, "The reducer's matching default is not activation provenance");
    const html = renderToStaticMarkup(createElement(ActivityRecord, { entry, open: true, toggle: () => undefined }));
    assert.doesNotMatch(html, /<dt>Activation<\/dt>/);
    assert.ok(html.includes(entry.notificationId || entry.wakeupId || "missing-identity"));
    assert.match(html, /recipient/);
  }
});

test("expanded execution details include full readable tool names and activation triggers beyond the clipped summary", () => {
  for (const title of ["write_workspace_file", `vendor.${"long_tool_name_".repeat(50)}`]) {
    const html = renderToStaticMarkup(createElement(ExecutionRecord, {
      entry: { id: "tool", kind: "tool", title, text: "", state: "Completed", result: "Retained result" }, open: true,
    }));
    const body = html.slice(html.indexOf("</summary>") + "</summary>".length);
    assert.ok(body.includes(`<dt>Tool name</dt><dd>${title}</dd>`));
    assert.doesNotMatch(body, /class="execution-name"/);
    assert.match(body, /Retained result/);
  }
  const trigger = "complete_activation_trigger_".repeat(30);
  const html = renderToStaticMarkup(createElement(ExecutionRecord, {
    entry: { id: "task", kind: "task", activation: 17, trigger, text: "", state: "Running" }, open: true,
  }));
  const body = html.slice(html.indexOf("</summary>") + "</summary>".length);
  assert.ok(body.includes(`<dt>Trigger</dt><dd>${trigger}</dd>`));
  assert.match(body, /<dt>Activation<\/dt><dd>17<\/dd>/);
});

test("the full workspace path has a keyboard-accessible disclosure that mobile CSS does not hide", () => {
  const workspace = `/workspace/${"very-long-segment".repeat(70)}/project`;
  const html = renderToStaticMarkup(createElement(SessionSidebar, {
    workspace, sessions: [], selected: "", disabled: false, open: true,
    select: () => assert.fail("Inspecting workspace information must not switch sessions"),
  }));
  assert.match(html, /<details class="sidebar-footer"><summary>Workspace info<\/summary>/);
  assert.match(html, /class="sidebar-info-content" role="region" aria-label="Workspace information" tabindex="0"/);
  const information = html.slice(html.indexOf('class="sidebar-info-content"'));
  assert.ok(information.includes(`<p>${workspace}</p>`));
  // SSR alone cannot catch a mobile-only display:none rule. Keep this narrow
  // stylesheet regression alongside the native-disclosure/keyboard contract.
  const css = readFileSync(new URL("../../../src/app/styles.css", import.meta.url), "utf8");
  for (const rule of css.matchAll(/\.sidebar-footer\s*\{([^}]*)\}/g))
    assert.doesNotMatch(rule[1], /display\s*:\s*none/);
  assert.match(css, /\.sidebar-info-content\s*\{[^}]*overflow-y:\s*auto/);
});

test("retained registry starts collapsed without removing its task identities or recorded evidence", () => {
  const html = markup([
    { id: "registry", kind: "retained_tasks", text: "" },
    ...entries.map((entry) => entry.kind === "task" ? { ...entry, recorded: true } : entry),
  ]);
  const registry = /<details data-disclosure-key="registry:[^"]+"([^>]*)>/.exec(html);
  assert.ok(registry);
  assert.doesNotMatch(registry[1], /\bopen\b/);
  assert.match(html, /data-task-id="child-full-id"/);
  assert.match(html, /Full child result/);
});

test("user-opened parent and child disclosures remain open when task completion updates their evidence", () => {
  const disclosures = new Map([["task:parent-full-id", true], ["task:child-full-id", true]]);
  const completed = entries.map((entry) => entry.kind === "task" ? { ...entry, state: "Completed", result: "Updated result" } : entry);
  for (const current of [entries, completed]) {
    const html = renderToStaticMarkup(createElement(TranscriptItems, {
      items: groupTranscript(current), names: new Map(), disclosures,
      toggle: () => assert.fail("Rendering an update must not collapse a branch"),
    }));
    assert.match(html, /data-disclosure-key="task:parent-full-id" open=""/);
    assert.match(html, /data-disclosure-key="task:child-full-id" open=""/);
    assert.doesNotMatch(html, /data-disclosure-key="tool-entry" open=/);
  }
});

test("activity reveal opens every required ancestor and stores those choices without changing another branch", () => {
  function element(tagName: string, parentElement?: HTMLElement, key = "") {
    return { tagName, parentElement, dataset: { disclosureKey: key }, open: false } as unknown as HTMLElement;
  }
  const boundary = element("DIV");
  const registry = element("DETAILS", boundary, "registry:retained");
  const parent = element("DETAILS", registry, "task:parent");
  const child = element("DETAILS", parent, "task:child");
  const target = element("ARTICLE", child);
  const keys = revealAncestors(target, boundary);
  assert.deepEqual(keys, ["task:child", "task:parent", "registry:retained"]);
  for (const node of [registry, parent, child]) assert.equal((node as HTMLDetailsElement).open, true);
  const initial = new Map([["task:other", false], ["user-opened", true]]);
  const expanded = keys.reduce((current, key) => rememberDisclosure(current, key, true), initial);
  assert.equal(expanded.get("task:other"), false);
  assert.equal(expanded.get("user-opened"), true);
  assert.equal(rememberDisclosure(expanded, "task:child", true), expanded);
  const closed = rememberDisclosure(expanded, "task:child", false);
  assert.equal(closed.get("task:child"), false);
  assert.equal(closed.get("task:parent"), true);
  assert.equal(expanded.get("task:child"), true);
  assert.equal(initial.has("task:parent"), false);
});
