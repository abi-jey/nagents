import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { ApprovalDialog } from "../approvals/ApprovalDialog.js";
import { useApproval } from "../approvals/useApproval.js";
import { appendEvent, type Entry } from "./transcript.js";
import { LiveSessions } from "./liveTranscript.js";
import { Conversation } from "./Conversation.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { activityCategory, importantActivity } from "./activityLayout.js";
import type { Approval, Decision, Snapshot, WireEvent } from "../../types.js";

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
  const mandatory = render({ ...approval, allow_tool: false, allow_tool_persistent: false });
  assert.match(mandatory, /Allow once/);
  assert.match(mandatory, /Approval applies only to these inputs/);
  assert.doesNotMatch(mandatory, /Always allow this tool|all chats and agents in this workspace/);
});

test("remote delegate approval names the remote operation and safely handles malformed nested data", () => {
  const render = (arguments_: unknown) => renderToStaticMarkup(createElement(ApprovalDialog, {
    approval: { ...approval, tool: "delegate", arguments: arguments_ }, busy: false, error: "", decide() {}, cancel() {},
  }));
  const remote = render({ agent: "reviewer", remote_operation: { tool: "shell", arguments: { command: "cat report.txt" } } });
  assert.match(remote, /Review remote <code>shell<\/code>/);
  assert.match(remote, /Requested by reviewer/);
  assert.match(remote, /cat report\.txt/);
  assert.doesNotMatch(remote, /Local tools are not sandboxed/);
  assert.doesNotMatch(remote, /Always allow this tool/);
  for (const malformed of [null, "nested", [], { remote_operation: [] }, { remote_operation: { tool: { name: "shell" } } }]) {
    assert.doesNotThrow(() => render(malformed));
    assert.match(render(malformed), /Review (?:remote )?<code>(?:delegate|operation)<\/code>/);
  }
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

test("remote approval completion stays a stable tools review record without claiming execution", () => {
  const arguments_ = { agent: "reviewer", remote_operation: { tool: "shell", arguments: { command: "printf A2A_REVIEW_ONLY_OK" } } };
  const call = { event: "tool_call", run_id: "remote-run", id: "remote-call", name: "delegate", arguments: arguments_ } as const;
  const request = { event: "approval", run_id: "remote-run", call_id: "remote-call", tool: "delegate",
    approval_id: "remote-nonce", arguments: arguments_ } as const;
  const closed = { event: "approval_closed", run_id: "remote-run", approval_id: "remote-nonce", decision: "allow" } as const;
  let entries = appendEvent([], call);
  entries = appendEvent(entries, request);
  entries = appendEvent(entries, closed);
  const stableId = entries[0].id;
  entries = appendEvent(entries, { event: "run_finished", run_id: "remote-run", status: "completed" });
  entries = appendEvent(entries, { event: "run_finished", run_id: "remote-run", status: "completed" });

  assert.equal(entries.length, 1);
  assert.equal(entries[0].id, stableId);
  assert.equal(entries[0].state, "Review approved");
  assert.equal(entries[0].result, undefined, "approval does not fabricate a remote execution result");
  assert.equal(entries[0].remoteReview?.agent, "reviewer");
  assert.equal(entries[0].remoteReview?.tool, "shell");
  assert.equal(activityCategory({ kind: "entry", entry: entries[0] }), "tools");
  assert.equal(importantActivity({ kind: "entry", entry: entries[0] }), false);

  const replay = [call, request, closed, { event: "run_finished", run_id: "remote-run", status: "completed" }]
    .reduce<Entry[]>((current, event) => appendEvent(current, event), []);
  assert.equal(replay.length, 1);
  assert.equal(replay[0].id, stableId);
  assert.equal(replay[0].state, "Review approved");
  assert.deepEqual(replay[0].remoteReview, entries[0].remoteReview);

  const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry: entries[0], open: true })));
  try {
    assert.match(dom.window.document.body.textContent || "", /Remote shell review/);
    assert.match(dom.window.document.body.textContent || "", /Review of shell requested by reviewer/);
    assert.match(dom.window.document.body.textContent || "", /A2A_REVIEW_ONLY_OK/);
    assert.match(dom.window.document.querySelector(".execution-record")?.getAttribute("data-tool-family") || "", /generic/);
  } finally { dom.window.close(); }
});

test("remote denial or expiry remains attention, and local delegate approval still awaits its result", () => {
  const arguments_ = { agent: "reviewer", remote_operation: { tool: "shell", arguments: { command: "pwd" } } };
  const start = (approvalId: string) => appendEvent(appendEvent([], {
    event: "tool_call", run_id: "review-run", id: `call-${approvalId}`, name: "delegate", arguments: arguments_,
  }), {
    event: "approval", run_id: "review-run", call_id: `call-${approvalId}`, tool: "delegate", approval_id: approvalId, arguments: arguments_,
  });
  for (const [approvalId, expired] of [["denied", false], ["expired", true]] as const) {
    const denied = appendEvent(start(approvalId), { event: "approval_closed", run_id: "review-run", approval_id: approvalId,
      decision: "deny", expired });
    assert.equal(denied[0].state, "Approval denied");
    assert.equal(importantActivity({ kind: "entry", entry: denied[0] }), true);
  }

  let local = appendEvent([], { event: "tool_call", run_id: "local-run", id: "local-call", name: "delegate", arguments: { task: "research" } });
  local = appendEvent(local, { event: "approval", run_id: "local-run", call_id: "local-call", tool: "delegate",
    approval_id: "local-nonce", arguments: { task: "research" } });
  local = appendEvent(local, { event: "approval_closed", run_id: "local-run", approval_id: "local-nonce", decision: "allow" });
  assert.equal(local[0].remoteReview, undefined);
  assert.equal(local[0].state, "Awaiting execution result");
  local = appendEvent(local, { event: "run_finished", run_id: "local-run", status: "completed" });
  assert.equal(local[0].state, "No result recorded");
  assert.equal(importantActivity({ kind: "entry", entry: local[0] }), true);
});

