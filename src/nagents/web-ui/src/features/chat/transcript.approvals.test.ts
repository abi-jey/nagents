import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { ApprovalDialog } from "../approvals/ApprovalDialog.js";
import { useApproval } from "../approvals/useApproval.js";
import { appendEvent, type Entry } from "./transcript.js";
import type { Approval, Decision } from "../../types.js";

const approval: Approval = {
  run_id: "run", approval_id: "nonce", call_id: "call", tool: "shell",
  description: "Execute this command", preview: "", arguments: { command: "pwd" },
  task_id: "", task_name: "", activation: 0, depth: 0, allow_tool: true, allow_tool_persistent: true,
};

test("approval dialog explains durable workspace scope and extension reload boundaries", () => {
  const render = (value: Approval) => renderToStaticMarkup(createElement(ApprovalDialog, {
    approval: value, busy: false, error: "", decide: () => {}, cancel: () => {},
  }));
  const html = render(approval);
  assert.match(html, /Allow once/);
  assert.match(html, /Always allow this tool/);
  assert.match(html, /all chats and agents in this workspace/);
  assert.match(html, /Saved across server restarts/);
  assert.match(html, /Change saved permissions in Tools/);
  assert.match(render({ ...approval, allow_tool_persistent: false }), /extension asks again after it is reloaded/);
  assert.doesNotMatch(render({ ...approval, allow_tool: false }), /Always allow this tool/);
});

test("permission buttons send distinct decisions and Escape still denies", async (t) => {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const root = createRoot(dom.window.document.getElementById("root")!);
  let controller: ReturnType<typeof useApproval>;
  const sent: Decision[] = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    assert.equal(path, "/api/approval");
    const body = JSON.parse(String(init.body));
    assert.equal(body.run_id, "run"); assert.equal(body.call_id, "call"); assert.equal(body.approval_id, "nonce");
    sent.push(body.decision);
    return Response.json({ status: body.decision });
  });
  function Harness() {
    controller = useApproval("token");
    return controller.pending ? createElement(ApprovalDialog, {
      approval: controller.pending, busy: controller.deciding, error: controller.error,
      decide: decision => { void controller.decide(decision); }, cancel: () => {},
    }) : null;
  }
  try {
    await act(async () => root.render(createElement(Harness)));
    for (const [label, decision] of [["Allow once", "allow"], ["Always allow this tool", "allow_tool"], ["Deny", "deny"]] as const) {
      await act(async () => controller.open({ event: "approval", ...approval }));
      await act(async () => [...dom.window.document.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === label)!.click());
      assert.equal(sent.at(-1), decision);
      assert.equal(dom.window.document.querySelector("dialog"), null);
    }
    await act(async () => controller.open({ event: "approval", ...approval }));
    await act(async () => dom.window.document.querySelector("dialog")!.dispatchEvent(new dom.window.Event("cancel", { bubbles: true, cancelable: true })));
    assert.equal(sent.at(-1), "deny");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});

test("saved and one-time approval labels remain bound to the exact task and call", () => {
  let entries: Entry[] = [];
  for (const task_id of ["one", "two"]) entries = appendEvent(entries, { event: "tool_call", run_id: "run", task_id, activation: 3, id: "same", name: "shell", arguments: {} });
  entries = appendEvent(entries, { event: "notice", run_id: "run", task_id: "two", activation: 3, call_id: "same", tool: "shell", policy: "workspace_tool_allow", text: "Saved permission" });
  assert.equal(entries.find(entry => entry.kind === "tool" && entry.taskId === "one")!.approval, undefined);
  assert.equal(entries.find(entry => entry.kind === "tool" && entry.taskId === "two")!.approval, "Always allowed in workspace");
  entries = appendEvent(entries, { event: "approval", run_id: "run", task_id: "one", activation: 3, id: "same", tool: "shell", approval_id: "nonce", arguments: {} });
  entries = appendEvent(entries, { event: "approval_closed", run_id: "run", approval_id: "nonce", decision: "allow_tool" });
  const first = entries.find(entry => entry.kind === "tool" && entry.taskId === "one")!;
  assert.equal(first.approval, "Always allowed in workspace");
  assert.equal(first.state, "Awaiting execution result");
});
