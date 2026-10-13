import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { ExecutionValue } from "./ExecutionValue.js";
import { ActivityRecord } from "./ActivityRecord.js";
import { appendEvent } from "./transcript.js";
import { presentToolValue, toolPresentation } from "./toolPresentation.js";

const readResult = {
  path: "/workspace/notes.md", content: "1:alpha\n2:beta", sha256: "abc123", total_lines: 2,
  truncated: false, instructions: "Read the requested file", newline_style: "LF",
  newline_counts: { LF: 2, CRLF: 0, CR: 0 },
};
const scheduleResult = {
  wakeup_id: "wake-1", task_id: "task-1", due_at: "2026-10-15T12:00:00Z", reason: "Review the draft",
  status: "scheduled", session_id: "session-1", run_id: "run-1",
};

test("native tools and MCP methods have distinct labels, icons, and origin metadata", () => {
  const native = [
    ["shell", "Run command", "terminal", "command"], ["read_file", "Read file", "file-read", "read"],
    ["edit_file", "Edit file", "edit", "edit"], ["write_file", "Write file", "file-write", "write"],
    ["list_directory", "List directory", "folder", "list"], ["search_files", "Search files", "search", "search"],
    ["skill", "Load skill", "book", "skill"], ["delegate", "Delegate task", "team", "team"],
    ["compact_history", "Compact history", "compact", "compact"], ["channel_send", "Send to channel", "channels", "channel"],
    ["schedule_wakeup", "Schedule wake-up", "clock", "schedule"],
  ] as const;
  for (const [name, label, icon, family] of native)
    assert.deepEqual(toolPresentation(name), { label, icon, family });
  assert.deepEqual(toolPresentation("mcp__drive__search_files"), {
    label: "drive · search_files", icon: "plug", family: "mcp", origin: "drive", method: "search_files",
  });
  assert.deepEqual(toolPresentation("mcp__server__method__v2"), {
    label: "server · method__v2", icon: "plug", family: "mcp", origin: "server", method: "method__v2",
  });
  for (const name of ["custom_tool", "mcp__broken", "mcp____method", "mcp__server__"]) {
    assert.deepEqual(toolPresentation(name), { label: name, icon: "tools", family: "generic" });
  }
  assert.deepEqual(presentToolValue('{"ok":true}', "mcp__drive__search_files").metadata, [
    { label: "MCP connection", text: "drive" }, { label: "Method", text: "search_files" },
  ]);
  const call = appendEvent([], { event: "tool_call", id: "read", name: "read_file", arguments: { path: "notes.md" } })[0];
  const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry: call, open: true })));
  try {
    assert.equal(dom.window.document.querySelector(".execution-name")?.textContent, "Read file");
    assert.equal(dom.window.document.querySelector(".tool-record")?.getAttribute("data-tool-family"), "read");
    assert.equal(dom.window.document.querySelector(".tool-symbol svg")?.getAttribute("aria-hidden"), "true");
    assert.equal(dom.window.document.querySelector('[role="tab"]')?.textContent, "Inputs");
  } finally { dom.window.close(); }
});

test("native file and wake-up requests show their purpose and keep input versus result labels clear", () => {
  const read = presentToolValue(JSON.stringify(readResult), "read_file");
  assert.equal(read.format, "File content");
  assert.deepEqual(read.sections?.map(({ label }) => label), ["Content", "Instructions"]);
  assert.deepEqual(read.metadata?.slice(0, 2), [
    { label: "Path", text: "/workspace/notes.md" }, { label: "Returned lines", text: "1–2" },
  ]);

  const edit = presentToolValue(JSON.stringify({ path: "a.txt", old: "before", new: "after" }), "edit_file", "inputs");
  assert.equal(edit.format, "Proposed edit");
  assert.deepEqual(edit.sections?.map(({ label }) => label), ["Original text to match", "Proposed replacement"]);

  const write = presentToolValue(JSON.stringify({ path: "b.txt", content: "draft" }), "write_file", "inputs");
  assert.equal(write.sections?.[0].label, "Proposed file content");
  const scheduleInput = presentToolValue(JSON.stringify({ reason: "Review the draft", minutes: 5 }), "schedule_wakeup", "inputs");
  assert.equal(scheduleInput.format, "Wake-up request");
  assert.deepEqual(scheduleInput.metadata?.[0], { label: "Delay", text: "300 seconds" });
  assert.match(scheduleInput.note || "", /reports firing/);

  const schedule = presentToolValue(JSON.stringify(scheduleResult), "schedule_wakeup");
  assert.equal(schedule.format, "Scheduling acknowledgement");
  assert.deepEqual(schedule.metadata?.slice(0, 2), [
    { label: "Status", text: "scheduled" }, { label: "Due at", text: scheduleResult.due_at },
  ]);
  assert.match(schedule.note || "", /firing is reported in wake-up activity/);
  assert.doesNotMatch(schedule.note || "", /wake-up fired/i);

  const diff = "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n-before\n+after\n";
  for (const toolName of ["edit_file", "write_file"]) {
    const result = presentToolValue(JSON.stringify({ path: "a.txt", diff, changed: true }), toolName);
    assert.equal(result.format, "File changes");
    assert.equal(result.sections?.[0].text, diff);
    assert.equal(result.sections?.[0].kind, "diff");
    assert.deepEqual(result.metadata, [{ label: "Path", text: "a.txt" }]);
  }
  const listing = presentToolValue(JSON.stringify({ paths: ["a.txt", "b.txt"], truncated: false, instructions: "Pick a file" }), "list_files");
  assert.equal(listing.sections?.[0].text, "a.txt\nb.txt");
  assert.equal(listing.sections?.[0].kind, "list");
  assert.deepEqual(listing.metadata, [{ label: "Paths returned", text: "2" }]);
  const search = presentToolValue(JSON.stringify({ matches: ["a.txt:1: before"], truncated: false, skipped_files: 3 }), "search_files");
  assert.equal(search.sections?.[0].text, "a.txt:1: before");
  assert.deepEqual(search.metadata, [{ label: "Matches returned", text: "1" }, { label: "Skipped files", text: "3" }]);
});

