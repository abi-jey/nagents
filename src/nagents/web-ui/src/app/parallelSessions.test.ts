import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { parseFrame } from "../api/subscription.js";
import { sessionActivities } from "../features/sessions/activity.js";
import { ApprovalReview } from "../features/approvals/ApprovalReview.js";
import type { Approval, Decision } from "../types.js";

test("parallel status updates preserve each root and reject nonmetadata payloads", () => {
  const frame = { type: "status", active_run_id: "a", active_session_id: "ngn-a", cursor: 1, epoch: "process",
    active_runs: [{ id: "a", session_id: "ngn-a", status: "approval" }, { id: "b", session_id: "ngn-b", status: "running" }] };
  const activities = sessionActivities({}, parseFrame(JSON.stringify(frame)));
  assert.deepEqual(Object.keys(activities), ["ngn-a", "ngn-b"]);
  assert.equal(activities["ngn-a"].status, "approval");
  const next = sessionActivities(activities, parseFrame(JSON.stringify({
    type: "event", cursor: 2, epoch: "process", session_id: "ngn-b", record: { event: "run_finished", run_id: "b" },
  })));
  assert.deepEqual(Object.keys(next), ["ngn-a"]);
  const stale = sessionActivities(next, parseFrame(JSON.stringify({
    type: "event", cursor: 3, epoch: "process", session_id: "ngn-a", record: { event: "run_finished", run_id: "old-a" },
  })));
  assert.equal(stale["ngn-a"].id, "a");
  for (const invalid of [null, [{ id: "a", session_id: "ngn-a", status: "running", records: [] }], [{ id: "a", status: "running" }]])
    assert.throws(() => parseFrame(JSON.stringify({ ...frame, active_runs: invalid })), /Invalid/);
});

test("Review later dismisses only presentation and returning to the chat reopens its pending decision", async () => {
  const dom = new JSDOM("<footer><div id='approval-waiting-slot'></div></footer><div id='root'></div>", { url: "https://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    HTMLDialogElement: dom.window.HTMLDialogElement, IS_REACT_ACT_ENVIRONMENT: true };
  for (const [name, value] of Object.entries(globals)) Object.defineProperty(globalThis, name, { configurable: true, value });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const root = createRoot(document.getElementById("root")!);
  const decisions: Decision[] = [];
  let stops = 0;
  const approval: Approval = { run_id: "run-a", approval_id: "nonce-a", call_id: "write-a", tool: "write",
    description: "Create a reviewed file", preview: "proposed change", arguments: { path: "file.txt" },
    task_id: "", task_name: "", depth: 0, activation: 0 };
  async function render(sessionId: string, pending: Approval | undefined) {
    await act(async () => root.render(createElement(ApprovalReview, { sessionId, pending, busy: false, error: "",
      decide: (decision: Decision) => { decisions.push(decision); }, cancel: () => { stops++; } })));
  }
  async function click(label: string) {
    const button = [...document.querySelectorAll("button")].find(item => item.textContent === label);
    assert.ok(button, label);
    await act(async () => button.click());
  }
  try {
    await render("ngn-a", approval);
    assert.ok(document.querySelector("dialog[open]"));
    await click("Review later");
    assert.equal(document.querySelector("dialog"), null);
    assert.match(document.body.textContent || "", /Approval waiting/);
    assert.match(document.querySelector("footer")?.textContent || "", /Approval waiting/);
    assert.deepEqual(decisions, []); assert.equal(stops, 0);
    await render("ngn-b", undefined);
    assert.equal(document.querySelector("dialog"), null);
    await render("ngn-a", approval);
    assert.ok(document.querySelector("dialog[open]"));
    await click("Deny");
    assert.deepEqual(decisions, ["deny"]);
    await click("Stop run");
    assert.equal(stops, 1);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of Object.keys(globals)) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
