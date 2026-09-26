import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { Conversation } from "./Conversation.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { executionName, executionStatus, executionTarget } from "./executionPresentation.js";
import { LiveSessions } from "./liveTranscript.js";
import type { Entry } from "./transcript.js";
import type { WireEvent } from "../../types.js";

test("summary targets are bounded plain text selected from recognized inputs only", () => {
  const entry: Entry = { id: "one", kind: "tool", text: "", inputs: JSON.stringify({ command: "<b>run</b>\n\u202e" + "x".repeat(200) }) };
  const target = executionTarget(entry);
  assert.equal(target.length, 120);
  assert.doesNotMatch(target, /[\n\u202e]/);
  const html = renderToStaticMarkup(createElement(ExecutionRecord, { entry }));
  assert.match(html, /&lt;b&gt;run&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<b>/);
  for (const inputs of ["partial {", '{"password":"secret"}', '["command"]', 'null'])
    assert.equal(executionTarget({ ...entry, inputs }), "");
  assert.equal(executionTarget({ ...entry, inputs: '{"path":"src/example.ts"}' }), "src/example.ts");
  assert.equal(executionTarget({ ...entry, title: "channel_send", inputs: '{"channel":"telegram","destination":"chat-123","text":"Private message","token":"secret"}' }), "telegram · chat-123");
  assert.equal(executionName("channel_send"), "Send to channel");
  assert.equal(executionName("constructor"), "constructor");
});

test("compact summaries retain long names, exact inputs, approval state and duration in the inspector", () => {
  for (const title of ["read_file", "shell", "custom_".repeat(40)]) {
    const inputs = JSON.stringify({ path: "/long-segment".repeat(80), command: "python " + "argument ".repeat(80) });
    const html = renderToStaticMarkup(createElement(ExecutionRecord, {
      entry: { id: title, kind: "tool", title, inputs, text: "", state: "Waiting for approval", durationMs: 1200 },
    }));
    const dom = new JSDOM(html);
    const summary = dom.window.document.querySelector("summary")!;
    assert.equal(summary.querySelector(".execution-target")!.getAttribute("title"), executionTarget({ id: title, kind: "tool", inputs, text: "" }));
    assert.equal(summary.querySelector(".execution-name")!.getAttribute("title"), title);
    assert.equal(summary.querySelector(".execution-state")!.textContent, "Needs approval");
    assert.equal(summary.querySelector(".execution-duration")!.textContent, "1.2 s");
    assert.equal(dom.window.document.querySelector(".execution-record")!.hasAttribute("open"), false);
    assert.ok(dom.window.document.querySelector('[data-panel="inputs"]')!.textContent!.includes(inputs));
    assert.equal(dom.window.document.querySelectorAll("details").length, 1, "One disclosure, without nested accordions");
    dom.window.close();
  }
});

test("saved results and delegated requests never imply an unobserved successful execution", () => {
  const entry: Entry = { id: "saved", kind: "tool", title: "channel_send", text: "", state: "Recorded result", result: "{'message_ids': ['123']}" };
  assert.deepEqual(executionStatus(entry), { label: "Saved result", tone: "neutral" });
  assert.deepEqual(executionStatus({ ...entry, error: "Recorded error" }), { label: "Saved error", tone: "error" });
  assert.deepEqual(executionStatus({ ...entry, state: "Completed" }), { label: "Completed", tone: "complete" });
  for (const title of ["delegate", "schedule_wakeup", "wake_up_in"]) {
    assert.equal(executionStatus({ ...entry, title, state: "Completed" }).label, "Request completed");
  }
  const html = renderToStaticMarkup(createElement(ExecutionRecord, { entry, open: true }));
  assert.match(html, /Original execution status and timing may be unavailable/);
  assert.doesNotMatch(html, /data-state="Completed"/);
});

