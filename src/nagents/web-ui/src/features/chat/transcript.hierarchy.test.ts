import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Conversation } from "./Conversation.js";
import { ApprovalDialog } from "../approvals/ApprovalDialog.js";
import {
  appendActivity,
  appendEvent,
  groupTranscript,
  type Entry,
} from "./transcript.js";
import type { WireEvent } from "../../types.js";

const reduce = (...events: WireEvent[]) =>
  events.reduce(appendEvent, [] as Entry[]);
const started = (
  task: string,
  parent = "",
  run = "run-1",
  activation = 0,
): WireEvent => ({
  event: "task_started",
  run_id: run,
  task_id: task,
  parent_task_id: parent,
  name: `Agent ${task}`,
  activation,
  followup: 0,
  trigger: activation ? "notification" : "delegation",
  prompt: `Work ${task}`,
});
const completed = (task: string, run = "run-1", activation = 0): WireEvent => ({
  event: "task_completed",
  run_id: run,
  task_id: task,
  name: `Agent ${task}`,
  activation,
  result: `${task} finished`,
});
const notification = (
  id: string,
  source: string,
  recipient: string,
): WireEvent => ({
  event: "task_notification",
  run_id: "run-2",
  notification_id: id,
  source_task_id: source,
  recipient_task_id: recipient,
  source_name: source ? `Agent ${source}` : "Main",
  recipient_name: recipient ? `Agent ${recipient}` : "Main",
  cause: "completion",
  text: `Delivery ${id}`,
});
function markup(entries: Entry[]) {
  return renderToStaticMarkup(
    createElement(Conversation, {
      entries,
      sessionId: "session",
      demo: true,
      canSubmit: false,
      submit: () => undefined,
    }),
  );
}

// Inspect actual rendered section containment, not the selector's metadata.
function renderedParents(html: string) {
  const sections: string[] = [];
  const parents = new Map<string, string>();
  for (const match of html.matchAll(/<section\b([^>]*)>|<\/section>/g)) {
    if (match[0] === "</section>") sections.pop();
    else {
      const task = /data-task-id="([^"]+)"/.exec(match[1])?.[1] || "";
      if (task) {
        assert.equal(
          parents.has(task),
          false,
          `Duplicate task identity ${task}`,
        );
        parents.set(task, sections.findLast(Boolean) || "");
      }
      sections.push(task);
    }
  }
  return parents;
}

test("task colors stay tied to identity across ordering, activation and restored state", () => {
  const ids = ["A", "B", "C", "D", "E", "F"];
  const entries = reduce(...ids.map((id) => started(id)));
  function accents(entries: Entry[]) {
    const html = markup(entries);
    return new Map(
      [...html.matchAll(/data-task-id="([^"]+)" data-accent="([0-5])"/g)].map(
        (match) => [match[1], match[2]] as const,
      ),
    );
  }
  const initial = accents(entries);
  assert.equal(initial.size, ids.length);
  assert.equal(new Set(initial.values()).size, 6);
  assert.deepEqual(accents([...entries].reverse()), initial);
  assert.deepEqual(
    accents(entries.reduce(
      (next, entry) => appendEvent(next, completed(entry.taskId!)),
      entries,
    )),
    initial,
  );
  assert.deepEqual(
    accents(reduce(...ids.map((id) => started(id, "", "later", 2)))),
    initial,
  );
  assert.deepEqual(
    accents(entries.map((entry) => ({
      ...entry,
      recorded: true,
      state: "Recorded result",
      runId: undefined,
    }))),
    initial,
  );
  assert.match(markup(entries), /Agent A/);
  assert.match(markup(entries), /Running/);
});

test("rendered task identities nest Main > A > B with independent siblings and root chronology", () => {
  const entries = reduce(
    { event: "user_message", text: "First main request" },
    started("A"),
    started("sibling"),
    started("B", "A"),
    { event: "text_done", run_id: "run-1", text: "Main response" },
    { event: "user_message", text: "Second main request" },
  );
  const html = markup(entries);
  assert.deepEqual(
    renderedParents(html),
    new Map([
      ["A", ""],
      ["B", "A"],
      ["sibling", ""],
    ]),
  );
  assert.ok(
    html.indexOf("First main request") < html.indexOf('data-task-id="A"'),
  );
  assert.ok(
    html.indexOf("Main response") < html.indexOf("Second main request"),
  );
  assert.match(html, /Parent: Agent A/);
  assert.match(html, /Task B/);
});