test("malformed remote operation metadata remains safe and cannot inject review markup", () => {
  for (const remoteOperation of [null, [], { tool: { name: "shell" } }, { tool: "<img src=x onerror=alert(1)>" }]) {
    const arguments_ = { agent: "<script>alert(1)</script>", remote_operation: remoteOperation };
    const entries = appendEvent([], { event: "approval", run_id: "run", call_id: "call", tool: "delegate",
      approval_id: "nonce", arguments: arguments_ });
    assert.equal(entries.length, 1);
    assert.ok([undefined, "operation"].includes(entries[0].remoteReview?.tool));
    const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry: entries[0], open: true })));
    try {
      assert.equal(dom.window.document.querySelector("script, img"), null);
      assert.match(dom.window.document.body.textContent || "", /alert\(1\)/);
    } finally { dom.window.close(); }
  }
});

for (const [policy, badge] of [
  ["workspace_tool_allow", "Always allowed in workspace"],
  ["channel_auto_reply", "Allowed by channel policy"],
] as const) {
  test(`${policy} renders only tool badges during streaming, reconnect and cold replay`, () => {
    const cache = new LiveSessions();
    const events: WireEvent[] = [];
    const tool = policy === "workspace_tool_allow" ? "shell" : "channel_send";
    const arguments_ = tool === "shell" ? { command: "pwd" } : { channel: "telegram", destination: "owning-chat", text: "Hello" };
    const assertCards = (entries: Entry[], count: number) => {
      assert.equal(entries.filter(entry => entry.kind === "status").length, 0);
      const calls = entries.filter(entry => entry.kind === "tool");
      assert.equal(calls.length, count);
      assert.ok(calls.every(entry => entry.approval === badge));
      const dom = new JSDOM(renderToStaticMarkup(createElement(Conversation, {
        entries, sessionId: "root", demo: false, canSubmit: false, submit: () => {},
      })));
      try {
        assert.equal(dom.window.document.querySelectorAll(".execution-record").length, count);
        assert.equal(dom.window.document.querySelectorAll(".execution-approval").length, count);
        assert.doesNotMatch(dom.window.document.body.textContent!, /is always allowed|Automatic approval for/);
      } finally { dom.window.close(); }
    };
    for (let i = 0; i < 20; i++) {
      const scope = { run_id: "run", generation_id: `generation-${i}`, index: 0 };
      // The host approval can arrive before the provider's queued tool call.
      for (const event of [
        { event: "notice", ...scope, automatic: true, policy, call_id: `call-${i}`, tool,
          text: policy === "workspace_tool_allow" ? "shell is always allowed in this workspace."
            : "Automatic approval for this session's permanently owned chat under its connection's automatic-message policy." },
        { event: "tool_call", ...scope, id: `call-${i}`, name: tool, arguments: arguments_ },
        { event: "tool_result", ...scope, id: `call-${i}`, result: "/workspace", duration_ms: 10 },
      ]) {
        events.push(event);
        cache.receive({ type: "event", session_id: "root", epoch: "one", cursor: events.length, record: event });
      }
      assertCards(cache.get("root").entries, i + 1);
    }
    const snapshot: Snapshot = { session_id: "root", sessions: [], history: [], retained_tasks: [],
      active_run: { id: "run", session_id: "root", status: "running", events } };
    const frame = { type: "snapshot" as const, session_id: "root", epoch: "one", cursor: events.length, snapshot };
    cache.receive(frame);
    assertCards(cache.get("root").entries, 20);
    const cold = new LiveSessions();
    cold.receive(frame);
    assertCards(cold.get("root").entries, 20);
  });

  test(`${policy} stays scoped to the correct child and consumes incomplete policy notices`, () => {
    let entries: Entry[] = [];
    for (const task_id of ["one", "two"]) entries = appendEvent(entries, {
      event: "tool_call", run_id: "run", task_id, activation: 2, id: "same", name: "channel_send", arguments: {},
    });
    entries = appendEvent(entries, { event: "notice", run_id: "run", task_id: "two", activation: 2,
      call_id: "same", tool: "channel_send", policy, text: "Policy evidence" });
    assert.equal(entries.length, 2);
    assert.equal(entries[0].approval, undefined);
    assert.equal(entries[1].approval, badge);
    assert.equal(appendEvent(entries, { event: "notice", policy, text: "No call identity" }), entries);
    const notice = appendEvent(entries, { event: "notice", text: "Unattended approval was denied; no action was taken." });
    assert.equal(notice.at(-1)!.kind, "status");
    assert.equal(notice.at(-1)!.text, "Unattended approval was denied; no action was taken.");
  });
}
