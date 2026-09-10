import assert from "node:assert/strict";
import test from "node:test";
import { appendEvent, fromHistory } from "./transcript.js";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MessageContent } from "./MessageContent.js";

test("text finalization does not duplicate streamed text and errors retain it", () => {
  let entries = appendEvent([], { event: "text_chunk", chunk: "partial" });
  entries = appendEvent(entries, { event: "text_done", text: "partial text" });
  entries = appendEvent(entries, { event: "error", message: "disconnected" });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].text, "partial text");
});

test("tool output is matched by call ID rather than tool name", () => {
  let entries = appendEvent([], {
    event: "tool_call",
    id: "one",
    name: "read_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "tool_call",
    id: "two",
    name: "read_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "tool_output",
    call_id: "one",
    text: "first result",
  });
  assert.match(entries[0].text, /first result/);
  assert.doesNotMatch(entries[1].text, /first result/);
});

test("background notices do not split or duplicate an assistant message", () => {
  let entries = appendEvent([], { event: "text_chunk", chunk: "part" });
  entries = appendEvent(entries, {
    event: "notice",
    text: "background task completed",
  });
  entries = appendEvent(entries, { event: "text_chunk", chunk: " two" });
  entries = appendEvent(entries, { event: "text_done", text: "part two" });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].text, "part two");
});

test("a new run never appends to a cancelled partial response", () => {
  let entries = appendEvent([], { event: "text_chunk", chunk: "partial" });
  entries = appendEvent(entries, {
    event: "run_finished",
    status: "cancelled",
  });
  entries = appendEvent(entries, {
    event: "text_chunk",
    chunk: "new response",
  });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].text, "partial");
});

test("saved tool results rejoin their call rather than duplicating activity", () => {
  const entries = fromHistory({
    session_id: "saved-session",
    retained_tasks: [],
    history: [
      {
        role: "assistant",
        content: "",
        name: "",
        tool_call_id: "",
        tool_calls: [
          { id: "one", name: "read_file", arguments: { path: "example" } },
        ],
      },
      {
        role: "tool",
        content: "saved result",
        name: "read_file",
        tool_call_id: "one",
        tool_calls: [],
      },
    ],
  });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].result, "saved result");
  assert.equal(entries[0].state, "Recorded result");
});

test("call inputs remain unchanged when output, error and duration arrive", () => {
  let entries = appendEvent([], {
    event: "tool_call",
    id: "one",
    name: "shell",
    arguments: { command: "test" },
  });
  const inputs = entries[0].inputs;
  entries = appendEvent(entries, {
    event: "tool_output",
    call_id: "one",
    text: "first\n",
  });
  entries = appendEvent(entries, {
    event: "tool_output",
    call_id: "one",
    text: "second\n",
  });
  entries = appendEvent(entries, {
    event: "tool_result",
    id: "one",
    result: { exit_code: 1 },
    error: "Test failed",
    duration_ms: 43,
  });
  assert.equal(entries[0].inputs, inputs);
  assert.equal(entries[0].text, "first\nsecond\n");
  assert.equal(entries[0].state, "Error");
  assert.equal(entries[0].error, "Test failed");
  assert.equal(entries[0].durationMs, 43);
});

test("parallel task records retain identity and complete independently", () => {
  let entries = appendEvent([], {
    event: "task_started",
    task_id: "child-a",
    name: "review",
    prompt: "Inspect a",
    parent_task_id: "parent",
    depth: 2,
  });
  entries = appendEvent(entries, {
    event: "task_started",
    task_id: "child-b",
    name: "review",
    prompt: "Inspect b",
    parent_task_id: "parent",
    depth: 2,
  });
  entries = appendEvent(entries, {
    event: "task_completed",
    task_id: "child-b",
    name: "review",
    result: "b done",
  });
  assert.equal(entries.length, 2);
  assert.equal(entries[0].state, "Running");
  assert.equal(entries[1].state, "Completed");
  assert.equal(entries[1].inputs, "Inspect b");
  assert.equal(entries[1].parentTaskId, "parent");
  assert.equal(entries[1].depth, 2);
});

test("missing results never become a fabricated success", () => {
  const pending = appendEvent([], {
    event: "tool_call",
    id: "one",
    name: "edit_file",
    arguments: {},
  });
  assert.equal(
    appendEvent(pending, { event: "run_finished", status: "cancelled" })[0]
      .state,
    "Cancelled",
  );
  assert.equal(
    appendEvent(pending, { event: "client_disconnected" })[0].state,
    "Disconnected",
  );
  assert.equal(
    appendEvent(pending, { event: "run_finished", status: "completed" })[0]
      .state,
    "No result recorded",
  );
});

test("approval decisions stay attached to their exact call and nonce", () => {
  let entries = appendEvent([], {
    event: "tool_call",
    id: "one",
    name: "edit_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "approval",
    id: "one",
    approval_id: "pending",
  });
  assert.equal(entries[0].state, "Waiting for approval");
  entries = appendEvent(entries, {
    event: "approval_closed",
    approval_id: "other",
    decision: "allow",
  });
  assert.equal(entries[0].state, "Waiting for approval");
  entries = appendEvent(entries, {
    event: "approval_closed",
    approval_id: "pending",
    decision: "deny",
  });
  assert.equal(entries[0].approval, "Denied");
});

test("call IDs reused in different runs do not overwrite older evidence", () => {
  let entries = appendEvent([], {
    event: "tool_call",
    run_id: "old",
    id: "same",
    name: "read_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "tool_call",
    run_id: "new",
    id: "same",
    name: "read_file",
    arguments: {},
  });
  entries = appendEvent(entries, {
    event: "tool_result",
    run_id: "new",
    id: "same",
    result: "new result",
  });
  assert.equal(entries[0].result, undefined);
  assert.equal(entries[1].result, "new result");
});

test("fenced code preserves indentation and escapes model HTML", () => {
  const markup = renderToStaticMarkup(
    createElement(MessageContent, {
      text: "Explanation\n```python\n  print('<script>')\n```\nAfter",
    }),
  );
  assert.match(markup, /<code>  print\(&#x27;&lt;script&gt;&#x27;\)\n<\/code>/);
  assert.match(markup, /Explanation/);
  assert.match(markup, /After/);
  assert.doesNotMatch(markup, /<script>/);
});

test("empty and null tool results remain distinguishable", () => {
  assert.equal(
    appendEvent([], { event: "tool_result", id: "empty", result: "" })[0]
      .result,
    "",
  );
  assert.equal(
    appendEvent([], { event: "tool_result", id: "null", result: null })[0]
      .result,
    "null",
  );
});