test("closed A, B wake-up, A delivery, actual activation and Main delivery remain distinct evidence", () => {
  let entries = reduce(
    started("A"),
    started("B", "A"),
    completed("B"),
    completed("A"),
  );
  entries = appendEvent(entries, {
    event: "wakeup",
    run_id: "run-2",
    task_id: "B",
    wakeup_id: "alarm",
    status: "fired",
    reason: "Check again",
  });
  entries = appendEvent(entries, notification("to-a", "B", "A"));
  assert.equal(
    groupTranscript(entries).find(
      (item) => item.kind === "thread" && item.thread.id === "A",
    )?.kind,
    "thread",
  );
  assert.equal(
    entries.filter((entry) => entry.kind === "task" && entry.taskId === "A")
      .length,
    1,
  );
  assert.equal(
    entries.find((entry) => entry.kind === "task" && entry.taskId === "A")
      ?.state,
    "Completed",
  );
  entries = appendEvent(entries, started("A", "", "run-2", 1));
  entries = appendEvent(entries, notification("to-main", "A", ""));
  const html = markup(entries);
  assert.deepEqual(
    renderedParents(html),
    new Map([
      ["A", ""],
      ["B", "A"],
    ]),
  );
  assert.equal(
    entries.filter((entry) => entry.kind === "task" && entry.taskId === "A")
      .length,
    2,
  );
  assert.equal(
    entries.filter((entry) => entry.kind === "notification").length,
    2,
  );
  assert.match(html, /Activation 0/);
  assert.match(html, /Activation 1/);
  assert.match(html, /Notification delivered: Agent B to Agent A/);
  assert.match(html, /Notification delivered: Agent A to Main/);
  assert.equal(appendEvent(entries, notification("to-a", "B", "A")), entries);
});

test("child approval creates evidence without stealing a same-ID root call; late call joins by exact owner", () => {
  let entries = reduce(
    {
      event: "tool_call",
      run_id: "run",
      id: "same",
      name: "shell",
      arguments: { command: "root" },
    },
    {
      event: "approval",
      run_id: "run",
      id: "same",
      task_id: "child",
      task_name: "Child",
      tool: "shell",
      arguments: { command: "child" },
      approval_id: "nonce",
    },
  );
  const root = entries[0];
  const provisionalId = entries[1].id;
  assert.equal(entries.length, 2);
  assert.equal(entries[1].taskId, "child");
  entries = appendEvent(entries, {
    event: "approval_closed",
    run_id: "other",
    approval_id: "nonce",
    decision: "allow",
  });
  assert.equal(entries[1].state, "Waiting for approval");
  entries = appendEvent(entries, {
    event: "approval_closed",
    run_id: "run",
    approval_id: "nonce",
    decision: "allow",
  });
  assert.equal(entries[1].state, "Awaiting execution result");
  assert.equal(entries[1].result, undefined);
  entries = appendEvent(entries, {
    event: "tool_call",
    run_id: "run",
    task_id: "child",
    id: "same",
    name: "shell",
    arguments: { command: "child" },
  });
  assert.equal(entries.length, 2);
  assert.equal(entries[1].id, provisionalId);
  assert.equal(entries[1].approval, "Allowed once");
  assert.equal(entries[1].approvalId, "nonce");
  assert.equal(entries[0], root);
  entries = appendEvent(entries, {
    event: "tool_result",
    run_id: "run",
    task_id: "child",
    id: "same",
    result: "child result",
  });
  assert.equal(entries[0].result, undefined);
  assert.equal(entries[1].result, "child result");
});

test("denied and expired provisional approvals keep their exact decision without an execution result", () => {
  for (const expired of [false, true]) {
    const entries = reduce(
      {
        event: "approval",
        run_id: "run",
        id: "call",
        task_id: "child",
        tool: "shell",
        approval_id: "nonce",
      },
      {
        event: "approval_closed",
        run_id: "run",
        approval_id: "nonce",
        decision: "deny",
        expired,
      },
      { event: "run_finished", run_id: "run", status: "completed" },
    );
    assert.equal(entries[0].state, "Approval denied");
    assert.equal(entries[0].approval, expired ? "Expired; denied" : "Denied");
    assert.equal(entries[0].result, undefined);
  }
});

test("late parents and out-of-order completion enrich identity without regressing terminal state", () => {
  let entries = reduce(completed("B"), started("B", "A"));
  const firstId = entries[0].id;
  assert.equal(entries.length, 1);
  assert.equal(entries[0].state, "Completed");
  assert.equal(entries[0].parentTaskId, "A");
  assert.match(markup(entries), /Parent unavailable: A/);
  entries = appendEvent(entries, started("A"));
  assert.equal(entries[0].id, firstId);
  assert.deepEqual(
    renderedParents(markup(entries)),
    new Map([
      ["A", ""],
      ["B", "A"],
    ]),
  );
});