test("malformed, unknown, duplicate, nested duplicate, and unsafe integer values retain every JSON token", () => {
  const samples = [
    '{"path":"a","content":"x","sha256":"sum","total_lines":1,"truncated":false,"instructions":"read","newline_style":"LF","newline_counts":{"LF":1,"CRLF":0,"CR":0},"diagnostic":"keep me"}',
    '{"path":"a","path":"b","content":"x","sha256":"sum","total_lines":1,"truncated":false,"instructions":"read","newline_style":"LF","newline_counts":{"LF":1,"CRLF":0,"CR":0}}',
    '{"path":"a","content":"x","sha256":"sum","total_lines":1,"truncated":false,"instructions":"read","newline_style":"LF","newline_counts":{"LF":1,"LF":2,"CRLF":0,"CR":0}}',
    '{"path":"a","content":"x","sha256":"sum","total_lines":9007199254740993,"truncated":false,"instructions":"read","newline_style":"LF","newline_counts":{"LF":1,"CRLF":0,"CR":0}}',
    '{"path":',
  ];
  for (const source of samples) {
    const view = presentToolValue(source, "read_file");
    assert.equal(view.sections, undefined, `must not reinterpret ${source}`);
    for (const significant of source.match(/"(?:[^"\\]|\\.)*"|-?\d+/g) || [])
      assert.ok(view.text.includes(significant), `${significant} is retained in ${view.text}`);
  }
  const unsafeInputs = '{"path":"a","start_line":9007199254740993}';
  assert.equal(presentToolValue(unsafeInputs, "read_file", "inputs").metadata, undefined);
});

test("native content remains inert text and scheduling requests stay separate from fired activity", () => {
  const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  let scheduleEntries = appendEvent([], {
    event: "tool_call", run_id: "run", id: "schedule", name: "schedule_wakeup", arguments: { reason: "Review the draft", minutes: 5 },
  });
  scheduleEntries = appendEvent(scheduleEntries, { event: "tool_execution_started", run_id: "run", id: "schedule", name: "schedule_wakeup" });
  scheduleEntries = appendEvent(scheduleEntries, { event: "tool_result", run_id: "run", id: "schedule", result: scheduleResult });
  const entry = scheduleEntries.find((item) => item.kind === "tool")!;
  const markup = renderToStaticMarkup(createElement(ExecutionRecord, { entry, open: true }));
  const dom = new JSDOM(markup);
  try {
    assert.equal(dom.window.document.querySelector("img, script"), null);
    assert.equal(dom.window.document.querySelector(".execution-state")?.textContent, "Request completed");
    assert.doesNotMatch(dom.window.document.querySelector(".execution-state")?.textContent || "", /Fired/);
    assert.equal(dom.window.document.querySelector('[data-panel="result"] .tool-value-metadata')?.textContent?.includes("scheduled"), true);
    assert.match(dom.window.document.body.textContent || "", /firing is reported in wake-up activity/i);

    let wakeupEntries = appendEvent([], { event: "wakeup", wakeup_id: "wake-1", status: "scheduled", due_at: scheduleResult.due_at, reason: scheduleResult.reason });
    assert.equal(wakeupEntries[0].state, "Scheduled");
    wakeupEntries = appendEvent(wakeupEntries, { event: "wakeup", wakeup_id: "wake-1", status: "fired", due_at: scheduleResult.due_at, reason: scheduleResult.reason });
    wakeupEntries = appendEvent(wakeupEntries, { event: "run_finished", run_id: "run", status: "completed" });
    assert.equal(wakeupEntries.find((item) => item.kind === "wakeup")?.state, "Fired");
    const fired = wakeupEntries.find((item) => item.kind === "wakeup")!;
    const activityDom = new JSDOM(renderToStaticMarkup(createElement(ActivityRecord, { entry: fired, open: true, toggle() {} })));
    try {
      assert.equal(activityDom.window.document.querySelector(".activity-record")?.getAttribute("data-activity-kind"), "wakeup");
      assert.equal(activityDom.window.document.querySelector(".activity-symbol svg")?.getAttribute("aria-hidden"), "true");
      assert.match(activityDom.window.document.body.textContent || "", /Wake-up/);
      assert.equal(activityDom.window.document.querySelector(".execution-state")?.textContent, "Fired");
    } finally { activityDom.window.close(); }

    const hostileEntry = appendEvent([], {
      event: "tool_result", id: "read", name: "read_file", result: JSON.stringify({ ...readResult, content: hostile }),
    })[0];
    const hostileDom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry: hostileEntry, open: true })));
    try {
      assert.equal(hostileDom.window.document.querySelector("img, script"), null);
      assert.equal(hostileDom.window.document.querySelector('[aria-label="Result: content"]')?.textContent, hostile);
    } finally { hostileDom.window.close(); }
  } finally { dom.window.close(); }
});

