import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { Conversation } from "./Conversation.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { executionTarget } from "./executionPresentation.js";
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
});

test("tool summary layout fixes mobile to two rows and preserves clipped evidence in titles/details", () => {
  const css = readFileSync(new URL("../../../src/app/styles.css", import.meta.url), "utf8");
  // jsdom has no layout engine. Assert the sizing contract directly, leaving
  // real 320/390px geometry and font rendering to the parent browser QA.
  const rule = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const matches = [...css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`, "g"))];
    assert.ok(matches.length, selector);
    return matches.map((match) => match[1]).join("\n");
  };
  assert.match(rule(".execution-tool-summary"), /display:\s*inline-grid/);
  assert.match(rule(".execution-tool-summary .execution-heading"), /display:\s*contents/);
  const targetRule = rule(".execution-tool-summary .execution-target");
  for (const declaration of [/white-space:\s*nowrap/, /text-overflow:\s*ellipsis/, /overflow:\s*hidden/, /min-width:\s*0/, /grid-row:\s*2/, /grid-column:\s*1 \/ -1/])
    assert.match(targetRule, declaration);
  assert.match(rule(".execution-tool-summary .execution-outcome"), /flex-wrap:\s*nowrap/);
  assert.match(rule(".execution-record > summary .execution-tool-summary"), /grid-template-columns:\s*minmax\(0, 1fr\) fit-content\(78%\)/);
  assert.match(rule(".execution-detail .code-block pre"), /max-height:\s*24rem/);
  assert.match(rule(".execution-detail .code-block pre"), /overflow:\s*auto/);
  assert.match(rule(".execution-evidence .code-block > h3"), /display:\s*none/);
  for (const title of ["read_file", "shell", "custom_".repeat(40)]) {
    const inputs = JSON.stringify({ path: "/long-segment".repeat(80), command: "python " + "argument ".repeat(80) });
    const html = renderToStaticMarkup(createElement(ExecutionRecord, {
      entry: { id: title, kind: "tool", title, inputs, text: "", state: "Waiting for approval", durationMs: 1200 },
    }));
    const dom = new JSDOM(html);
    const summary = dom.window.document.querySelector(".execution-tool-summary")!;
    assert.equal(summary.querySelector(".execution-target")!.getAttribute("title"), inputs);
    assert.equal(summary.querySelector(".execution-name")!.getAttribute("title"), title);
    assert.equal(summary.querySelector(".execution-state")!.textContent, "Waiting for approval");
    assert.equal(summary.querySelector(".execution-duration")!.textContent, "1.2 s");
    assert.equal(dom.window.document.querySelector(".execution-record")!.hasAttribute("open"), false);
    assert.ok(dom.window.document.querySelector(".execution-detail")!.textContent!.includes(inputs));
    dom.window.close();
  }
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
      const metadata = tool.querySelector<HTMLDetailsElement>(".execution-metadata")!;
      const key = tool.dataset.disclosureKey;
      assert.equal(tool.open, false);
      assert.equal(metadata.open, false);
      await toggle(tool, true);
      await toggle(metadata, true);
      receive({ event: "approval", approval_id: "nonce", tool: "shell" });
      await render();
      assert.match(tool.querySelector("summary")!.textContent!, /Waiting for approval/);
      receive({ event: "approval_closed", approval_id: "nonce", decision: "allow" });
      const output = "unique evidence line\n".repeat(300) + "FINAL RECEIVED LINE";
      receive({ event: "tool_output", text: output });
      await render();
      assert.equal(container.querySelector(".execution-record"), tool);
      assert.equal(tool.dataset.disclosureKey, key);
      assert.equal(tool.open, true);
      assert.equal(metadata.open, true);
      const stream = tool.querySelector<HTMLDetailsElement>('[data-disclosure-key^="stream:"]')!;
      assert.equal(stream.open, false);
      await toggle(stream, true);
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
      assert.equal(metadata.open, true);
      assert.match(tool.querySelector("summary")!.textContent!, failure ? /Error/ : /Completed/);
      assert.match(tool.querySelector("summary")!.textContent!, /Approval: Allowed once/);
      assert.match(tool.querySelector("summary")!.textContent!, /1.2 s/);
      cache.receive({ type: "event", epoch: "one", cursor, session_id: "root", record: terminal });
      await render();
      assert.equal(container.querySelectorAll(".execution-record").length, 1);
      assert.equal(container.querySelector(".execution-record"), tool);
      assert.equal(tool.open, !failure);
      await toggle(tool, true);
      assert.ok(tool.textContent!.includes("FINAL RECEIVED LINE"));
      if (failure) {
        assert.equal(tool.querySelector('[data-disclosure-key^="stream:"]'), stream);
        assert.equal(stream.open, true);
        assert.match(tool.textContent!, /Exit code 7/);
      } else {
        assert.equal(tool.querySelector('[data-disclosure-key^="stream:"]'), stream);
        assert.equal(tool.querySelector('[data-disclosure-key^="result:"]'), null);
        assert.equal(stream.open, true);
        assert.equal(stream.querySelector("pre"), reading);
        assert.equal(dom.window.document.activeElement, reading);
        assert.match(stream.querySelector("summary")!.textContent!, /^Result/);
        assert.match(tool.textContent!, /Streamed output matches/);
      }
      assert.ok(tool.textContent!.indexOf("Inputs") < tool.textContent!.indexOf("Execution metadata"));
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

test("incremental evidence crosses character and line limits without replacing, hiding or defocusing content", async () => {
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
      const evidence = container.querySelector<HTMLDetailsElement>('[data-disclosure-key^="stream:"]')!;
      const reading = evidence.querySelector("pre")!;
      assert.equal(evidence.open, true);
      assert.equal(reading.getAttribute("role"), "region");
      assert.equal(reading.getAttribute("aria-label"), "Streamed output");
      assert.doesNotMatch(evidence.querySelector("summary")!.textContent!, /Collapse content|Expand full content/);
      reading.focus();
      reading.scrollTop = 40;
      // 2399 → 2400 → 2401 characters; 23 → 24 → 25 lines.
      for (let step = 0; step < 2; step++) {
        entry = { ...entry, text: entry.text + (boundary === "characters" ? "x" : "\nnext") };
        await render();
        assert.equal(container.querySelector('[data-disclosure-key^="stream:"]'), evidence);
        assert.equal(evidence.querySelector("pre"), reading);
        assert.equal(evidence.open, true);
        assert.equal(dom.window.document.activeElement, reading);
        assert.equal(reading.scrollTop, 40);
        assert.equal(reading.textContent, entry.text);
      }
      await toggle(evidence, false);
      const summary = evidence.querySelector("summary")!;
      summary.focus();
      entry = { ...entry, text: entry.text + "growth" };
      await render();
      assert.equal(evidence.open, false, "User-closed output never auto-expands on growth");
      entry = { ...entry, result: entry.text, state: "Completed" };
      await render();
      assert.equal(container.querySelector('[data-disclosure-key^="stream:"]'), evidence);
      assert.equal(evidence.open, false, "Promotion preserves an explicit closed choice too");
      assert.equal(dom.window.document.activeElement, summary);
      assert.match(summary.textContent!, /^Result/);
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