test("missing and cyclic ancestry remains explicitly unresolved rather than fabricated", () => {
  const entries = reduce(
    started("A", "B"),
    started("B", "A"),
    started("orphan", "missing"),
    started("self", "self"),
  );
  const html = markup(entries);
  assert.deepEqual(
    renderedParents(html),
    new Map([
      ["A", ""],
      ["B", ""],
      ["orphan", ""],
      ["self", ""],
    ]),
  );
  assert.match(html, /Cyclic ancestry/);
  assert.match(html, /Parent unavailable: missing/);
  assert.doesNotMatch(html, /Parent: Main/);
});

test("human follow-ups and automatic activations do not overwrite previous execution or notification evidence", () => {
  const entries = reduce(
    started("A"),
    completed("A"),
    {
      event: "task_message",
      run_id: "run-2",
      task_id: "A",
      name: "Agent A",
      followup: 1,
      prompt: "Human request",
    },
    { ...started("A", "", "run-2", 1), followup: 1, trigger: "human" },
    { ...completed("A", "run-2", 1), followup: 1 },
    { ...started("A", "", "run-3", 2), followup: 1, trigger: "wakeup" },
    completed("A"),
  );
  const executions = entries.filter((entry) => entry.kind === "task");
  assert.equal(executions.length, 3);
  assert.equal(executions[0].result, "A finished");
  assert.equal(executions[2].state, "Running");
  assert.equal(entries.filter((entry) => entry.kind === "followup").length, 1);
  assert.equal(renderedParents(markup(entries)).size, 1);
  assert.match(markup(entries), /Human follow-up 1/);
});

test("text, tools, compaction and run termination are actor/activation scoped", () => {
  let entries = reduce(
    { event: "text_chunk", run_id: "r1", chunk: "Main" },
    { event: "text_chunk", run_id: "r1", task_id: "A", chunk: "A" },
    { event: "text_chunk", run_id: "r2", task_id: "B", chunk: "B" },
    { event: "compaction_started", run_id: "r1", task_id: "A" },
    { event: "text_chunk", run_id: "r1", task_id: "A", chunk: "hidden" },
    { event: "text_chunk", run_id: "r1", chunk: " continued" },
    {
      event: "text_chunk",
      run_id: "r1",
      task_id: "A",
      activation: 1,
      chunk: "A resumed",
    },
    {
      event: "tool_call",
      run_id: "r1",
      task_id: "A",
      id: "same",
      name: "read_file",
    },
    {
      event: "tool_call",
      run_id: "r1",
      task_id: "B",
      id: "same",
      name: "read_file",
    },
    {
      event: "tool_output",
      run_id: "r1",
      task_id: "A",
      call_id: "same",
      text: "A output",
    },
    { event: "run_finished", run_id: "r1", status: "completed" },
  );
  const assistants = entries.filter((entry) => entry.kind === "assistant");
  assert.deepEqual(
    assistants.map((entry) => entry.text),
    ["Main continued", "A", "B", "A resumed"],
  );
  assert.deepEqual(
    assistants.map((entry) => entry.streaming),
    [false, false, true, false],
  );
  assert.deepEqual(
    entries.filter((entry) => entry.kind === "tool").map((entry) => entry.text),
    ["A output", ""],
  );
  entries = appendEvent(entries, {
    event: "text_done",
    run_id: "r2",
    task_id: "B",
    text: "B final",
  });
  assert.equal(entries.filter((entry) => entry.kind === "assistant").length, 4);
});