test("mounted conversation preserves disclosure choices and nodes across progress, terminal events and replay", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    for (const failure of [false, true]) {
      const cache = new LiveSessions();
      let cursor = 0;
      const runId = failure ? "failed-run" : "completed-run";
      const record = (event: WireEvent) => ({ ...event, run_id: runId, call_id: "call" });
      const receive = (event: WireEvent) => cache.receive({ type: "event", epoch: "one", cursor: ++cursor, session_id: "root", record: record(event) });
      const render = async () => {
        await act(async () => root.render(createElement(Conversation, {
          key: runId,
          entries: cache.get("root").entries, sessionId: "root", demo: false, canSubmit: false, submit: () => assert.fail("unexpected submit"),
        })));
      };
      const toggle = async (details: HTMLDetailsElement, open: boolean) => {
        await act(async () => {
          details.open = open;
          details.dispatchEvent(new dom.window.Event("toggle"));
        });
      };
      receive({ event: "tool_call", name: "shell", arguments: { command: "slow command" } });
      await render();
      const tool = container.querySelector<HTMLDetailsElement>(".execution-record")!;
      const metadata = tool.querySelector<HTMLElement>('[data-panel="metadata"]')!;
      const tab = (label: string) => [...tool.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((button) => button.textContent === label)!;
      const key = tool.dataset.disclosureKey;
      assert.equal(tool.open, false);
      assert.equal(metadata.hidden, true);
      await toggle(tool, true);
      await act(async () => tab("Details").click());
      receive({ event: "approval", approval_id: "nonce", tool: "shell" });
      await render();
      assert.match(tool.querySelector("summary")!.textContent!, /Needs approval/);
      receive({ event: "approval_closed", approval_id: "nonce", decision: "allow" });
      const output = "unique evidence line\n".repeat(300) + "FINAL RECEIVED LINE";
      receive({ event: "tool_output", text: output });
      await render();
      assert.equal(container.querySelector(".execution-record"), tool);
      assert.equal(tool.dataset.disclosureKey, key);
      assert.equal(tool.open, true);
      assert.equal(metadata.hidden, false, "Incoming output does not replace the selected tab");
      const stream = tool.querySelector<HTMLElement>('[data-panel="stream"]')!;
      assert.equal(stream.hidden, true);
      await act(async () => tab("Output").click());
      const reading = stream.querySelector("pre")!;
      reading.focus();
      reading.scrollTop = 77;
      if (failure) await toggle(tool, false);
      const terminal = record({ event: "tool_result", result: failure ? "partial result" : output, error: failure ? "Exit code 7" : "", duration_ms: 1200 });
      receive(terminal);
      await render();
      assert.equal(tool.open, !failure);
      if (!failure) {
        assert.equal(dom.window.document.activeElement, reading, "Promotion preserves the focused reading node");
        assert.equal(reading.scrollTop, 77);
      }
      assert.equal(metadata.hidden, true);
      assert.match(tool.querySelector("summary")!.textContent!, failure ? /Error/ : /Completed/);
      assert.match(metadata.textContent!, /ApprovalAllowed once/);
      assert.match(tool.querySelector("summary")!.textContent!, /1.2 s/);
      cache.receive({ type: "event", epoch: "one", cursor, session_id: "root", record: terminal });
      await render();
      assert.equal(container.querySelectorAll(".execution-record").length, 1);
      assert.equal(container.querySelector(".execution-record"), tool);
      assert.equal(tool.open, !failure);
      await toggle(tool, true);
      assert.ok(tool.textContent!.includes("FINAL RECEIVED LINE"));
      if (failure) {
        assert.equal(tool.querySelector('[data-panel="stream"]'), stream);
        assert.equal(stream.hidden, false);
        assert.match(tool.textContent!, /Exit code 7/);
      } else {
        assert.equal(tool.querySelector('[data-panel="stream"]'), stream);
        assert.equal(tool.querySelector('[data-panel="result"]'), null);
        assert.equal(stream.hidden, false);
        assert.equal(stream.querySelector("pre"), reading);
        assert.equal(dom.window.document.activeElement, reading);
        assert.equal(tab("Result").getAttribute("aria-selected"), "true");
        assert.equal(reading.getAttribute("aria-label"), "Result");
      }
      assert.ok(tool.textContent!.includes("Inputs"));
    }
    let nested: Entry[] = [
      { id: "earlier", kind: "user", text: "Earlier message" },
      { id: "parent", kind: "task", taskId: "parent", text: "", state: "Running" },
      { id: "child", kind: "task", taskId: "child", parentTaskId: "parent", text: "", state: "Running" },
      { id: "nested-tool", kind: "tool", taskId: "child", text: "", title: "shell", state: "Requested" },
    ];
    const renderNested = async () => act(async () => root.render(createElement(Conversation, {
      key: "nested", entries: nested, sessionId: "nested", demo: false, canSubmit: false, submit: () => undefined,
    })));
    await renderNested();
    const feed = container.querySelector<HTMLElement>(".conversation")!;
    const earlier = container.querySelector<HTMLElement>('[data-record-key="earlier"]')!;
    const target = container.querySelector<HTMLElement>('[data-record-key="nested-tool"]')!;
    let top = 20;
    const rect = (y: number, height: number) => new dom.window.DOMRect(0, y, 300, height);
    feed.getBoundingClientRect = () => rect(0, 300);
    earlier.getBoundingClientRect = () => rect(top, 40);
    target.getBoundingClientRect = () => rect(0, 0); // Hidden by closed task ancestors.
    Object.defineProperties(feed, { scrollHeight: { value: 1200 }, clientHeight: { value: 300 } });
    feed.scrollTop = 100;
    await act(async () => feed.dispatchEvent(new dom.window.Event("scroll")));
    top = 50;
    nested = nested.map((entry) => entry.id === "nested-tool" ? { ...entry, text: "partial", state: "Receiving output", activity: 50 } : entry);
    await renderNested();
    assert.equal(feed.scrollTop, 130, "Reader anchor follows geometry changes rather than jumping to bottom");
    const branches = [...container.querySelectorAll<HTMLDetailsElement>('[data-disclosure-key^="task:"]')];
    assert.equal(branches.length, 2);
    assert.ok(branches.every((branch) => !branch.open), "Incoming child activity never auto-expands ancestors");
    let navigated = false;
    target.scrollIntoView = () => { navigated = true; };
    const navigate = container.querySelector<HTMLButtonElement>(".activity-announcement button")!;
    assert.equal(navigate.textContent, "New task activity");
    await act(async () => navigate.click());
    assert.ok(navigated);
    assert.ok(branches.every((branch) => branch.open));
    assert.equal(target.querySelector<HTMLDetailsElement>(".execution-record")!.open, false);
    nested = nested.map((entry) => entry.id === "nested-tool" ? { ...entry, result: "done", state: "Completed" } : entry);
    await renderNested();
    assert.ok(branches.every((branch) => branch.open), "Explicit activity navigation choices survive completion");

    // The viewport starts inside a large child record. Its task ancestors also
    // contain the top; a later article is entirely offscreen and moves on growth.
    let height = 800;
    let shiftAbove = 0;
    nested = [...nested, { id: "later", kind: "assistant", text: "Later message" }];
    await renderNested();
    earlier.getBoundingClientRect = () => rect(-700, 40);
    target.getBoundingClientRect = () => rect(-200 + shiftAbove, height);
    for (const branch of branches) {
      branch.parentElement!.getBoundingClientRect = () => rect(-500, height + 700);
    }
    const later = container.querySelector<HTMLElement>('[data-record-key="later"]')!;
    later.getBoundingClientRect = () => rect(height - 200 + shiftAbove, 50);
    feed.scrollTop = 600;
    await act(async () => feed.dispatchEvent(new dom.window.Event("scroll")));
    height += 200;
    nested = nested.map((entry) => entry.id === "nested-tool" ? { ...entry, text: entry.text + "more output" } : entry);
    await renderNested();
    assert.equal(feed.scrollTop, 600, "Growth below the reading position must not follow the offscreen later article");
    shiftAbove = 30;
    nested = nested.map((entry) => entry.id === "nested-tool" ? { ...entry, text: entry.text + "another chunk" } : entry);
    await renderNested();
    assert.equal(feed.scrollTop, 630, "Anchor is the child being read, not its earlier task ancestor");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("incremental output stays mounted, bounded and selected across growth and result promotion", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    for (const boundary of ["characters", "lines"]) {
      let entry: Entry = { id: boundary, kind: "tool", title: "shell", state: "Receiving output", text: boundary === "characters" ? "x".repeat(2399) : "line\n".repeat(22) + "last" };
      const render = async () => act(async () => root.render(createElement(Conversation, {
        key: boundary, entries: [entry], sessionId: boundary, demo: false, canSubmit: false, submit: () => undefined,
      })));
      const toggle = async (details: HTMLDetailsElement, open: boolean) => act(async () => {
        details.open = open;
        details.dispatchEvent(new dom.window.Event("toggle"));
      });
      await render();
      await toggle(container.querySelector<HTMLDetailsElement>(".execution-record")!, true);
      const evidence = container.querySelector<HTMLElement>('[data-panel="stream"]')!;
      const reading = evidence.querySelector("pre")!;
      assert.equal(evidence.hidden, false);
      assert.equal(reading.getAttribute("role"), "region");
      assert.equal(reading.getAttribute("aria-label"), "Streamed output");
      reading.focus();
      reading.scrollTop = 40;
      // 2399 → 2400 → 2401 characters; 23 → 24 → 25 lines.
      for (let step = 0; step < 2; step++) {
        entry = { ...entry, text: entry.text + (boundary === "characters" ? "x" : "\nnext") };
        await render();
        assert.equal(container.querySelector('[data-panel="stream"]'), evidence);
        assert.equal(evidence.querySelector("pre"), reading);
        assert.equal(evidence.hidden, false);
        assert.equal(dom.window.document.activeElement, reading);
        assert.equal(reading.scrollTop, 40);
        assert.equal(reading.textContent, entry.text);
      }
      const details = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((button) => button.textContent === "Details")!;
      await act(async () => { details.click(); details.focus(); });
      entry = { ...entry, text: entry.text + "growth" };
      await render();
      assert.equal(evidence.hidden, true, "Selecting another tab is not undone by growth");
      entry = { ...entry, result: entry.text, state: "Completed" };
      await render();
      assert.equal(container.querySelector('[data-panel="stream"]'), evidence);
      assert.equal(evidence.hidden, true, "Promotion preserves tab selection too");
      assert.equal(dom.window.document.activeElement, details);
      assert.equal(reading.getAttribute("aria-label"), "Result");
    }
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("inspector tabs support keyboard navigation, preserve raw evidence, and report clipboard failure", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  let copied = "";
  let reject = false;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
    clipboard: { writeText: async (value: string) => { if (reject) throw new Error("Denied"); copied = value; } },
  } });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    let entry: Entry = { id: "inspect", kind: "tool", title: "channel_send", state: "Recorded result", text: "",
      inputs: '{"channel":"telegram","destination":"chat-123","large_id":9007199254740993}',
      result: "{'message_ids': ['123'], 'text': '<script>plain text</script>'}" };
    const render = () => act(async () => root.render(createElement(ExecutionRecord, { entry, open: true })));
    const selected = () => container.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')!;
    const panel = () => container.querySelector<HTMLElement>('[role="tabpanel"]:not([hidden])')!;
    const key = async (value: string) => act(async () => selected().dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: value, bubbles: true })));
    await render();
    assert.equal(selected().textContent, "Result", "Opening a saved call prioritizes its result");
    assert.equal(panel().querySelector("pre")!.textContent, entry.result);
    assert.equal(container.querySelector("script"), null);
    selected().focus();
    await key("ArrowRight");
    assert.equal(selected().textContent, "Inputs");
    assert.equal(dom.window.document.activeElement, selected());
    assert.equal(panel().id, selected().getAttribute("aria-controls"));
    assert.equal(panel().getAttribute("aria-labelledby"), selected().id);
    assert.equal(panel().querySelector("pre")!.textContent, entry.inputs, "Large integer literals are not reserialized or rounded");
    const copy = panel().querySelector<HTMLButtonElement>("button")!;
    await act(async () => copy.click());
    assert.equal(copied, entry.inputs);
    assert.equal(copy.textContent, "Copied");
    entry = { ...entry, inputs: entry.inputs + "\n" };
    await render();
    assert.equal(copy.textContent, "Copy", "Changed evidence does not keep a stale copied confirmation");
    reject = true;
    await act(async () => copy.click());
    assert.match(panel().querySelector('[role="status"]')!.textContent!, /Could not copy/);
    await key("End");
    assert.equal(selected().textContent, "Details");
    await key("ArrowRight");
    assert.equal(selected().textContent, "Result");
    await key("ArrowLeft");
    assert.equal(selected().textContent, "Details");
    await key("Home");
    assert.equal(selected().textContent, "Result");
    assert.equal(container.querySelectorAll('[role="tab"][tabindex="0"]').length, 1);

    // A result is a stable reading surface even if matching late output arrives.
    const reading = panel().querySelector("pre")!;
    reading.focus();
    reading.scrollTop = 45;
    entry = { ...entry, text: entry.result! };
    await render();
    assert.equal(panel().querySelector("pre"), reading);
    assert.equal(dom.window.document.activeElement, reading);
    assert.equal(reading.scrollTop, 45);
    await key("End");
    assert.equal(selected().textContent, "Details");
    await key("Home");
    assert.equal(selected().textContent, "Result");
    assert.equal(panel().querySelector("pre"), reading, "Late matching output must not replace the result when switching tabs");
    assert.equal(reading.scrollTop, 45);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "navigator", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("inspecting a tool near the bottom pauses conversation following until the reader returns to the feed", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    let entry: Entry = { id: "live", kind: "tool", title: "shell", text: "Starting", state: "Receiving output" };
    const render = () => act(async () => root.render(createElement(Conversation, {
      entries: [entry], sessionId: "focus", demo: false, canSubmit: false, submit: () => undefined,
    })));
    await render();
    const feed = container.querySelector<HTMLElement>(".conversation")!;
    const article = container.querySelector<HTMLElement>("article")!;
    const summary = article.querySelector("summary")!;
    let height = 1000;
    Object.defineProperties(feed, { scrollHeight: { get: () => height }, clientHeight: { value: 300 } });
    feed.getBoundingClientRect = () => new dom.window.DOMRect(0, 0, 390, 300);
    article.getBoundingClientRect = () => new dom.window.DOMRect(0, 200, 370, 100);
    summary.getBoundingClientRect = () => new dom.window.DOMRect(0, 200, 370, 44);
    feed.scrollTop = 700;
    await act(async () => feed.dispatchEvent(new dom.window.Event("scroll")));
    await act(async () => summary.focus());
    // Focus and an incidental scroll event must not re-enable following merely
    // because this short card was near the bottom when inspection began.
    await act(async () => feed.dispatchEvent(new dom.window.Event("scroll")));
    height = 1300;
    entry = { ...entry, text: "output\n".repeat(300) };
    await render();
    assert.equal(feed.scrollTop, 700);
    assert.equal(dom.window.document.activeElement, summary);
    await act(async () => feed.focus());
    feed.scrollTop = 1000;
    await act(async () => feed.dispatchEvent(new dom.window.Event("scroll")));
    height = 1400;
    entry = { ...entry, result: entry.text, state: "Completed" };
    await render();
    assert.equal(feed.scrollTop, 1400, "Returning to the feed's bottom resumes following");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]);
      else Reflect.deleteProperty(globalThis, name);
    }
  }
});