test("formatted input views keep raw display and copy byte-for-byte exact", async () => {
  const source = ' { "path" : "notes.md", "start_line" : 9007199254740993 }\n';
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  let copied = "";
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (value: string) => { copied = value; } } } });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(ExecutionValue, { label: "Inputs", text: source, formatInputs: true, toolName: "read_file" })));
    const pre = container.querySelector("pre")!;
    assert.equal(pre.textContent, source, "unsafe integer inputs retain their exact raw representation");
    const copy = container.querySelector<HTMLButtonElement>('[aria-label="Copy inputs"]')!;
    await act(async () => copy.click());
    assert.equal(copied, source);
    const raw = container.querySelector<HTMLButtonElement>('[aria-label="Show raw inputs"]')!;
    await act(async () => raw.click());
    assert.equal(pre.textContent, source);
    assert.equal(container.querySelector("pre"), pre);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "navigator", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("live tool output retains its pre and code nodes, tab choice, focus, and manual close", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  let open = true;
  let entry = { id: "stream", kind: "tool" as const, title: "read_file", state: "Receiving output", text: "first line\n",
    inputs: JSON.stringify({ path: "notes.md" }) };
  const render = () => act(async () => root.render(createElement(ExecutionRecord, {
    entry, open, toggle(value) { open = value; },
  })));
  try {
    await render();
    const record = container.querySelector<HTMLDetailsElement>(".tool-record")!;
    const stream = container.querySelector<HTMLElement>('[data-panel="stream"]')!;
    const pre = stream.querySelector("pre")!;
    const code = pre.querySelector("code")!;
    pre.focus();
    pre.scrollTop = 31;
    entry = { ...entry, text: `${entry.text}second line\n` };
    await render();
    assert.equal(container.querySelector("pre"), pre);
    assert.equal(pre.querySelector("code"), code);
    assert.equal(pre.textContent, "first line\nsecond line\n");
    assert.equal(dom.window.document.activeElement, pre);
    assert.equal(pre.scrollTop, 31);

    const inputs = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((tab) => tab.textContent === "Inputs")!;
    await act(async () => inputs.click());
    inputs.focus();
    assert.equal(container.querySelector<HTMLElement>('[data-panel="inputs"]')?.hidden, false);
    assert.equal(container.querySelector("pre"), pre, "switching tabs keeps the stream reading surface mounted");
    entry = { ...entry, text: `${entry.text}third line\n` };
    await render();
    assert.equal(container.querySelector("pre"), pre);
    assert.equal(pre.querySelector("code"), code);
    assert.equal(dom.window.document.activeElement, inputs, "stream updates do not steal focus from a chosen tab");

    record.open = false;
    record.dispatchEvent(new dom.window.Event("toggle"));
    await render();
    entry = { ...entry, text: `${entry.text}fourth line\n` };
    await render();
    assert.equal(record.open, false, "a live update does not undo a manual close");
    assert.equal(container.querySelector("pre"), pre);
    assert.equal(pre.textContent, "first line\nsecond line\nthird line\nfourth line\n");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("native metadata disclosure toggles without closing the tool or changing its selected tab", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const entry = appendEvent([], { event: "tool_result", id: "read", name: "read_file", result: readResult })[0];
  try {
    await act(async () => root.render(createElement(ExecutionRecord, { entry, open: true })));
    const record = container.querySelector<HTMLDetailsElement>(".tool-record")!;
    const metadata = container.querySelector<HTMLDetailsElement>(".tool-value-diagnostics")!;
    assert.equal(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent, "Result");
    metadata.open = true;
    metadata.dispatchEvent(new dom.window.Event("toggle", { bubbles: true }));
    assert.equal(record.open, true);
    assert.equal(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent, "Result");
    metadata.open = false;
    metadata.dispatchEvent(new dom.window.Event("toggle", { bubbles: true }));
    assert.equal(record.open, true);
    assert.equal(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent, "Result");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