test("schedule acknowledgments, firing and delegation completion have separate visible meanings", () => {
  let entries = reduce(
    {
      event: "tool_call",
      run_id: "r",
      id: "schedule",
      name: "schedule_wakeup",
      arguments: { seconds: 1 },
    },
    {
      event: "tool_result",
      run_id: "r",
      id: "schedule",
      result: { status: "scheduled" },
    },
    {
      event: "wakeup",
      run_id: "r",
      wakeup_id: "w",
      task_id: "B",
      status: "scheduled",
      due_at: "2030-01-01T00:00:00Z",
      reason: "Review",
    },
    { event: "tool_call", run_id: "r", id: "delegate", name: "delegate" },
    {
      event: "tool_result",
      run_id: "r",
      id: "delegate",
      result: { task_id: "B", status: "running" },
    },
  );
  const html = markup(entries);
  assert.match(html, /Scheduling request completed/);
  assert.match(html, /Delegation request completed/);
  assert.match(html, /Scheduled/);
  assert.doesNotMatch(html, />Fired</);
  entries = appendEvent(entries, {
    event: "wakeup",
    run_id: "later",
    wakeup_id: "w",
    task_id: "B",
    status: "fired",
  });
  entries = appendEvent(entries, {
    event: "wakeup",
    wakeup_id: "w",
    task_id: "B",
    status: "scheduled",
  });
  assert.equal(entries.filter((entry) => entry.kind === "wakeup").length, 1);
  assert.equal(
    entries.find((entry) => entry.kind === "wakeup")?.state,
    "Fired",
  );
  assert.equal(entries.filter((entry) => entry.kind === "task").length, 0);
});

test("streamed token changes do not create per-token activity announcements", () => {
  let entries = reduce(started("A"));
  const revision = entries[0].activity;
  entries = appendEvent(entries, {
    event: "text_chunk",
    run_id: "run-1",
    task_id: "A",
    chunk: "one",
  });
  entries = appendEvent(entries, {
    event: "text_chunk",
    run_id: "run-1",
    task_id: "A",
    chunk: "two",
  });
  assert.equal(entries[0].activity, revision);
  assert.equal(entries[1].activity, undefined);
  assert.match(markup(entries), /aria-live="polite"/);
  assert.equal(appendEvent(entries, started("A")), entries);
});

test("background batches preserve foreground output, show explicit gaps and retain timer evidence after it leaves pending", () => {
  const foreground = reduce({
    event: "text_chunk",
    run_id: "foreground",
    chunk: "Keep this response",
  });
  let entries = appendActivity(foreground, {
    session_id: "session",
    cursor: 10,
    active_run_id: "background",
    truncated: true,
    events: [started("A", "", "background")],
    pending_wakeups: [
      {
        wakeup_id: "w",
        task_id: "A",
        due_at: "2030-01-01T00:00:00Z",
        reason: "Review",
      },
    ],
  });
  assert.equal(entries[0], foreground[0]);
  assert.equal(entries[0].streaming, true);
  assert.equal(
    entries.find((entry) => entry.kind === "task")?.state,
    "Running",
  );
  assert.match(markup(entries), /Background activity gap/);
  assert.ok(entries.find((entry) => entry.level === "warning")?.activity);
  entries = appendActivity(entries, {
    session_id: "session",
    cursor: 11,
    active_run_id: "",
    truncated: false,
    events: [
      { event: "run_finished", run_id: "background", status: "completed" },
    ],
    pending_wakeups: [],
  });
  assert.equal(entries[0].streaming, true);
  assert.equal(
    entries.find((entry) => entry.kind === "task")?.state,
    "No result recorded",
  );
  assert.equal(
    entries.find((entry) => entry.kind === "wakeup")?.state,
    "Scheduled",
  );
});

test("an unscoped finish cannot terminate a named run", () => {
  const entries = reduce(
    { event: "text_chunk", run_id: "owned", chunk: "Still working" },
    { event: "run_finished", status: "completed" },
  );
  assert.equal(entries[0].streaming, true);
});

test("the legacy wake_up_in alias is visibly a scheduling request rather than a fired timer", () => {
  const entries = reduce(
    { event: "tool_call", id: "legacy", name: "wake_up_in" },
    { event: "tool_result", id: "legacy", result: { status: "scheduled" } },
  );
  assert.match(markup(entries), /Scheduling request completed/);
  assert.equal(
    entries.some((entry) => entry.kind === "wakeup"),
    false,
  );
});

test("deep ancestry keeps every real parent without making duplicate task identities", () => {
  const entries = reduce(
    ...Array.from({ length: 8 }, (_, index) =>
      started(`task-${index}`, index ? `task-${index - 1}` : ""),
    ),
  );
  const parents = renderedParents(markup(entries));
  assert.equal(parents.size, 8);
  for (let index = 1; index < 8; index++)
    assert.equal(parents.get(`task-${index}`), `task-${index - 1}`);
});

test("completed root read survives a denied grandchild write with the same call ID", () => {
  let entries = reduce(
    {
      event: "tool_call",
      run_id: "run-1",
      id: "shared-call",
      name: "read_file",
      arguments: { path: "root.txt" },
    },
    {
      event: "tool_result",
      run_id: "run-1",
      id: "shared-call",
      name: "read_file",
      result: "Root read succeeded",
    },
    started("A"),
    started("B", "A"),
    started("sibling"),
  );
  const root = entries[0];
  entries = appendEvent(entries, {
    event: "approval",
    run_id: "run-1",
    id: "shared-call",
    tool: "write_file",
    task_id: "B",
    task_name: "Agent B",
    depth: 2,
    activation: 0,
    approval_id: "grandchild-denial",
    arguments: { path: "child.txt", content: "Not written" },
  });
  entries = appendEvent(entries, {
    event: "approval_closed",
    run_id: "run-1",
    approval_id: "grandchild-denial",
    decision: "deny",
  });
  entries = appendEvent(entries, {
    event: "run_finished",
    run_id: "run-1",
    status: "completed",
  });
  assert.equal(entries[0], root);
  assert.equal(root.state, "Completed");
  assert.equal(root.taskId, "");
  assert.equal(root.title, "read_file");
  assert.equal(root.result, "Root read succeeded");
  assert.equal(root.approval, undefined);
  assert.match(root.inputs || "", /root.txt/);
  const child = entries.find(
    (entry) => entry.kind === "tool" && entry.taskId === "B",
  )!;
  assert.equal(child.title, "write_file");
  assert.equal(child.approval, "Denied");
  assert.equal(child.result, undefined);
  assert.equal(child.state, "Approval denied");
  assert.match(child.inputs || "", /child.txt/);
  assert.deepEqual(
    renderedParents(markup(entries)),
    new Map([
      ["A", ""],
      ["B", "A"],
      ["sibling", ""],
    ]),
  );
});

test("same run, task and call ID remain isolated by activation, including late calls and default activation zero", () => {
  let entries = reduce(
    {
      event: "approval",
      run_id: "run",
      task_id: "A",
      id: "shared",
      activation: 0,
      approval_id: "old-nonce",
      tool: "write_file",
      arguments: { path: "old.txt" },
    },
    {
      event: "approval",
      run_id: "run",
      task_id: "A",
      id: "shared",
      activation: 1,
      approval_id: "new-nonce",
      tool: "write_file",
      arguments: { path: "new.txt" },
    },
    {
      event: "approval_closed",
      run_id: "run",
      approval_id: "old-nonce",
      decision: "deny",
    },
    {
      event: "approval_closed",
      run_id: "run",
      approval_id: "new-nonce",
      decision: "allow",
    },
  );
  const [oldId, newId] = entries.map((entry) => entry.id);
  entries = appendEvent(entries, {
    event: "tool_call",
    run_id: "run",
    task_id: "A",
    id: "shared",
    name: "write_file",
    arguments: { path: "old.txt" },
  });
  entries = appendEvent(entries, {
    event: "tool_call",
    run_id: "run",
    task_id: "A",
    id: "shared",
    activation: 1,
    name: "write_file",
    arguments: { path: "new.txt" },
  });
  entries = appendEvent(entries, {
    event: "tool_result",
    run_id: "run",
    task_id: "A",
    id: "shared",
    activation: 1,
    result: "New result",
  });
  entries = appendEvent(entries, {
    event: "tool_output",
    run_id: "run",
    task_id: "A",
    call_id: "shared",
    text: "Old output",
  });
  assert.equal(entries.length, 2);
  assert.deepEqual(
    entries.map((entry) => entry.id),
    [oldId, newId],
  );
  assert.equal(entries[0].activation, 0);
  assert.equal(entries[0].approvalId, "old-nonce");
  assert.equal(entries[0].approval, "Denied");
  assert.equal(entries[0].text, "Old output");
  assert.equal(entries[0].result, undefined);
  assert.equal(entries[1].activation, 1);
  assert.equal(entries[1].approvalId, "new-nonce");
  assert.equal(entries[1].approval, "Allowed once");
  assert.equal(entries[1].text, "");
  assert.equal(entries[1].result, "New result");
});

test("approval dialog includes the requested child activation", () => {
  const html = renderToStaticMarkup(
    createElement(ApprovalDialog, {
      approval: {
        run_id: "run",
        approval_id: "nonce",
        call_id: "shared",
        tool: "write_file",
        description: "Review child write",
        arguments: {},
        preview: "",
        task_id: "B",
        task_name: "Agent B",
        depth: 2,
        activation: 3,
      },
      busy: false,
      error: "",
      decide: () => undefined,
      cancel: () => undefined,
    }),
  );
  assert.match(html, /Agent B \(B\), depth 2, activation 3/);
});
